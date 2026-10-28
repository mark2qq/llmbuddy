# -*- coding: utf-8 -*-
"""
长期记忆（1.0.12）回归：文件层 + 注入 + 工具 + 配置 + 接口校验。

覆盖：
    · 空记忆不占提示词；写入后 build_hint 出现「## 记忆」三块
    · 日志按天追加（同一天累加、不覆盖）、空间记忆首次带模板、全局记忆同理
    · 模板脚手架（H1 / > 说明 / <!-- 例如 -->）不进 system
    · 注入预算：超长截断并标注
    · `memory_read` / `memory_append` 两个工具：注册、风险级、分组、真实落盘
    · `memory_enabled=false` 时不注入（文件仍保留）
    · `PUT /memory` 的参数校验与三种 scope
全部用例跑在临时目录，不碰真实数据目录、不联网。
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import agent as agent_mod
from core.assistant import memory as memory_mod
from core.assistant import store as store_mod
from core.assistant.providers import ChatReply
from core.assistant.tools import ToolRegistry


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把 assistant 目录整体搬进 tmp_path，并把 memory 的三个路径一起重定向。"""
    root = tmp_path / "assistant"
    for sub in ("skills", "sessions", "audit", "workspace", "catalog"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    mem = root / "memory"
    monkeypatch.setattr(store_mod, "ASSISTANT_DIR", str(root))
    monkeypatch.setattr(store_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(store_mod, "SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(store_mod, "AUDIT_DIR", str(root / "audit"))
    monkeypatch.setattr(store_mod, "CONFIG_PATH", str(root / "config.json"))
    monkeypatch.setattr(store_mod, "DEFAULT_WORKSPACE", str(root / "workspace"))
    monkeypatch.setattr(store_mod, "CATALOG_DIR", str(root / "catalog"))
    monkeypatch.setattr(memory_mod, "MEMORY_DIR", str(mem))
    monkeypatch.setattr(memory_mod, "GLOBAL_MD", str(mem / "MEMORY.md"))
    monkeypatch.setattr(memory_mod, "SPACES_DIR", str(mem / "spaces"))
    # agent 单例与两个管理器都要重置，否则会拿着别处的工作区跑
    monkeypatch.setattr(agent_mod, "_ASSISTANT", None)
    from core.assistant import mcp as mcp_mod
    from core.assistant import approval as approval_mod
    monkeypatch.setattr(mcp_mod, "_MANAGER", None)
    monkeypatch.setattr(approval_mod, "_MANAGER", None)
    store_mod.ensure_dirs()
    ws = str(root / "workspace")
    return {"root": root, "ws": ws, "mem": mem}


class FakeProvider:
    """只回一句话的假提供商：用来抓 system 提示词。"""

    def __init__(self):
        self.calls = []

    def resolve_model(self, preferred=""):
        return preferred or "fake-model"

    def chat(self, messages, tools=None, model="", temperature=0.3,
             max_tokens=2048, on_delta=None, cancel_check=None, timeout=300):
        self.calls.append({"messages": messages, "tools": tools})
        return ChatReply(content="好")


def _system_of(provider) -> str:
    msgs = provider.calls[-1]["messages"]
    return "\n".join(m["content"] for m in msgs if m.get("role") == "system")


# ----------------------------------------------------------------------------
# 1. 文件层
# ----------------------------------------------------------------------------
def test_empty_memory_adds_nothing_to_prompt(env):
    """一条记忆都没有时不注入——不能凭一堆空标题吃上下文。"""
    assert memory_mod.build_hint(env["ws"]) == ""
    assert memory_mod.log_summary_line(env["ws"]) == ""


def test_append_log_creates_dated_file_and_appends(env):
    """日志落到 spaces/<slug>/YYYY-MM-DD.md，同一天多次追加是累加。"""
    path = memory_mod.append_log(env["ws"], "第一件事")
    assert path and os.path.exists(path)
    assert os.path.basename(path) == time.strftime("%Y-%m-%d") + ".md"
    memory_mod.append_log(env["ws"], "第二件事")
    body = memory_mod.read_log(env["ws"])
    assert "第一件事" in body and "第二件事" in body
    assert body.count("# 工作日志") == 1          # 标题只出现一次
    # 多行内容缩进两格，Markdown 列表不断行
    memory_mod.append_log(env["ws"], "第三件事\n细节：用缩进")
    assert "\n  细节：用缩进" in memory_mod.read_log(env["ws"])


def test_space_memory_gets_template_once(env):
    """空间记忆首次写入自动套模板；第二次追加不重复模板。"""
    memory_mod.append_memory(env["ws"], "结论 A")
    first = memory_mod.read_space(env["ws"])
    assert "## 项目约定" in first and "结论 A" in first
    memory_mod.append_memory(env["ws"], "结论 B")
    second = memory_mod.read_space(env["ws"])
    assert second.count("# 空间记忆") == 1
    assert "结论 A" in second and "结论 B" in second


def test_global_memory_roundtrip(env):
    assert memory_mod.read_global() == ""
    memory_mod.append_global("回答用简体中文")
    assert "回答用简体中文" in memory_mod.read_global()
    assert memory_mod.save_global("整篇替换") is True
    assert memory_mod.read_global().strip() == "整篇替换"


def test_snapshot_shape(env):
    memory_mod.append_log(env["ws"], "一条")
    memory_mod.append_global("全局一条")
    snap = memory_mod.snapshot(env["ws"])
    assert snap["enabled"] is True
    assert snap["workspace"]
    assert "全局一条" in snap["global"]["text"]
    assert snap["space"]["path"].endswith("MEMORY.md")
    assert [x["date"] for x in snap["logs"]] == [time.strftime("%Y-%m-%d")]
    assert snap["logs"][0]["text"].strip()
    assert snap["stats"]["logs"] == 1
    assert snap["stats"]["global_chars"] > 0


# ----------------------------------------------------------------------------
# 2. 注入
# ----------------------------------------------------------------------------
def test_hint_has_three_sections_and_no_template_noise(env):
    memory_mod.append_global("偏好：先给结论")
    memory_mod.append_memory(env["ws"], "约定：提交信息用中文")
    memory_mod.append_log(env["ws"], "今天做了接口")
    hint = memory_mod.build_hint(env["ws"])
    assert hint.startswith("\n## 记忆")
    assert "### 全局记忆" in hint and "### 空间记忆" in hint
    assert "### 最近工作日志" in hint
    assert "偏好：先给结论" in hint
    assert "约定：提交信息用中文" in hint
    assert "今天做了接口" in hint
    # 模板脚手架（H1 / 注释里的「例如」）不该进提示词
    assert "例如：" not in hint
    assert "\n# 全局记忆" not in hint
    assert "\n# 空间记忆" not in hint
    # 写记忆的指令要在，否则模型不会主动记
    assert "memory_append" in hint


def test_hint_respects_enabled_flag(env):
    memory_mod.append_global("偏好：先给结论")
    assert memory_mod.build_hint(env["ws"], enabled=False) == ""
    assert memory_mod.read_global()          # 关掉注入不等于删数据


def test_hint_truncates_long_memory(env):
    memory_mod.save_global("啊" * (memory_mod.MAX_GLOBAL_CHARS + 500))
    hint = memory_mod.build_hint(env["ws"])
    assert "已截断" in hint
    assert len(hint) <= memory_mod.MAX_HINT_CHARS + 600


def test_log_summary_line_counts(env):
    memory_mod.append_log(env["ws"], "一条")
    line = memory_mod.log_summary_line(env["ws"])
    assert "记忆" in line and "1 天日志" in line


# ----------------------------------------------------------------------------
# 3. 工具
# ----------------------------------------------------------------------------
def test_memory_tools_registered(env):
    reg = ToolRegistry(env["ws"])
    rows = {t["name"]: t for t in reg.list()}
    assert "memory_read" in rows and "memory_append" in rows
    assert rows["memory_read"]["risk"] == "low"
    assert rows["memory_append"]["risk"] == "medium"
    for name in ("memory_read", "memory_append"):
        assert rows[name]["meta"].get("default") is True
        assert rows[name]["meta"].get("group") == "记忆"
    # 两个工具都在下发的 schema 里（不是只登记不给模型）
    names = {s["function"]["name"] for s in reg.select_schemas()}
    assert {"memory_read", "memory_append"} <= names


def test_memory_append_tool_writes_then_read_back(env):
    reg = ToolRegistry(env["ws"])
    res = reg.call("memory_append", {"text": "用户喜欢表格", "to": "memory"})
    assert res.ok, res.error
    assert "MEMORY.md" in res.content
    got = reg.call("memory_read", {"scope": "space"})
    assert got.ok and "用户喜欢表格" in got.content
    # 写日志后再读日志清单
    reg.call("memory_append", {"text": "今天调了接口"})
    listed = reg.call("memory_read", {"scope": "log"})
    assert listed.ok and "今天调了接口".replace("今天", "") in listed.content


def test_memory_append_tool_rejects_empty(env):
    reg = ToolRegistry(env["ws"])
    res = reg.call("memory_append", {"text": "   "})
    assert not res.ok and "不能为空" in (res.error or "")


def test_memory_read_tool_on_empty_is_ok(env):
    """没有记忆不算失败：返回「还是空的」，别让模型以为工具坏了。"""
    reg = ToolRegistry(env["ws"])
    res = reg.call("memory_read", {"scope": "global"})
    assert res.ok and "空" in res.content


# ----------------------------------------------------------------------------
# 4. 配置与接口
# ----------------------------------------------------------------------------
def test_config_declares_memory_enabled(env):
    cfg = store_mod.load_config()
    assert cfg.get("memory_enabled") is True
    # 关掉后读回来仍是 False（浅合并不能把它丢掉）
    store_mod.save_config({"memory_enabled": False})
    assert store_mod.load_config().get("memory_enabled") is False


def test_router_memory_get_and_put(env):
    from routers import assistant as api
    got = asyncio.run(api.memory_get(workspace=env["ws"]))
    assert got["code"] == 0 and got["data"]["workspace"]
    saved = asyncio.run(api.memory_put(
        {"workspace": env["ws"], "scope": "global", "text": "偏好：中文"}))
    assert saved["code"] == 0
    assert "偏好：中文" in memory_mod.read_global()
    # 空间记忆
    saved = asyncio.run(api.memory_put(
        {"workspace": env["ws"], "scope": "space", "text": "约定：中文提交"}))
    assert saved["code"] == 0
    assert "约定：中文提交" in memory_mod.read_space(env["ws"])
    # 某天日志
    saved = asyncio.run(api.memory_put(
        {"workspace": env["ws"], "scope": "log", "date": "2026-01-02",
         "text": "补记一条"}))
    assert saved["code"] == 0
    assert "补记一条" in memory_mod.read_log(env["ws"], "2026-01-02")


def test_router_memory_put_validation(env):
    from routers import assistant as api
    bad = asyncio.run(api.memory_put({"scope": "global"}))
    assert bad["code"] == 2050
    too_long = asyncio.run(api.memory_put(
        {"scope": "global", "text": "x" * (memory_mod.MAX_WRITE_CHARS + 1)}))
    assert too_long["code"] == 2051
    unknown = asyncio.run(api.memory_put({"scope": "??", "text": "x"}))
    assert unknown["code"] == 2053


# ----------------------------------------------------------------------------
# 5. Agent：记忆进 system（端到端）
# ----------------------------------------------------------------------------
def _run_turn(env, monkeypatch, text="你好"):
    provider = FakeProvider()
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events: list = []
    ass.start_turn(session["id"], text, emit=events.append)
    return provider, session, events


def test_agent_injects_memory_into_system(env, monkeypatch):
    """记忆真的进了 system —— 这是「集成到系统」的验收点。"""
    memory_mod.append_global("偏好：回答先给结论")
    memory_mod.append_log(env["ws"], "今天把端口改成了 8021")
    provider, session, events = _run_turn(env, monkeypatch)
    system = _system_of(provider)
    assert "## 记忆（本机长期记忆" in system
    assert "偏好：回答先给结论" in system
    assert "今天把端口改成了 8021" in system
    # 注入的是**任务所属空间**的记忆（session.workspace），不是别的目录
    assert os.path.normcase(session["workspace"]) == os.path.normcase(env["ws"])
    # 状态里有记忆体量，界面菜单能显示
    st = agent_mod.get_assistant().state()
    assert st["memory"]["global_chars"] > 0


def test_agent_skips_memory_when_disabled(env, monkeypatch):
    memory_mod.append_global("偏好：回答先给结论")
    store_mod.save_config({"memory_enabled": False})
    provider, _session, _events = _run_turn(env, monkeypatch)
    assert "## 记忆" not in _system_of(provider)
