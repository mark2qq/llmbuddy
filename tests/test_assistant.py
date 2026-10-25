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


def test_text_tool_call_fallback(env):
    from core.assistant import agent as agent_mod
    ass = agent_mod.get_assistant()
    content = ('我先看一下目录。\n```tool_call\n'
               '{"name": "fs_list", "arguments": {"path": "."}}\n```')
    calls, cleaned = ass._parse_text_calls(content)
    assert len(calls) == 1 and calls[0]["name"] == "fs_list"
    assert "tool_call" not in cleaned
    assert ass._parse_text_calls("普通回答，没有调用")[0] == []


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
    assert data["version"] == "1.0.8"
    assert data["orchestrator"] in ("langgraph", "sequential")
    assert any(t["name"] == "fs_read" for t in data["tools"])

    skills_body = asyncio.run(api.skill_list())
    assert skills_body["code"] == 0 and skills_body["data"]
