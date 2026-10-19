# -*- coding: utf-8 -*-
"""
上下文适配回归测试（test_context_fit.py）。

背景（2026-10-19 线上故障）：
    WorkBuddy 等 Agent 客户端单次请求把「系统提示 + 上百个工具 JSON Schema + 历史」
    一起发送，token 量常达 3~4 万；旧网关只裁剪 messages、完全没计算 tools，
    引擎于是返回 400 `exceed_context_size_error`，客户端只显示「未知错误」。

本测试只验证纯逻辑（不需要起引擎）：
    1. fit_payload 计入 tools，并按 200 字 → 80 字 → 丢弃工具的次序降级；
    2. 上下文够大时不做无谓裁剪；
    3. 仍放不下时返回 overflow，由网关回 400 中文指引；
    4. 引擎英文错误被翻译成可操作中文。
"""

import json

import pytest

from routers.gateway import (
    CTX_MARGIN,
    _friendly_engine_error,
    estimate_tokens,
    estimate_tools,
    fit_payload,
    trim_messages,
)


def _huge_tools(n: int = 8, repeat: int = 4000):
    desc = "工具调用说明。" * repeat
    return [{
        "type": "function",
        "function": {
            "name": f"tool{i}",
            "description": desc,
            "parameters": {"type": "object", "properties": {}},
        },
    } for i in range(n)]


def test_estimate_tools_counts_tool_schema():
    """tools 必须计入 token（旧实现恒为 0，是本次故障根因）。"""
    assert estimate_tools(None) == 0
    assert estimate_tools([]) == 0
    assert estimate_tools(_huge_tools(1, 100)) > 100


def test_fit_payload_keeps_tools_when_ctx_enough():
    tools = _huge_tools(2, 200)
    msgs = [{"role": "user", "content": "你好"}]
    out_msgs, out_tools, dropped, used, budget, overflow = fit_payload(
        msgs, tools, 32768, 2048)
    assert dropped is False
    assert overflow is False
    assert out_tools is not None and len(out_tools) == 2
    assert used <= budget


def test_fit_payload_slims_tools_then_fits():
    """32k 上下文 + 超大工具描述：截断 description 后应能塞进预算。"""
    tools = _huge_tools(8, 4000)
    msgs = [{"role": "system", "content": "你是助手。" * 1000},
            {"role": "user", "content": "你好"}]
    _, out_tools, dropped, used, budget, overflow = fit_payload(
        msgs, tools, 32768, 2048)
    assert dropped is False
    assert overflow is False
    assert used <= budget
    # 工具名与参数结构必须保留，只截断描述
    assert [t["function"]["name"] for t in out_tools] == [f"tool{i}" for i in range(8)]
    assert all("parameters" in t["function"] for t in out_tools)
    desc = out_tools[0]["function"]["description"]
    # 分级截断第一档是 1000 字（尽量多保留描述，Agent 客户端靠它选工具）
    assert len(desc) <= 1001, len(desc)


def test_fit_payload_drops_tools_and_reports_overflow_when_impossible():
    """8k 上下文 + 13k 系统提示：降级到丢工具仍放不下，必须 overflow=True。"""
    tools = _huge_tools(8, 4000)
    msgs = [{"role": "system", "content": "你是助手。" * 5000},
            {"role": "user", "content": "你好"}]
    out_msgs, out_tools, dropped, used, budget, overflow = fit_payload(
        msgs, tools, 8192, 2048)
    assert dropped is True
    assert out_tools is None
    assert overflow is True
    # 加了一条提示「工具已禁用」
    assert out_msgs and out_msgs[0]["role"] == "system"


def test_fit_payload_trims_history_keeping_latest():
    msgs = [{"role": "user", "content": "旧历史。" * 20000} for _ in range(3)]
    msgs.append({"role": "user", "content": "最新问题"})
    out_msgs, _, _, used, budget, overflow = fit_payload(msgs, None, 8192, 512)
    assert overflow is False
    assert used <= budget
    assert out_msgs[-1]["content"] == "最新问题"


def test_fit_payload_clamps_max_tokens():
    """max_tokens 大于窗口时不得把 budget 压成负数。"""
    _, _, _, _, budget, _ = fit_payload(
        [{"role": "user", "content": "hi"}], None, 2048, 999999)
    assert budget >= 64


def test_friendly_engine_error_translates_context_overflow():
    # 真实 llama.cpp 报文结构：n_prompt_tokens 在 error 对象内部（2026-10-19 实测抓包）
    payload = {"error": {"code": 400, "type": "exceed_context_size_error",
                         "message": "request (38593 tokens) exceeds the available "
                                    "context size (8192 tokens), try increasing it",
                         "n_prompt_tokens": 38593, "n_ctx": 8192}}
    err = _friendly_engine_error(payload, 8192, "qwen2.5-0.5b-instruct-q4_k_m")
    msg = err["error"]["message"]
    assert err["error"]["code"] == 1010
    assert "上下文超限" in msg
    assert "38593" in msg and "8192" in msg
    assert "n_ctx" in msg


def test_friendly_engine_error_reads_flat_n_prompt_tokens_too():
    """兼容顶层 n_prompt_tokens（老格式/其它引擎实现）。"""
    payload = {"error": {"code": 400,
                         "message": "request (1234 tokens) exceeds the available context size"},
               "n_prompt_tokens": 1234}
    err = _friendly_engine_error(payload, 8192, "m")
    assert "1234" in err["error"]["message"]


def test_friendly_engine_error_passes_through_other_errors():
    err = _friendly_engine_error({"error": {"message": "boom"}}, 8192, "m")
    assert err["error"]["message"] == "boom"
    assert err["error"]["type"] == "engine_error"


def test_friendly_engine_error_handles_non_json_bytes():
    err = _friendly_engine_error(b"<html>502</html>", 8192, "m")
    assert err["error"]["code"] == 1009
