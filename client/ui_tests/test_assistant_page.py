# -*- coding: utf-8 -*-
"""
智能助理页面（1.0.8）无头烟雾测试。

验证点：
    - 页面可构造、导航项出现在第一位
    - 状态回填：提供商 / 审批策略 / 技能 / 工具 / MCP / 新会话
    - SSE 事件渲染：delta → 气泡；tool_call → 工具卡片；approval_request → 审批卡
    - 审批卡片点「允许」会回调后端
    - 浅色主题下新增选择器被正确覆盖（不破坏既有样式）
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def _pump(cond, timeout=3.0):
    """跑事件循环直到 cond() 为真或超时。"""
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class _Cfg:
    def gateway_url(self):
        return "http://127.0.0.1:8000"

    api_key = ""


class _Api:
    def list_models(self):
        return [{"id": "qwen3-4b", "loaded": True}]


class _FakeAssistApi:
    def __init__(self, *a, **kw):
        self.saved = {}
        self.approvals = []

    def state(self):
        return {
            "version": "1.0.8",
            "policy": "readonly_auto",
            "workspace": r"E:\tmp\ws",
            "active_provider": "local",
            "orchestrator": "langgraph",
            "providers": [
                {"id": "local", "label": "本地模型", "kind": "local",
                 "enabled": True, "base_url": "", "model": ""},
                {"id": "siliconflow", "label": "硅基流动", "kind": "openai",
                 "enabled": True, "base_url": "https://api.siliconflow.cn/v1",
                 "model": "Qwen/Qwen3-8B"},
            ],
            "tools": [
                {"name": "fs_read", "description": "读文件", "risk": "low",
                 "source": "builtin", "parameters": {}, "meta": {}},
                {"name": "shell_run", "description": "执行命令", "risk": "high",
                 "source": "builtin", "parameters": {}, "meta": {}},
            ],
            "skills": [{"slug": "repo-digest", "name": "repo-digest",
                        "description": "代码摘要", "tools": ["fs_read"]}],
            "mcp": [{"id": "fs", "name": "文件系统", "transport": "stdio",
                     "status": "connected", "tool_count": 3, "error": ""}],
            "sessions": [],
        }

    def sessions(self):
        return [{"id": "s1", "title": "历史会话", "message_count": 2}]

    def new_session(self):
        return {"id": "s-new", "title": "新会话"}

    def session(self, sid):
        return {"id": sid, "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的，需要我做什么？"}]}

    def delete_session(self, sid):
        return {"deleted": True}

    def save_settings(self, patch):
        self.saved.update(patch)
        return patch

    def stop(self, sid):
        return {"stopped": True}

    def resolve_approval(self, aid, approve, remember=False):
        self.approvals.append((aid, approve, remember))
        return {"id": aid, "status": "approved" if approve else "denied"}

    def skill(self, slug):
        return {"slug": slug, "meta": {"name": slug, "description": "d",
                                       "tools": ["fs_read"]}, "body": "# 目标",
                "files": ["SKILL.md"]}

    def audit(self, limit=100):
        return [{"time": "2026-10-24 21:00:00", "event": "tool_called",
                 "tool": "fs_read"}]

    def skills(self):
        return []

    def mcp_servers(self):
        return []


@pytest.fixture()
def page(monkeypatch):
    from ui.pages import assistant_page as mod
    fake = _FakeAssistApi()
    monkeypatch.setattr(mod, "AssistantApi", lambda *a, **kw: fake)
    p = mod.AssistantPage(_Api(), None, _Cfg())
    p.refresh()          # 构造不自动刷新（避免 exec() 前起线程），测试里手动触发
    assert _pump(lambda: bool(p._state))
    assert _pump(lambda: bool(p._session_id))
    return p, fake


def test_nav_has_assistant_first():
    from ui.main_window import MainWindow
    assert MainWindow.NAV_ITEMS[0] == ("智能助理", 0)
    assert len(MainWindow.NAV_ITEMS) == 6


def test_state_fills_ui(page):
    p, fake = page
    assert p._provider_combo.count() == 2
    assert p._provider_combo.currentData() == "local"
    assert p._policy_combo.currentData() == "readonly_auto"
    assert p._skill_list.count() == 1
    assert p._tool_list.count() == 2
    assert p._mcp_list.count() == 1
    assert "工作区" in p._subtitle.text()
    assert "LangGraph" in p._subtitle.text()


def test_policy_change_persists(page):
    p, fake = page
    idx = p._policy_combo.findData("ask_all")
    p._policy_combo.setCurrentIndex(idx)
    assert _pump(lambda: fake.saved.get("policy") == "ask_all")


def test_sse_events_render(page):
    p, fake = page
    p._begin_streaming()
    p._on_event({"type": "delta", "text": "我先看一下目录。"})
    p._on_event({"type": "tool_call", "name": "fs_read",
                 "args": {"path": "a.txt"}, "risk": "low", "summary": "读取 a.txt"})
    assert len(p._tool_cards) == 1
    p._on_event({"type": "tool_result", "name": "fs_read", "ok": True,
                 "content": "hello"})
    assert p._tool_cards[0]._result[1] == "hello"
    p._on_event({"type": "done", "content": "**结果**：a.txt 里有 hello"})
    p._end_streaming()
    assert p._bubble is None
    assert p._busy is False


def test_approval_card_resolves(page):
    p, fake = page
    p._begin_streaming()
    p._on_event({"type": "approval_request", "id": "ap1", "tool": "shell_run",
                 "args": {"command": "dir"}, "risk": "high",
                 "reason": "执行命令：dir"})
    card = p._approvals.get("ap1")
    assert card is not None
    card._done(True, True)
    assert _pump(lambda: fake.approvals == [("ap1", True, True)])


def test_load_session_renders_history(page):
    p, fake = page
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("历史会话")
    item.setData(0x0100, "s1")          # Qt.UserRole == 0x0100
    p._load_session_item(item)
    assert _pump(lambda: p._session_id == "s1")
    assert p._msg_layout.count() >= 3   # 2 条消息 + 末尾 stretch


def test_light_theme_covers_assistant(page):
    from core.theme import build_stylesheet
    css = build_stylesheet("#ffffff")
    for sel in ("#AssistantSideBar", "#ApprovalCard", "#BotBubble", "#ToolCard"):
        assert sel in css
    dark = build_stylesheet("")
    assert "#AssistantSideBar" in dark


def test_model_controls_live_in_composer(page):
    """模型 / 审批 / 工作区必须贴在输入框里，而不是顶部标题栏（参考 WorkBuddy）。"""
    p, _fake = page
    assert p._composer is not None
    for w in (p._input, p._provider_combo, p._model_combo, p._policy_combo,
              p._ws_btn, p._send_btn):
        assert p._composer.isAncestorOf(w), w.objectName()
    assert not p._composer.isAncestorOf(p._new_btn)   # 新会话仍在标题栏


def test_send_stop_swap_while_streaming(page):
    p, _fake = page
    assert not p._send_btn.isHidden()
    assert p._stop_btn.isHidden()
    p._begin_streaming()
    assert p._send_btn.isHidden()
    assert not p._stop_btn.isHidden()
    p._end_streaming()
    assert not p._send_btn.isHidden()
    assert p._stop_btn.isHidden()


def test_skill_shortcut_prefills_input(page):
    """输入框旁的「技能」入口：选中后把「用 xx 技能：」写进输入框。"""
    p, _fake = page
    p._use_skill("repo-digest")
    assert p._input.toPlainText().startswith("用 repo-digest 技能")
    p._input.setPlainText("看看这个项目")
    p._use_skill("repo-digest")
    assert p._input.toPlainText() == "用 repo-digest 技能：看看这个项目"


def test_done_after_tool_call_still_renders_answer(page):
    """工具调用已把上一条气泡定稿，done 里的最终答复不能丢。"""
    p, _fake = page
    before = p._msg_layout.count()
    p._begin_streaming()
    p._on_event({"type": "tool_call", "name": "fs_list", "args": {},
                 "risk": "low", "summary": "列目录"})
    p._on_event({"type": "tool_result", "name": "fs_list", "ok": True,
                 "content": "[]"})
    p._on_event({"type": "done", "content": "工作区是空的。"})
    p._end_streaming()
    assert p._msg_layout.count() == before + 2      # 工具卡 + 最终答复气泡
    assert p._bubble is None
