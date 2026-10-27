# -*- coding: utf-8 -*-
"""
智能助理内核回归（1.0.8）。

覆盖：
    - 工具注册表：文件读写/查找/删除进回收站、越界风险升级、命令黑名单
    - 审批台：策略矩阵、批准/拒绝/超时、会话级免审
    - 技能包：frontmatter 解析、导出→删除→导入 往返
    - LangGraph 编排：plan→act→review→finish 全流程（用假模型）
    - Agent 循环：工具调用→结果回填→最终回答；文本内工具调用兜底
    - HTTP 层：/api/v1/assistant/state 与 /tools 可用
    - 引导会话队列（1.0.9）：入队/引导广播、多轮 FIFO 消费（唯一 end）、
      停止暂停 + resume 继续、队列 REST 端点错误码、删会话清队列

全部用例都在临时目录里跑，不碰用户真实数据。
"""
import base64
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import skills as skills_mod
from core.assistant import store as store_mod
from core.assistant.approval import ApprovalManager
from core.assistant.providers import ChatReply
from core.assistant.tools import ToolRegistry


# ----------------------------------------------------------------------------
# 夹具：把助理数据目录整体指到 tmp_path，并重置单例
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


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def test_fs_tools_roundtrip(env):
    reg = ToolRegistry(str(env["workspace"]))
    data = "第一行\n第二行\nneedle-here\n"
    r = reg.call("fs_write", {"path": "notes/a.txt", "content": data})
    assert r.ok, r.error

    r = reg.call("fs_read", {"path": "notes/a.txt"})
    assert r.ok and "needle-here" in r.content and "1|" in r.content

    r = reg.call("fs_glob", {"pattern": "**/*.txt"})
    assert r.ok and "notes/a.txt" in r.content

    r = reg.call("fs_grep", {"pattern": "needle"})
    assert r.ok and "a.txt:3" in r.content

    r = reg.call("fs_edit", {"path": "notes/a.txt", "old_text": "needle-here",
                             "new_text": "needle-replaced"})
    assert r.ok
    assert "needle-replaced" in reg.call("fs_read", {"path": "notes/a.txt"}).content

    # 未命中 -> 报错且不改文件
    r = reg.call("fs_edit", {"path": "notes/a.txt", "old_text": "not-exist",
                             "new_text": "x"})
    assert not r.ok and "未找到" in r.error


def test_outside_workspace_escalates_risk(env, tmp_path):
    reg = ToolRegistry(str(env["workspace"]))
    inside = reg.risk_of("fs_write", {"path": "a.txt", "content": "x"})
    outside = reg.risk_of("fs_write", {"path": str(tmp_path / "x.txt"), "content": "x"})
    assert inside == "medium"
    assert outside == "high", "越出工作区的写入必须升级为 high"


def test_delete_moves_to_trash(env):
    reg = ToolRegistry(str(env["workspace"]))
    reg.call("fs_write", {"path": "tmp.txt", "content": "x"})
    r = reg.call("fs_delete", {"path": "tmp.txt"})
    assert r.ok, r.error
    assert not os.path.exists(os.path.join(str(env["workspace"]), "tmp.txt"))
    trashed = r.data["trashed_to"]
    assert os.path.isfile(trashed)
    # 不允许删除工作区根
    assert not reg.call("fs_delete", {"path": "."}).ok


def test_tool_accepts_json_string_args(env):
    """编排器传进来的是 JSON 字符串参数，工具必须能自己解析（曾经直接 AttributeError）。"""
    reg = ToolRegistry(str(env["workspace"]))
    r = reg.call("fs_list", '{"path": ".", "recursive": false}')
    assert r.ok, r.error
    r2 = reg.call("fs_list", "not-json")
    assert not r2.ok and "JSON" in r2.error


def test_shell_denylist(env):
    reg = ToolRegistry(str(env["workspace"]))
    r = reg.call("shell_run", {"command": "format c: /q"})
    assert not r.ok and "安全策略拒绝" in r.error
    ok = reg.call("shell_run", {"command": "echo hello-buddy"})
    assert ok.ok and "hello-buddy" in ok.content


# ----------------------------------------------------------------------------
# 审批
# ----------------------------------------------------------------------------
def test_approval_policy_matrix():
    m = ApprovalManager()
    assert not m.needs_approval("readonly_auto", "low")
    assert m.needs_approval("readonly_auto", "medium")
    assert m.needs_approval("readonly_auto", "high")
    assert m.needs_approval("ask_all", "low")
    assert not m.needs_approval("yolo", "high")


def test_approval_approve_and_deny():
    m = ApprovalManager()
    out = {}

    def worker(kind):
        p = m.request("s1", "fs_delete", {"path": "x"}, "high", "删除 x",
                      policy="readonly_auto", timeout=5)
        out[kind] = p

    t = threading.Thread(target=worker, args=("approve",))
    t.start()
    for _ in range(50):
        pend = m.list_pending("s1")
        if pend:
            break
        time.sleep(0.05)
    assert pend, "未产生待审批项"
    m.resolve(pend[0]["id"], approve=True, remember=True)
    t.join(timeout=5)
    assert out["approve"].status == "approved"
    # remember=True 后，同会话同工具直接放行
    p2 = m.request("s1", "fs_delete", {"path": "y"}, "high", policy="readonly_auto", timeout=1)
    assert p2.status == "approved"

    out2 = {}

    def worker2():
        out2["p"] = m.request("s2", "shell_run", {"command": "dir"}, "high",
                              policy="readonly_auto", timeout=5)

    t2 = threading.Thread(target=worker2)
    t2.start()
    for _ in range(50):
        pend2 = m.list_pending("s2")
        if pend2:
            break
        time.sleep(0.05)
    m.resolve(pend2[0]["id"], approve=False, note="不允许")
    t2.join(timeout=5)
    assert out2["p"].status == "denied"


def test_approval_timeout():
    m = ApprovalManager()
    t0 = time.time()
    p = m.request("s3", "shell_run", {"command": "dir"}, "high",
                  policy="readonly_auto", timeout=0.3)
    assert p.status == "timeout"
    assert time.time() - t0 < 3


# ----------------------------------------------------------------------------
# 技能
# ----------------------------------------------------------------------------
def test_skill_parse(env):
    parsed = skills_mod.parse_skill_md(
        "---\nname: demo\ndescription: 演示\ntools: [fs_read, fs_write]\n---\n正文")
    assert parsed["meta"]["name"] == "demo"
    assert parsed["meta"]["tools"] == ["fs_read", "fs_write"]
    assert parsed["body"] == "正文"


def test_skill_export_import_roundtrip(env):
    items = skills_mod.list_skills()
    assert any(s["slug"] == "workspace-organizer" for s in items)
    blob = skills_mod.export_zip("repo-digest")
    assert blob[:2] == b"PK"
    assert skills_mod.delete_skill("repo-digest")
    assert not any(s["slug"] == "repo-digest" for s in skills_mod.list_skills())
    res = skills_mod.import_zip(blob)
    assert res["imported"] == ["repo-digest"]
    assert any(s["slug"] == "repo-digest" for s in skills_mod.list_skills())


def test_skill_import_rejects_garbage(env):
    with pytest.raises(skills_mod.SkillError):
        skills_mod.import_zip(b"not a zip at all")


# ----------------------------------------------------------------------------
# LangGraph 编排
# ----------------------------------------------------------------------------
class FakeProvider:
    """按队列返回预设回复的假模型；记录收到的消息便于断言。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def resolve_model(self, preferred=""):
        return preferred or "fake-model"

    def chat(self, messages, tools=None, model="", temperature=0.3,
             max_tokens=2048, on_delta=None, cancel_check=None, timeout=300):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.replies:
            return ChatReply(content="完成")
        r = self.replies.pop(0)
        if callable(r):
            r = r(messages, tools)
        if on_delta and r.content:
            for i in range(0, len(r.content), 16):
                on_delta("content", r.content[i:i + 16])
        return r


def test_orchestrator_langgraph(env):
    from core.assistant.orchestrator import run_skill
    provider = FakeProvider([
        ChatReply(content='["列出文件", "汇总"]'),
        ChatReply(content="", tool_calls=[{"id": "c1", "name": "fs_list",
                                          "arguments": {"path": "."}}]),
        ChatReply(content="已列出工作区"),
        ChatReply(content="DONE"),
        ChatReply(content="## 结果\n工作区为空。"),
    ])
    reg = ToolRegistry(str(env["workspace"]))
    events = []
    skill = skills_mod.load_skill("workspace-organizer")
    out = run_skill(skill, "整理工作区", {
        "registry": reg, "provider": provider, "emit": events.append,
        "exec_tool": lambda n, a, source="skill": reg.call(n, a, {}).ok and "ok" or "fail",
        "depth": 1,
    })
    assert out["engine"] == "langgraph", out
    assert out["ok"] and "结果" in out["answer"]
    phases = [e["phase"] for e in events if e.get("type") == "skill_step"]
    assert phases[0] == "plan"
    assert "act" in phases and "finish" in phases
    # 顺序必须是 plan → act → (review) → finish
    assert phases.index("act") < phases.index("finish")


def test_orchestrator_reports_failure_instead_of_fabricating(env):
    """所有工具都失败时（限流/断网），不能让模型编一份结果出来。"""
    from core.assistant.orchestrator import run_skill
    provider = FakeProvider([
        ChatReply(content='["列目录"]'),
        ChatReply(content="", tool_calls=[{"id": "c1", "name": "fs_list",
                                          "arguments": {"path": "."}}]),
        ChatReply(content="本应被跳过"),
    ])
    skill = skills_mod.load_skill("repo-digest")
    out = run_skill(skill, "体检", {
        "registry": ToolRegistry(str(env["workspace"])), "provider": provider,
        "emit": lambda e: None,
        "exec_tool": lambda n, a, source="skill": (False, "错误：rate limit"),
        "depth": 1,
    })
    assert out["ok"] is False
    assert "没有执行成功" in out["answer"]
    assert "rate limit" in out["error"]


def test_orchestrator_respects_tool_whitelist(env):
    from core.assistant.orchestrator import _filter_tools
    reg = ToolRegistry(str(env["workspace"]))
    names = _filter_tools(reg, ["fs_read"])
    assert names == ["fs_read"]
    assert "shell_run" not in names


# ----------------------------------------------------------------------------
# Agent 循环
# ----------------------------------------------------------------------------
def test_agent_turn_runs_tool_and_reports(env, monkeypatch):
    from core.assistant import agent as agent_mod
    provider = FakeProvider([
        ChatReply(content="", tool_calls=[{"id": "c1", "name": "fs_write",
                                          "arguments": {"path": "out/hello.txt",
                                                        "content": "hi-buddy"}}]),
        ChatReply(content="已写入 out/hello.txt"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "在工作区写一个 hello.txt", emit=events.append)
    assert res["ok"], res
    assert res["content"] == "已写入 out/hello.txt"
    assert os.path.isfile(os.path.join(str(env["workspace"]), "out", "hello.txt"))
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds and "done" in kinds
    # 会话落盘
    saved = store_mod.load_session(session["id"])
    assert len(saved["messages"]) == 2
    assert saved["title"].startswith("在工作区")


def test_agent_tool_rounds_unlimited_by_default(env, monkeypatch):
    """默认 max_tool_rounds=0：超过旧 12 次上限也不中止（用户要求工具调用不限次）。

    旧实现写死 MAX_TOOL_ROUNDS=12，多步任务（列目录→取文件→安装→验证）
    会被中途掐断；现在 0=不限制，兜底靠 900s 墙钟 + 用户停止。
    """
    from core.assistant import agent as agent_mod
    replies = [ChatReply(content="", tool_calls=[{"id": f"c{i}", "name": "fs_list",
                                                  "arguments": {"path": "."}}])
               for i in range(20)]                       # 20 轮，旧的 12 上限必杀
    replies.append(ChatReply(content="列了 20 次，收工"))
    provider = FakeProvider(replies)
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})            # max_tool_rounds 缺省 → 0

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "反复列目录", emit=events.append)
    assert res["ok"], res
    assert res["content"] == "列了 20 次，收工"
    assert not any("上限" in str(e.get("message") or "") for e in events)
    assert len(provider.calls) == 21                     # 20 轮工具 + 1 轮收尾


def test_agent_tool_rounds_positive_cap_still_enforced(env, monkeypatch):
    """设了正数上限就仍然生效——不搞一刀切，配置可调。"""
    from core.assistant import agent as agent_mod
    replies = [ChatReply(content="", tool_calls=[{"id": f"c{i}", "name": "fs_list",
                                                  "arguments": {"path": "."}}])
               for i in range(10)]                       # 永远调工具，不会自己收尾
    provider = FakeProvider(replies)
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo", "max_tool_rounds": 3})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "反复列目录", emit=events.append)
    assert not res["ok"]
    assert any("上限（3 次）" in str(e.get("message") or "") for e in events)
    assert len(provider.calls) == 3


def test_agent_approval_denied_path(env, monkeypatch):
    from core.assistant import agent as agent_mod
    provider = FakeProvider([
        ChatReply(content="", tool_calls=[{"id": "c1", "name": "fs_write",
                                          "arguments": {"path": "deny.txt",
                                                        "content": "x"}}]),
        ChatReply(content="好的，已取消写入"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "readonly_auto", "approval_timeout": 5})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []

    def auto_deny():
        from core.assistant.approval import get_approval_manager
        mgr = get_approval_manager()
        for _ in range(100):
            pend = mgr.list_pending(session["id"])
            if pend:
                mgr.resolve(pend[0]["id"], approve=False)
                return
            time.sleep(0.05)

    threading.Thread(target=auto_deny, daemon=True).start()
    res = ass.start_turn(session["id"], "写个文件", emit=events.append)
    assert res["ok"]
    assert not os.path.exists(os.path.join(str(env["workspace"]), "deny.txt")), \
        "审批被拒后不应落盘"
    assert any(e["type"] == "approval_request" for e in events)


def test_agent_runs_multiple_tool_calls_in_parallel(env, monkeypatch):
    """一轮返回多个 tool_calls 时全部执行，tool 消息按调用顺序回填。"""
    from core.assistant import agent as agent_mod
    provider = FakeProvider([
        ChatReply(content="", tool_calls=[
            {"id": "c1", "name": "fs_write",
             "arguments": {"path": "a.txt", "content": "A"}},
            {"id": "c2", "name": "fs_write",
             "arguments": {"path": "b.txt", "content": "B"}},
            {"id": "c3", "name": "fs_write",
             "arguments": {"path": "c.txt", "content": "C"}},
        ]),
        ChatReply(content="已写完三个文件"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "写三个文件", emit=events.append)
    assert res["ok"], res
    # 三个工具都真正落地
    for name in ("a.txt", "b.txt", "c.txt"):
        p = os.path.join(str(env["workspace"]), name)
        assert os.path.isfile(p), p
    # tool_call / tool_result 事件齐全，且各一次、带正确序号
    tcalls = [e for e in events if e["type"] == "tool_call"]
    tresults = [e for e in events if e["type"] == "tool_result"]
    assert len(tcalls) == 3 and len(tresults) == 3
    # 三个调用都发出，且各自带正确序号（并行下事件顺序不保证，用集合比对）
    assert {e.get("index") for e in tcalls} == {0, 1, 2}
    assert all(e["ok"] for e in tresults)
    # tool 消息按调用顺序回填到下一轮模型上下文（call_0/1/2），不乱序
    ctx = provider.calls[-1]["messages"]
    tool_msgs = [m for m in ctx if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2", "c3"]


def test_text_tool_call_fallback(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    content = ('我先看一下目录。\n```tool_call\n'
               '{"name": "fs_list", "arguments": {"path": "."}}\n```')
    calls, cleaned, _bad = ass._parse_text_calls(content, known={"fs_list"})
    assert len(calls) == 1 and calls[0]["name"] == "fs_list"
    assert "tool_call" not in cleaned
    assert ass._parse_text_calls("普通回答，没有调用")[0] == []


def test_text_tool_call_bare_name(env):
    """`<tool_call>skill_list</tool_call>`：只有工具名、没有参数的老实写法。

    用户截图里的报错就是这个：解析不出来 → 被当成「只输出了思考内容」，
    然后把标记原样贴进气泡。这里必须识别成一次真实调用。
    """
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    known = {"skill_list", "fs_list"}
    for content in ("<tool_call>skill_list</tool_call>",
                    "<tool_call> skill_list() </tool_call>",
                    "```tool_call\nskill_list\n```"):
        calls, cleaned, bad = ass._parse_text_calls(content, known=known)
        assert [c["name"] for c in calls] == ["skill_list"], content
        assert calls[0]["arguments"] == {}
        assert cleaned == "" and bad == []


def test_text_tool_call_bare_name_with_args(env):
    """`<tool_call>fs_list {"path": "."}</tool_call>`：名字 + 裸 JSON 参数。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    calls, cleaned, _bad = ass._parse_text_calls(
        '<tool_call>fs_list {"path": "."}</tool_call>', known={"fs_list"})
    assert calls[0]["name"] == "fs_list"
    assert calls[0]["arguments"] == {"path": "."}
    assert cleaned == ""


def test_text_tool_call_unknown_name_not_executed(env):
    """幻觉出来的工具名：不当成调用执行，但标记必须从正文里摘掉。

    否则用户看到的就是一行 `<tool_call>skill_lst</tool_call>` 被当回答。
    """
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    known = {"skill_list"}
    calls, cleaned, bad = ass._parse_text_calls(
        '<tool_call>skill_lst</tool_call>\n我以为这样就能列出技能', known=known)
    assert calls == []
    assert bad == ["skill_lst"]
    assert "tool_call" not in cleaned
    assert "我以为这样就能列出技能" in cleaned


def test_turn_executes_bare_name_switch_call(env, monkeypatch):
    """整轮：模型正文就是 `<tool_call>skill_list</tool_call>` → 真执行工具再答。

    1.0.11 用户报的错（气泡里贴一行 `<tool_call>skill_list</tool_call>` 并
    提示「只输出了思考内容」）就是这条链路断了：解析不出来 → 当成正文。
    """
    from core.assistant import agent as agent_mod
    provider = FakeProvider([
        ChatReply(content="<tool_call>skill_list</tool_call>"),
        ChatReply(content="本机装了 3 个技能。"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "你有哪些技能", emit=events.append)
    assert res["ok"], res
    assert res["content"] == "本机装了 3 个技能。"
    assert any(e["type"] == "tool_call" and e["name"] == "skill_list"
               for e in events)
    # 一个 error 都不许有：这轮本来就是能跑的
    assert not [e for e in events if e["type"] == "error"]


def test_turn_cleans_tool_markup_from_reasoning(env, monkeypatch):
    """思考里混进工具标记：思考正文也要清干净，不留 <tool_call> 原文。

    本地 qwen3 这类思考型模型经常把工具调用写在「思考」里；正文清干净了
    但思考块还留着原文，用户在界面上仍会看到那一行标记。
    """
    from core.assistant import agent as agent_mod
    provider = FakeProvider([
        ChatReply(content="<tool_call>skill_list</tool_call>",
                  reasoning="我先查一下技能 <tool_call>skill_list</tool_call>"),
        ChatReply(content="本机装了 3 个技能。"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "你有哪些技能", emit=events.append)
    assert res["ok"], res
    reps = [e for e in events if e["type"] == "reasoning_replace"]
    assert reps, events
    assert "tool_call" not in reps[0]["text"]
    assert "我先查一下技能" in reps[0]["text"]


def test_turn_reports_unknown_tool_instead_of_dumping_markup(env, monkeypatch):
    """模型调了一个本机没有的工具：报清楚的错误，绝不把标记当回答显示。"""
    from core.assistant import agent as agent_mod
    provider = FakeProvider([ChatReply(content="<tool_call>write_file</tool_call>")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"policy": "yolo"})

    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    res = ass.start_turn(session["id"], "写个文件", emit=events.append)
    assert not res["ok"]
    assert "write_file" in res["error"]
    assert "function calling" in res["error"]
    shown = "".join(e.get("content") or "" for e in events
                    if e["type"] in ("done", "replace"))
    assert "<tool_call>" not in shown, shown


def test_skill_run_tool_allows_one_level(env):
    """Agent 直接调 skill_run 必须能跑（曾因 depth 算成 1 被误判为「嵌套过深」）。"""
    provider = FakeProvider([
        ChatReply(content='["看文件"]'),
        ChatReply(content="已看过"),
        ChatReply(content="摘要：空工作区"),
    ])
    reg = ToolRegistry(str(env["workspace"]))
    ctx = {"registry": reg, "provider": provider, "emit": lambda e: None,
           "exec_tool": lambda n, a, source="skill": (True, "ok"), "depth": 0}
    r = reg.call("skill_run", {"name": "repo-digest", "task": "体检"}, ctx)
    assert "层级过深" not in (r.error or ""), r.error
    assert r.ok and "摘要" in r.content


def test_skill_depth_guard_blocks_infinite_recursion(env):
    from core.assistant.orchestrator import run_skill
    skill = skills_mod.load_skill("repo-digest")
    out = run_skill(skill, "x", {"registry": ToolRegistry(str(env["workspace"])),
                                 "provider": FakeProvider([]), "depth": 2})
    assert not out["ok"] and "层级过深" in out["error"]


# ----------------------------------------------------------------------------
# 任务 / 空间（1.0.8：会话改叫「任务」，按工作区目录分组成「空间」）
# ----------------------------------------------------------------------------
def test_new_task_belongs_to_current_workspace(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    s = ass.new_session()
    assert s["title"] == "新任务"
    assert store_mod.norm_workspace(s["workspace"]) == \
        store_mod.norm_workspace(str(env["workspace"]))
    assert s["pinned"] is False and s["archived"] is False


def test_rename_pin_archive_task(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    s = ass.new_session()
    ass.update_session(s["id"], {"title": "  月度报表  ", "pinned": True})
    got = store_mod.load_session(s["id"])
    assert got["title"] == "月度报表" and got["pinned"] is True
    # 归档后默认列表不出现，include_archived 才给
    ass.update_session(s["id"], {"archived": True})
    assert s["id"] not in [x["id"] for x in ass.session_list()]
    assert s["id"] in [x["id"] for x in ass.session_list(include_archived=True)]


def test_update_task_does_not_bump_updated_at(env):
    """重命名/置顶不该把任务顶到「最近活动」最前面（与 WorkBuddy 一致）。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    s = ass.new_session()
    before = store_mod.load_session(s["id"])["updated_at"]
    time.sleep(0.02)
    ass.update_session(s["id"], {"title": "改名", "pinned": True})
    assert store_mod.load_session(s["id"])["updated_at"] == before


def test_pinned_task_sorts_first(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    old = ass.new_session("旧")
    store_mod.save_session({**store_mod.load_session(old["id"]),
                            "updated_at": 100.0})
    pinned = ass.new_session("置顶")
    store_mod.save_session({**store_mod.load_session(pinned["id"]),
                            "updated_at": 50.0, "pinned": True})
    rows = ass.session_list()
    assert rows[0]["id"] == pinned["id"]


def test_space_list_groups_tasks_by_workspace(env, tmp_path):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    a = ass.new_session("甲", str(ws1))
    b = ass.new_session("乙", str(tmp_path / "ws2"))
    ass.update_session(b["id"], {"pinned": True})
    spaces = {sp["name"]: sp for sp in ass.space_list()}
    assert {"ws1", "ws2"} <= set(spaces)
    assert spaces["ws1"]["count"] == 1
    assert spaces["ws2"]["tasks"][0]["id"] == b["id"]      # 置顶排组内第一
    assert spaces["ws1"]["exists"] is True
    assert a["id"] in [t["id"] for t in spaces["ws1"]["tasks"]]


def test_space_path_normalized(env, tmp_path):
    """E:////x 与 E:/x/ 是同一个空间，不能拆成两条同名目录。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    ws = tmp_path / "merged"
    ws.mkdir()
    alt = str(ws).replace("\\", "/") + "/"
    ass.new_session("一", str(ws))
    ass.new_session("二", alt)
    groups = [sp for sp in ass.space_list() if sp["name"] == "merged"]
    assert len(groups) == 1 and groups[0]["count"] == 2


def test_legacy_task_without_workspace_falls_back(env):
    """1.0.8 之前的老会话没有 workspace 字段，归到当前工作区而不是单列一组。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    store_mod.save_session({"id": "sold", "title": "老任务", "created_at": 1,
                            "updated_at": 1, "messages": []})
    spaces = ass.space_list()
    assert "（未归属）" not in [sp["name"] for sp in spaces]
    hit = [t for sp in spaces for t in sp["tasks"] if t["id"] == "sold"]
    assert hit and hit[0]["workspace"] == \
        store_mod.norm_workspace(str(env["workspace"]))


def test_space_missing_dir_flagged(env, tmp_path):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    gone = tmp_path / "gone"
    ass.new_session("x", str(gone))         # 不创建目录
    sp = [s for s in ass.space_list() if s["name"] == "gone"]
    assert sp and sp[0]["exists"] is False


def test_http_task_and_space_endpoints(env):
    import asyncio
    from routers import assistant as api

    created = asyncio.run(api.create_session({"title": "接口任务"}))
    assert created["code"] == 0
    sid = created["data"]["id"]

    patch = asyncio.run(api.session_update(sid, {"pinned": True,
                                                 "title": "接口任务改名"}))
    assert patch["code"] == 0 and patch["data"]["pinned"] is True

    spaces = asyncio.run(api.spaces())
    assert spaces["code"] == 0
    assert any(t["id"] == sid for sp in spaces["data"] for t in sp["tasks"])

    state = asyncio.run(api.state())
    assert any(s["id"] == sid for s in state["data"]["sessions"])

    missing = asyncio.run(api.session_update("nope", {"pinned": True}))
    assert missing["code"] != 0


# ----------------------------------------------------------------------------
# 任务 / 空间（1.0.8：会话改叫「任务」，按工作区目录分组成「空间」）
# ----------------------------------------------------------------------------
def test_new_task_belongs_to_current_workspace(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    s = ass.new_session()
    assert s["title"] == "新任务"
    assert store_mod.norm_workspace(s["workspace"]) ==         store_mod.norm_workspace(str(env["workspace"]))
    assert s["pinned"] is False and s["archived"] is False


def test_rename_pin_archive_task(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    s = ass.new_session()
    ass.update_session(s["id"], {"title": "  月度报表  ", "pinned": True})
    got = store_mod.load_session(s["id"])
    assert got["title"] == "月度报表" and got["pinned"] is True
    # 归档后默认列表不出现，include_archived 才给
    ass.update_session(s["id"], {"archived": True})
    assert s["id"] not in [x["id"] for x in ass.session_list()]
    assert s["id"] in [x["id"] for x in ass.session_list(include_archived=True)]


def test_update_task_does_not_bump_updated_at(env):
    """重命名/置顶不该把任务顶到「最近活动」最前面（与 WorkBuddy 一致）。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    s = ass.new_session()
    before = store_mod.load_session(s["id"])["updated_at"]
    time.sleep(0.02)
    ass.update_session(s["id"], {"title": "改名", "pinned": True})
    assert store_mod.load_session(s["id"])["updated_at"] == before


def test_pinned_task_sorts_first(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    old = ass.new_session("旧")
    store_mod.save_session({**store_mod.load_session(old["id"]),
                            "updated_at": 100.0})
    pinned = ass.new_session("置顶")
    store_mod.save_session({**store_mod.load_session(pinned["id"]),
                            "updated_at": 50.0, "pinned": True})
    rows = ass.session_list()
    assert rows[0]["id"] == pinned["id"]


def test_space_list_groups_tasks_by_workspace(env, tmp_path):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    ws1, ws2 = tmp_path / "ws1", tmp_path / "ws2"
    ws1.mkdir()
    ws2.mkdir()
    a = ass.new_session("甲", str(ws1))
    b = ass.new_session("乙", str(tmp_path / "ws2"))
    ass.update_session(b["id"], {"pinned": True})
    spaces = {sp["name"]: sp for sp in ass.space_list()}
    assert {"ws1", "ws2"} <= set(spaces)
    assert spaces["ws1"]["count"] == 1
    assert spaces["ws2"]["tasks"][0]["id"] == b["id"]      # 置顶排组内第一
    assert spaces["ws1"]["exists"] is True
    assert a["id"] in [t["id"] for t in spaces["ws1"]["tasks"]]


def test_space_path_normalized(env, tmp_path):
    """反斜杠写法与正斜杠写法属于同一个空间，不能拆成两条同名目录。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    ws = tmp_path / "merged"
    ws.mkdir()
    alt = str(ws).replace("\\", "/") + "/"
    ass.new_session("一", str(ws))
    ass.new_session("二", alt)
    groups = [sp for sp in ass.space_list() if sp["name"] == "merged"]
    assert len(groups) == 1 and groups[0]["count"] == 2


def test_legacy_task_without_workspace_falls_back(env):
    """1.0.8 之前的老会话没有 workspace 字段，归到当前工作区而不是单列一组。"""
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    store_mod.save_session({"id": "sold", "title": "老任务", "created_at": 1,
                            "updated_at": 1, "messages": []})
    spaces = ass.space_list()
    assert "（未归属）" not in [sp["name"] for sp in spaces]
    hit = [t for sp in spaces for t in sp["tasks"] if t["id"] == "sold"]
    assert hit and hit[0]["workspace"] ==         store_mod.norm_workspace(str(env["workspace"]))


def test_space_missing_dir_flagged(env, tmp_path):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    gone = tmp_path / "gone"
    ass.new_session("x", str(gone))         # 不创建目录
    sp = [s for s in ass.space_list() if s["name"] == "gone"]
    assert sp and sp[0]["exists"] is False


def test_http_task_and_space_endpoints(env):
    import asyncio
    from routers import assistant as api

    created = asyncio.run(api.create_session({"title": "接口任务"}))
    assert created["code"] == 0
    sid = created["data"]["id"]

    patch = asyncio.run(api.session_update(sid, {"pinned": True,
                                                 "title": "接口任务改名"}))
    assert patch["code"] == 0 and patch["data"]["pinned"] is True

    spaces = asyncio.run(api.spaces())
    assert spaces["code"] == 0
    assert any(t["id"] == sid for sp in spaces["data"] for t in sp["tasks"])

    state = asyncio.run(api.state())
    assert any(s["id"] == sid for s in state["data"]["sessions"])

    missing = asyncio.run(api.session_update("nope", {"pinned": True}))
    assert missing["code"] != 0


# ----------------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------------
def test_http_state_and_tools(env):
    """不依赖 TestClient（本机 starlette/httpx 版本不兼容），直接调用异步端点。"""
    import asyncio
    import serve
    from routers import assistant as api

    paths = [getattr(r, "path", "") for r in serve.app.routes]
    assert "/api/v1/assistant/state" in paths
    assert "/api/v1/assistant/chat" in paths

    body = asyncio.run(api.state())
    assert body["code"] == 0
    data = body["data"]
    assert data["version"].count(".") == 2      # 别硬编码版本号：每次发版都要改测试
    assert data["orchestrator"] in ("langgraph", "sequential")
    assert any(t["name"] == "fs_read" for t in data["tools"])

    skills_body = asyncio.run(api.skill_list())
    assert skills_body["code"] == 0 and skills_body["data"]


# ----------------------------------------------------------------------------
# 引导会话队列（1.0.9 增量）：排队插话 / 引导发送 / 自动消费 / 停止暂停
# ----------------------------------------------------------------------------
def _parse_sse(chunks):
    """把 StreamingResponse 的 data: 行解析成事件列表 + 是否见到 [DONE]。"""
    events, done = [], False
    for chunk in chunks:
        for line in str(chunk).splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if raw == "[DONE]":
                done = True
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            if isinstance(ev, dict):
                events.append(ev)
    return events, done


def _fresh_sid(ass):
    sid = ass.new_session()["id"]
    from core.assistant.queue import get_queue
    get_queue().drop_session(sid)
    return sid


def test_chat_queue_mode_enqueues_and_broadcasts(env):
    """忙碌态入队：REST 短请求不新开流，假 sink 依次收到 queued、queue。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import get_queue
    from routers import assistant as api

    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    sink: "asyncio.Queue" = asyncio.Queue()
    api._STREAM_SINKS[sid] = sink
    try:
        res = asyncio.run(api.chat({"session_id": sid, "message": "插一句",
                                    "mode": "queue", "stream": False}))
        assert res["code"] == 0
        item = res["data"]["queued"]
        assert item["text"] == "插一句" and item["guided"] is False
        assert res["data"]["paused"] is False
        ev1 = sink.get_nowait()
        ev2 = sink.get_nowait()
        assert ev1["type"] == "queued" and ev1["text"] == "插一句"
        assert ev1["index"] == 0 and len(ev1["time"]) == 5
        assert ev2["type"] == "queue"
        assert [i["id"] for i in ev2["items"]] == [item["id"]]
        assert q.pending_count(sid) == 1
    finally:
        api._STREAM_SINKS.pop(sid, None)
        q.drop_session(sid)


def test_chat_guided_mode_broadcasts_guided_and_queue(env):
    """引导发送：立即以 guided 事件登记（客户端渲染气泡），不打断当前轮。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import get_queue
    from routers import assistant as api

    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    sink: "asyncio.Queue" = asyncio.Queue()
    api._STREAM_SINKS[sid] = sink
    try:
        res = asyncio.run(api.chat({"session_id": sid, "message": "立即引导",
                                    "mode": "guided", "stream": False}))
        assert res["code"] == 0
        item = res["data"]["queued"]
        assert item["guided"] is True
        types = [sink.get_nowait()["type"] for _ in range(3)]
        assert types == ["queued", "guided", "queue"]
        assert q.snapshot(sid)[0]["guided"] is True
    finally:
        api._STREAM_SINKS.pop(sid, None)
        q.drop_session(sid)


def test_chat_queue_mode_full_returns_2012(env):
    """队列满 20 条：第 21 条入队被拒（2012），消息保留在输入框不丢。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import MAX_QUEUE_ITEMS, get_queue
    from routers import assistant as api

    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    try:
        for i in range(MAX_QUEUE_ITEMS):
            q.enqueue(sid, f"m{i}")
        api._STREAM_SINKS[sid] = asyncio.Queue()
        try:
            res = asyncio.run(api.chat({"session_id": sid, "message": "挤不进去",
                                        "mode": "queue", "stream": False}))
            assert res["code"] == 2012
        finally:
            api._STREAM_SINKS.pop(sid, None)
        assert q.pending_count(sid) == MAX_QUEUE_ITEMS
    finally:
        q.drop_session(sid)


def test_chat_queue_mode_without_stream_degrades_to_normal(env, monkeypatch):
    """无活跃流时 queue/guided 退化为普通发送：不报错、不丢消息。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import get_queue
    from routers import assistant as api

    provider = FakeProvider([ChatReply(content="直接开跑的答复")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    try:
        async def run():
            resp = await api.chat({"session_id": sid, "message": "没流就正常发",
                                   "mode": "queue", "stream": True})
            return [c async for c in resp.body_iterator]

        chunks = asyncio.run(run())
        events, done = _parse_sse(chunks)
        kinds = [e.get("type") for e in events]
        assert "done" in kinds and kinds[-1] == "end" and done is True
        assert not any(e.get("type") == "queued" for e in events)
        assert q.pending_count(sid) == 0
        saved = store_mod.load_session(sid)
        assert any(m["role"] == "user" and m["content"] == "没流就正常发"
                   for m in saved["messages"])
    finally:
        q.drop_session(sid)


def test_chat_stream_consumes_queue_fifo(env, monkeypatch):
    """本轮结束自动按 FIFO 消费队列：同一条连接续跑，唯一一次 end 在末尾。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import get_queue
    from routers import assistant as api

    provider = FakeProvider([
        ChatReply(content="第一轮答复"),
        ChatReply(content="第二轮答复"),
        ChatReply(content="第三轮答复"),
    ])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)

    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    i1 = q.enqueue(sid, "队列消息一")
    i2 = q.enqueue(sid, "队列消息二")

    async def run():
        resp = await api.chat({"session_id": sid, "message": "首条消息",
                               "stream": True})
        return [c async for c in resp.body_iterator]

    try:
        chunks = asyncio.run(run())
        events, done = _parse_sse(chunks)
        kinds = [e.get("type") for e in events]
        # 恰好一次 end、在末尾，终止于 [DONE]（单轮 end 被吞掉）
        assert kinds.count("end") == 1 and kinds[-1] == "end" and done is True
        end_ev = events[-1]
        assert end_ev["content"] == "第三轮答复" and end_ev["error"] == ""
        assert end_ev["paused"] is False
        # 3 个 done（每轮一个）、2 个 dequeued（id/text/mode 正确）
        assert kinds.count("done") == 3
        dq = [e for e in events if e.get("type") == "dequeued"]
        assert [e["id"] for e in dq] == [i1.id, i2.id]
        assert [e["text"] for e in dq] == ["队列消息一", "队列消息二"]
        assert [e["mode"] for e in dq] == ["queue", "queue"]
        # 每个 dequeued 后跟一次快照；末尾快照为空（队列耗尽）
        snaps = [e for e in events if e.get("type") == "queue"]
        assert len(snaps) == 3 and snaps[-1]["items"] == []
        # FIFO 落库：3 user + 3 assistant，顺序与到达一致
        saved = store_mod.load_session(sid)
        users = [m["content"] for m in saved["messages"] if m["role"] == "user"]
        bots = [m for m in saved["messages"] if m["role"] == "assistant"]
        assert users == ["首条消息", "队列消息一", "队列消息二"]
        assert len(bots) == 3
        # 流结束后 sink 注销（resume 才不撞 2017）
        assert sid not in api._STREAM_SINKS
    finally:
        q.drop_session(sid)


def test_stop_pauses_queue_and_resume_continues(env, monkeypatch):
    """/stop = 取消 + 队列暂停（不出队不清空）；resume 解暂停并出队头条。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import get_queue
    from routers import assistant as api

    provider = FakeProvider([ChatReply(content="恢复后的答复")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    q.enqueue(sid, "排队消息")

    sink: "asyncio.Queue" = asyncio.Queue()
    api._STREAM_SINKS[sid] = sink
    try:
        res = asyncio.run(api.stop({"session_id": sid}))
        assert res["code"] == 0 and res["data"]["stopped"] is True
        assert q.is_paused(sid) is True
        ev1 = sink.get_nowait()
        ev2 = sink.get_nowait()
        assert ev1["type"] == "queue_state" and ev1["paused"] is True
        assert ev1["reason"] == "stopped"
        assert ev2["type"] == "queue" and len(ev2["items"]) == 1
        assert q.pending_count(sid) == 1        # 不出队、不清空
    finally:
        api._STREAM_SINKS.pop(sid, None)

    # resume 空队列 -> HTTP 400 + fail(2013)（唯一 HTTP 非 200）
    sid2 = _fresh_sid(ass)
    try:
        resp = asyncio.run(api.chat({"session_id": sid2, "mode": "resume",
                                     "stream": True}))
        assert getattr(resp, "status_code", None) == 400
        assert json.loads(resp.body)["code"] == 2013

        # resume 撞活跃流 -> 2017（竞态兜底）
        api._STREAM_SINKS[sid] = asyncio.Queue()
        try:
            busy = asyncio.run(api.chat({"session_id": sid, "mode": "resume"}))
            assert busy["code"] == 2017
        finally:
            api._STREAM_SINKS.pop(sid, None)

        # resume 非空：解暂停 + 出队头条开轮，回到自动消费路径
        async def run():
            r = await api.chat({"session_id": sid, "mode": "resume",
                                "stream": True})
            return [c async for c in r.body_iterator]

        chunks = asyncio.run(run())
        events, done = _parse_sse(chunks)
        kinds = [e.get("type") for e in events]
        assert kinds[0] == "session"
        assert "queue_state" in kinds and q.is_paused(sid) is False
        dq = [e for e in events if e.get("type") == "dequeued"]
        assert len(dq) == 1 and dq[0]["text"] == "排队消息"
        assert kinds[-1] == "end" and done is True
        assert q.pending_count(sid) == 0
    finally:
        q.drop_session(sid)
        q.drop_session(sid2)


def test_queue_endpoints_crud_and_error_codes(env):
    """队列 REST 端点：快照/编辑/排序/删除/清除 + 2002/2012/2014/2015/2016。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import MAX_QUEUE_ITEMS, get_queue
    from routers import assistant as api

    ass = agent_mod.get_assistant()
    sid = _fresh_sid(ass)
    q = get_queue()
    try:
        a = q.enqueue(sid, "甲")
        b = q.enqueue(sid, "乙")
        g = q.enqueue(sid, "引导", guided=True)

        # GET 快照（FIFO 顺序 + paused）
        snap = asyncio.run(api.queue_snapshot(sid))
        assert snap["code"] == 0
        assert [i["id"] for i in snap["data"]["items"]] == [a.id, b.id, g.id]
        assert snap["data"]["paused"] is False
        assert asyncio.run(api.queue_snapshot(""))["code"] == 2002

        # PATCH 编辑 + 错误码
        upd = asyncio.run(api.queue_update(a.id, sid, {"text": "甲改"}))
        assert upd["code"] == 0 and upd["data"]["text"] == "甲改"
        assert upd["data"]["edited_at"] is not None
        assert asyncio.run(api.queue_update(g.id, sid, {"text": "x"}))["code"] == 2016
        assert asyncio.run(api.queue_update("qnope", sid, {"text": "x"}))["code"] == 2014
        assert asyncio.run(api.queue_update(a.id, sid, {"text": "  "}))["code"] == 2002

        # move 排序 + 错误码（乙上移一位；首条再上移越界）
        mv = asyncio.run(api.queue_move(b.id, sid, {"delta": -1}))
        assert mv["code"] == 0
        assert [i["text"] for i in mv["data"]["items"]] == ["乙", "甲改", "引导"]
        assert asyncio.run(api.queue_move(g.id, sid, {"delta": -1}))["code"] == 2016
        assert asyncio.run(api.queue_move(b.id, sid, {"delta": -1}))["code"] == 2002
        assert asyncio.run(api.queue_move(b.id, sid, {"delta": 7}))["code"] == 2002

        # DELETE 删除 + 错误码
        dele = asyncio.run(api.queue_delete(a.id, sid))
        assert dele["code"] == 0 and dele["data"]["deleted"] == a.id
        assert asyncio.run(api.queue_delete(g.id, sid))["code"] == 2016
        assert asyncio.run(api.queue_delete("qnope", sid))["code"] == 2014

        # clear 只清非引导（此时队列：乙、引导 -> 清 1 条）
        cl = asyncio.run(api.queue_clear(sid))
        assert cl["code"] == 0 and cl["data"]["cleared"] == 1
        assert [i["text"] for i in q.snapshot(sid)] == ["引导"]

        # 已出队 -> 2015
        popped = q.pop_next(sid)
        late = asyncio.run(api.queue_update(popped.id, sid, {"text": "迟到的编辑"}))
        assert late["code"] == 2015

        # 队列满 20 的 REST 路径（再塞 20 条撞上限）
        for i in range(MAX_QUEUE_ITEMS):
            q.enqueue(sid, f"f{i}")
        api._STREAM_SINKS[sid] = asyncio.Queue()
        try:
            full = asyncio.run(api.chat({"session_id": sid, "message": "第21条",
                                         "mode": "queue", "stream": False}))
            assert full["code"] == 2012
        finally:
            api._STREAM_SINKS.pop(sid, None)
    finally:
        q.drop_session(sid)


def test_delete_session_drops_queue(env):
    """删任务时队列状态（条目/暂停/已出队记忆）一起清掉。"""
    import asyncio
    from core.assistant import agent as agent_mod
    from core.assistant.queue import get_queue
    from routers import assistant as api

    ass = agent_mod.get_assistant()
    sid = ass.new_session()["id"]
    q = get_queue()
    q.drop_session(sid)
    q.enqueue(sid, "跟着会话一起走")
    q.set_paused(sid, True)
    res = asyncio.run(api.session_delete(sid))
    assert res["code"] == 0 and res["data"]["deleted"] is True
    assert q.pending_count(sid) == 0
    assert q.is_paused(sid) is False
