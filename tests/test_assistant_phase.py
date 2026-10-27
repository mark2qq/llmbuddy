# -*- coding: utf-8 -*-
"""
思考/执行阶段串行化回归（2026-10-27）。

用户需求：后端执行必须严格串行 ——
    「思考 → 执行 → 如有问题再思考 → 再执行」，
    不得出现思考与工具/技能调用同时发生。

覆盖：
    - PhaseGate 本身：思考阶段直通、执行阶段缓存、切回思考按原顺序补发
    - 阶段事件只能由闸门发（调用方塞 phase 事件会被丢掉）
    - agent.start_turn 的事件序列：phase(thinking,1) → … → phase(executing,1)
      → tool_call/tool_result → phase(thinking,2) → done
    - 异步 provider 在执行阶段继续推 token 时，事件被推迟到执行结束之后，
      且内容一个不丢（用户看到的顺序永远是「先执行完，再继续思考」）
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import skills as skills_mod
from core.assistant import store as store_mod
from core.assistant import turn_phase as tp
from core.assistant.providers import ChatReply


# ----------------------------------------------------------------------------
# 夹具（与 test_assistant.py 同口径：整个助理目录指到 tmp_path）
# ----------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    for sub in ("skills", "sessions", "audit", "workspace"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "ASSISTANT_DIR", str(root))
    monkeypatch.setattr(store_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(store_mod, "SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(store_mod, "AUDIT_DIR", str(root / "audit"))
    monkeypatch.setattr(store_mod, "CONFIG_PATH", str(root / "config.json"))
    monkeypatch.setattr(store_mod, "DEFAULT_WORKSPACE", str(root / "workspace"))
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(root / "skills"))

    from core.assistant import agent as agent_mod
    from core.assistant import mcp as mcp_mod
    from core.assistant import approval as approval_mod
    monkeypatch.setattr(agent_mod, "_ASSISTANT", None)
    monkeypatch.setattr(mcp_mod, "_MANAGER", None)
    monkeypatch.setattr(approval_mod, "_MANAGER", None)
    store_mod.ensure_dirs()
    skills_mod.ensure_builtin_skills()
    return {"root": root, "workspace": root / "workspace"}


class FakeProvider:
    """按队列返回预设回复的假模型（可注入「执行期间推 token」的异步尾巴）。"""

    def __init__(self, replies, late=None):
        self.replies = list(replies)
        self.calls = 0
        self.late = late            # callable(on_delta) -> None（在后台线程延迟回调）

    def resolve_model(self, preferred=""):
        return preferred or "fake-model"

    def chat(self, messages, tools=None, model="", temperature=0.3,
             max_tokens=2048, on_delta=None, cancel_check=None, timeout=300):
        self.calls += 1
        r = self.replies.pop(0) if self.replies else ChatReply(content="完成")
        if on_delta and r.reasoning:
            on_delta("reasoning", r.reasoning)
        if on_delta and r.content:
            on_delta("content", r.content)
        if self.late is not None and self.calls == 1 and on_delta:
            # 模拟「provider 在 chat() 返回后继续推 token」（异步流式实现的尾巴）
            self.late(on_delta)
        return r


def _kinds(events):
    return [(e.get("type"), e.get("phase")) for e in events]


def _between(events, start_phase, end_phase):
    """取 [phase=start_phase 之后, phase=end_phase 之前] 的事件类型列表。"""
    out = []
    inside = False
    for e in events:
        if e.get("type") == "phase":
            if e.get("phase") == start_phase:
                inside = True
                continue
            if e.get("phase") == end_phase and inside:
                break
        if inside:
            out.append(e.get("type"))
    return out


# ----------------------------------------------------------------------------
# PhaseGate 单元
# ----------------------------------------------------------------------------
def test_gate_passes_model_events_while_thinking():
    got = []
    g = tp.PhaseGate(got.append)
    g.emit({"type": "reasoning", "text": "想"})
    g.emit({"type": "delta", "text": "答"})
    assert [e["type"] for e in got] == ["reasoning", "delta"]
    assert g.buffered == 0 and g.phase == tp.THINKING


def test_gate_buffers_model_events_while_executing():
    got = []
    g = tp.PhaseGate(got.append)
    g.enter_executing(1)
    g.emit({"type": "reasoning", "text": "偷偷想"})
    g.emit({"type": "delta", "text": "偷偷说"})
    g.emit({"type": "tool_call", "name": "fs_list"})
    # 执行阶段：只有 phase + tool_call 放行，思考/正文必须被挡住
    assert [e["type"] for e in got] == ["phase", "tool_call"]
    assert g.buffered == 2

    g.enter_thinking(2)
    # 顺序：先补发缓存（保序），再宣布阶段切换
    assert [e["type"] for e in got] == [
        "phase", "tool_call", "reasoning", "delta", "phase"]
    assert got[0]["phase"] == tp.EXECUTING and got[4]["phase"] == tp.THINKING


def test_gate_rejects_foreign_phase_events():
    """阶段只能由闸门发：调用方自己塞 phase 会被丢掉（防互相矛盾的阶段）。"""
    got = []
    g = tp.PhaseGate(got.append)
    g.emit({"type": "phase", "phase": "executing"})
    assert got == []


def test_gate_flush_never_loses_content():
    got = []
    g = tp.PhaseGate(got.append)
    g.enter_executing(1)
    g.emit({"type": "delta", "text": "尾巴"})
    g.flush()
    assert [e["type"] for e in got] == ["phase", "delta"]
    assert g.buffered == 0


def test_gate_round_number_advances():
    got = []
    g = tp.PhaseGate(got.append)
    g.enter_executing(1)
    g.enter_thinking(2)
    rounds = [(e["phase"], e["round"]) for e in got if e["type"] == "phase"]
    assert rounds == [(tp.EXECUTING, 1), (tp.THINKING, 2)]


# ----------------------------------------------------------------------------
# Agent 集成：事件序列
# ----------------------------------------------------------------------------
def test_turn_phase_sequence_thinking_then_executing(env, monkeypatch):
    from core.assistant import agent as agent_mod
    provider = FakeProvider([
        ChatReply(reasoning="先看看目录", content="",
                  tool_calls=[{"id": "c1", "name": "fs_list",
                               "arguments": {"path": "."}}]),
        ChatReply(reasoning="拿到结果了", content="目录是空的"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "看看目录", emit=events.append)
    assert res["ok"], res

    phases = [(e["phase"], e.get("round")) for e in events if e["type"] == "phase"]
    assert phases == [(tp.THINKING, 1), (tp.EXECUTING, 1), (tp.THINKING, 2)]

    # 执行阶段里不允许出现思考/正文事件
    during = _between(events, tp.EXECUTING, tp.THINKING)
    assert "reasoning" not in during and "delta" not in during
    assert "tool_call" in during and "tool_result" in during

    # 第一轮思考的 reasoning 在执行之前，第二轮的 reasoning 在执行之后
    first_reason = next(i for i, e in enumerate(events)
                        if e.get("text") == "先看看目录")
    second_reason = next(i for i, e in enumerate(events)
                         if e.get("text") == "拿到结果了")
    exec_at = _index_of(events, tp.EXECUTING)
    think2_at = _index_of(events, tp.THINKING, start=exec_at)
    tool_at = next(i for i, e in enumerate(events) if e["type"] == "tool_call")
    assert first_reason < exec_at < tool_at < second_reason < think2_at or (
        first_reason < exec_at < tool_at < think2_at < second_reason)


def test_late_tokens_during_execution_are_deferred(env, monkeypatch):
    """provider 在执行阶段继续推 token：内容必须排到执行结束之后，不交叉。"""
    from core.assistant import agent as agent_mod
    holder = {}

    def later(on_delta):
        def run():
            time.sleep(0.05)                 # 等工具开始执行
            on_delta("reasoning", "迟到的思考")
        holder["t"] = threading.Thread(target=run, daemon=True)
        holder["t"].start()

    provider = FakeProvider([
        ChatReply(reasoning="先跑个命令", content="",
                  tool_calls=[{"id": "c1", "name": "fs_list",
                               "arguments": {"path": "."}}]),
        ChatReply(content="好了"),
    ], late=later)
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    import core.assistant.tools as tools_mod
    orig = tools_mod.ToolRegistry.call

    def slow_call(self, name, args, ctx=None):
        time.sleep(0.3)                       # 让迟到的 token 落在执行中间
        return orig(self, name, args, ctx)

    monkeypatch.setattr(tools_mod.ToolRegistry, "call", slow_call)

    ass.start_turn(session["id"], "跑一下", emit=events.append)
    exec_at = _index_of(events, tp.EXECUTING)
    tool_at = next(i for i, e in enumerate(events) if e["type"] == "tool_result")
    late_at = next(i for i, e in enumerate(events)
                   if e.get("text") == "迟到的思考")
    think2_at = _index_of(events, tp.THINKING, start=exec_at)
    # 执行开始 → 工具返回 → （补发）迟到思考 → 下一轮思考开始
    assert exec_at < tool_at < late_at < think2_at, (exec_at, tool_at,
                                                     late_at, think2_at)


def _index_of(events, phase, start=0):
    for i, e in enumerate(events[start:], start):
        if e.get("type") == "phase" and e.get("phase") == phase:
            return i
    return -1
