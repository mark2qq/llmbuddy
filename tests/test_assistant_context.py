# -*- coding: utf-8 -*-
"""
上下文窗口管理回归（1.0.11）。

用户需求原话：「请分析 workbuddy 里上下文处理是怎么实现的，然后模仿一样实现；
同时输出信息时屏幕显示应该是最新的输出信息而不是需要手动滚动」。

本文件覆盖的是「上下文怎么处理」，滚动跟随在客户端用例里。

要点：
    · token 估算的保守口径（汉字 1 token / 其它 4 字符 1 token）；
    · 未超阈值**不压缩**（短会话行为与老版本完全一致）；
    · 超阈值 → 较早消息压成摘要、保留最近若干条，原件仍留在会话里；
    · 压缩后摘要以「user 说明 + assistant 确认」置顶（兼容单 system 端点）；
    · 滚动压缩：再次超线时把「旧摘要 + 新一批」合并重压，摘要不会无限膨胀；
    · 摘要调用失败必须降级到规则摘要，**绝不能影响本轮对话**；
    · 附件（粘贴的图片）路径要进到本轮上下文里，但不进 base64。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import context as ctx_mod


# ----------------------------------------------------------------------------
# 估算
# ----------------------------------------------------------------------------
def test_estimate_counts_cjk_as_one_token():
    assert ctx_mod.estimate_tokens("") == 0
    assert ctx_mod.estimate_tokens("你好世界") == 4


def test_estimate_counts_ascii_by_four_chars():
    # 11 个 ASCII 字符 -> ceil(11/4) = 3
    assert ctx_mod.estimate_tokens("hello world") == 3


def test_estimate_mixed_text():
    # 4 汉字 + 8 ASCII -> 4 + 2 = 6
    assert ctx_mod.estimate_tokens("你好世界abcdefgh") == 6


def test_context_limit_accepts_k_suffix_and_numbers():
    assert ctx_mod.context_limit({}) == ctx_mod.DEFAULT_LIMIT
    assert ctx_mod.context_limit({"context_window": "32k"}) == 32 * 1024
    assert ctx_mod.context_limit({"context_window": 4096}) == 4096
    assert ctx_mod.context_limit({"context_window": "abc"}) == ctx_mod.DEFAULT_LIMIT


# ----------------------------------------------------------------------------
# 不压缩 / 压缩
# ----------------------------------------------------------------------------
def _session(n_pairs: int, chars: int = 400) -> dict:
    msgs = []
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"第{i}问 " + "甲" * chars})
        msgs.append({"role": "assistant", "content": f"第{i}答 " + "乙" * chars})
    return {"messages": msgs}


def test_short_session_is_not_compressed():
    s = _session(2, 40)
    assert ctx_mod.maybe_compress(s, {"context_window": 8192}) is None
    assert "context" not in s


def test_usage_reports_ratio_without_compression():
    s = _session(2, 40)
    u = ctx_mod.usage(s, {"context_window": 8192})
    assert u["limit"] == 8192
    assert 0 < u["used"] < 8192
    assert u["compressed"] is False


def test_over_threshold_compresses_and_keeps_tail():
    s = _session(8, 600)          # 16 条 × ~600 token 级别，远超 2048 的 72%
    ev = ctx_mod.maybe_compress(s, {"context_window": 2048})
    assert ev is not None and ev["compressed"] is True
    assert ev["folded"] > 0
    c = s["context"]
    assert c["summary"]
    # 原件一条都没删（回看历史不受影响）
    assert len(s["messages"]) == 16
    # 保留窗口至少 MIN_KEEP 条
    assert c["keep"] >= ctx_mod.MIN_KEEP


def test_compressed_usage_drops_below_before():
    s = _session(10, 600)
    before = ctx_mod.usage(s, {"context_window": 4096})["used"]
    ctx_mod.maybe_compress(s, {"context_window": 4096})
    after = ctx_mod.usage(s, {"context_window": 4096})["used"]
    assert after < before
    assert ctx_mod.usage(s, {"context_window": 4096})["compressed"] is True


def test_llm_summary_failure_falls_back_to_rules():
    class Boom:
        def chat(self, *a, **kw):
            raise RuntimeError("模型挂了")

    s = _session(8, 600)
    ev = ctx_mod.maybe_compress(s, {"context_window": 2048}, Boom())
    assert ev["compressed"] is True
    assert ev["via"] == "rule"
    assert s["context"]["summary"].strip()


def test_llm_summary_is_used_when_provider_works():
    class Fake:
        def chat(self, messages, **kw):
            class R:
                content = "模型给出的摘要"
                error = ""
            return R()

    s = _session(8, 600)
    ev = ctx_mod.maybe_compress(s, {"context_window": 2048}, Fake())
    assert ev["via"] == "llm"
    assert s["context"]["summary"] == "模型给出的摘要"


def test_rolling_compression_merges_previous_summary():
    s = _session(6, 600)
    ctx_mod.maybe_compress(s, {"context_window": 2048})
    first = s["context"]["summary"]
    # 再来一大批消息，再次超线
    for i in range(6):
        s["messages"].append({"role": "user", "content": "追加" * 400})
        s["messages"].append({"role": "assistant", "content": "回应" * 400})
    ev = ctx_mod.maybe_compress(s, {"context_window": 2048})
    assert ev is not None and ev["compressed"] is True
    # 旧摘要必须被合并进来（不能丢），且新摘要长度仍受上限约束
    second = s["context"]["summary"]
    assert len(second) <= ctx_mod.SUMMARY_MAX_CHARS
    assert second != first


def test_force_compress_even_below_threshold():
    s = _session(4, 100)
    ev = ctx_mod.maybe_compress(s, {"context_window": 8192}, force=True)
    assert ev is not None and ev["compressed"] is True


def test_no_history_returns_none():
    assert ctx_mod.maybe_compress({"messages": []}, {}) is None


# ----------------------------------------------------------------------------
# agent._build_messages：摘要置顶 + 附件路径
# ----------------------------------------------------------------------------
def _agent():
    from core.assistant import agent as agent_mod
    return agent_mod.Assistant()


def test_build_messages_prepends_summary_when_compressed():
    a = _agent()
    s = _session(10, 600)
    ctx_mod.maybe_compress(s, {"context_window": 2048})
    cfg = {"workspace": os.getcwd(), "policy": "readonly_auto"}
    msgs = a._build_messages(s, a.registry(), cfg, schemas=[])
    assert msgs[0]["role"] == "system"
    assert "压缩摘要" in msgs[1]["content"]
    assert msgs[2]["role"] == "assistant"
    # 摘要之后只跟保留窗口内的消息
    keep = int(s["context"]["keep"])
    assert len(msgs) == 3 + keep


def test_build_messages_without_summary_keeps_old_behaviour():
    a = _agent()
    s = _session(3, 20)
    cfg = {"workspace": os.getcwd(), "policy": "readonly_auto"}
    msgs = a._build_messages(s, a.registry(), cfg, schemas=[])
    assert len(msgs) == 1 + 6          # system + 3 轮问答
    assert "压缩摘要" not in msgs[1]["content"]


def test_build_messages_appends_attachment_paths_to_last_user():
    a = _agent()
    s = _session(1, 10)
    cfg = {"workspace": os.getcwd(), "policy": "readonly_auto"}
    msgs = a._build_messages(s, a.registry(), cfg, schemas=[],
                             attachments=[r"C:\tmp\paste-1.png"])
    last_user = [m for m in msgs if m["role"] == "user"][-1]
    assert r"C:\tmp\paste-1.png" in last_user["content"]
    assert "fs_read" in last_user["content"]
    # 绝不能把图片编码进正文（会瞬间吃满上下文）
    assert "base64" not in last_user["content"].lower()


def test_attachment_note_only_on_last_user_message():
    a = _agent()
    s = _session(3, 10)
    cfg = {"workspace": os.getcwd(), "policy": "readonly_auto"}
    msgs = a._build_messages(s, a.registry(), cfg, schemas=[],
                             attachments=["/tmp/a.png"])
    users = [m for m in msgs if m["role"] == "user"]
    assert sum("/tmp/a.png" in m["content"] for m in users) == 1
    assert "/tmp/a.png" in users[-1]["content"]
