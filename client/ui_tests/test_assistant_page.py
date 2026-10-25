# -*- coding: utf-8 -*-
"""
智能助理页面（1.0.8）无头烟雾测试。

1.0.9 结构调整（照 WorkBuddy 重排）后验证点：
    - 页面可构造、导航项出现在第一位
    - 底栏融合工具条：附件/工作区/审批/技能/模型/发送全在输入容器内，单行
    - 模型来源与模型合并进一个下拉菜单（不再两个 QComboBox 并排）
    - 左栏二级栏目：会话直接列出、管理入口带数量，其余进管理面板
    - 状态回填：审批策略 / 工作区 / 技能 / 工具 / MCP / 新会话
    - SSE 事件渲染：delta → 气泡；tool_call → 工具卡片；approval_request → 审批卡
    - 技能 chip：挂载后发消息自动带「用 xx 技能」前缀，输入框不被改写
    - 浅色主题覆盖新增的选择器
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
from PySide6.QtCore import QObject, Signal  # noqa: E402
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog,  # noqa: E402
                               QMessageBox)

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
    def __init__(self):
        self.assistant_workspace = ""     # 本机记的上次工作区
        self.saved = 0

    def gateway_url(self):
        return "http://127.0.0.1:8000"

    def save(self):
        self.saved += 1

    api_key = ""


class _Api:
    def list_models(self):
        return [{"id": "qwen3-4b", "loaded": True}]


class _FakeAssistApi:
    def __init__(self, *a, **kw):
        self.saved = {}
        self.save_calls = []
        self.save_error = ""      # 非空则 save_settings 抛错（模拟后端拒绝）
        self.approvals = []
        self.deleted = []
        self.created = 0

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
        self.created += 1
        return {"id": "s-new", "title": "新会话"}

    def session(self, sid):
        return {"id": sid, "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在的，需要我做什么？"}]}

    def delete_session(self, sid):
        self.deleted.append(sid)
        return {"deleted": True}

    def save_settings(self, patch):
        self.save_calls.append(dict(patch))
        if self.save_error:
            raise RuntimeError(self.save_error)
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


class _FakeStream(QObject):
    """替代 AssistantStream：只记录 payload，不真的发请求。"""

    event = Signal(dict)
    failed = Signal(str)
    closed = Signal()

    instances = []

    def __init__(self, base, key, payload):
        super().__init__()
        self.payload = payload
        _FakeStream.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        pass


@pytest.fixture()
def page(monkeypatch):
    from ui.pages import assistant_page as mod
    fake = _FakeAssistApi()
    monkeypatch.setattr(mod, "AssistantApi", lambda *a, **kw: fake)
    p = mod.AssistantPage(_Api(), None, _Cfg())
    p.build_nav_panel()   # 主窗口会把这块挂到左栏「智能助理」下面
    p.refresh()           # 构造不自动刷新（避免 exec() 前起线程），测试里手动触发
    assert _pump(lambda: bool(p._state))
    assert _pump(lambda: bool(p._session_id))
    assert _pump(lambda: p._nav_sess.count() == 1)
    return p, fake


# ---------------------------------------------------------------------------
# 导航
# ---------------------------------------------------------------------------
def test_nav_has_assistant_first():
    from ui.main_window import MainWindow
    assert MainWindow.NAV_ITEMS[0] == ("智能助理", 0)
    assert len(MainWindow.NAV_ITEMS) == 6


def test_nav_panel_lists_sessions_directly(page):
    """批注：会话/技能/工具/MCP/审计 作为二级栏目放在智能助理下面直接显示。"""
    p, _fake = page
    assert p._nav_sess is not None          # 二级栏目在页面构造时就绪
    assert p._nav_sess.count() == 1         # sessions() 返回 1 条
    assert p._nav_sess.height() > 0
    assert p._nav_entries["skills"].text() == "技能 · 1"
    assert p._nav_entries["tools"].text() == "工具 · 2"
    assert p._nav_entries["mcp"].text() == "MCP · 1"
    assert p._nav_entries["audit"].text() == "审计"


def test_nav_panel_hidden_when_empty(page):
    p, _fake = page
    p._apply_sessions([])
    assert p._nav_sess.count() == 0
    assert p._nav_sess.height() == 0        # 空列表不占位


# ---------------------------------------------------------------------------
# 底栏
# ---------------------------------------------------------------------------
def test_bottom_bar_is_single_row_inside_composer(page):
    """附件/工作区/审批/技能/模型/发送 全部贴在输入框底栏（参考 WorkBuddy）。"""
    p, _fake = page
    assert p._composer is not None
    for w in (p._input, p._attach_btn, p._ws_btn, p._policy_btn, p._skill_btn,
              p._model_btn, p._send_btn, p._stop_btn, p._busy_lbl):
        assert p._composer.isAncestorOf(w), w.objectName()
    # 顶栏只留标题 + 管理入口
    assert not p._composer.isAncestorOf(p._manage_btn)


def test_model_source_and_model_merged_into_one_menu(page):
    """模型来源与模型名不再各占一个下拉，合并为一个按钮 + 菜单。"""
    p, _fake = page
    assert not hasattr(p, "_provider_combo")
    assert not hasattr(p, "_model_combo")
    assert hasattr(p, "_model_btn")
    assert _pump(lambda: "qwen3-4b" in p._model_btn.text())


def test_state_fills_bottom_bar(page):
    p, _fake = page
    assert "只读自动" in p._policy_btn.text()
    assert p._ws_btn.text() == "ws"          # E:\tmp\ws -> 目录名 ws
    assert "LangGraph" in p._subtitle.text()
    assert "工作区" in p._subtitle.text()


def test_policy_change_persists(page):
    p, fake = page
    p._set_policy("ask_all")
    assert _pump(lambda: fake.saved.get("policy") == "ask_all")
    assert "全部确认" in p._policy_btn.text()


def test_switch_model_persists_provider(page):
    p, fake = page
    p._set_provider("siliconflow")
    assert _pump(lambda: fake.saved.get("active_provider") == "siliconflow")
    assert p._active_provider == "siliconflow"


def test_skill_chip_mounts_without_touching_input(page):
    """技能按钮变成 chip：挂载后不改写输入框，只记状态。"""
    p, _fake = page
    p._use_skill("repo-digest")
    assert p._skill_slug == "repo-digest"
    assert p._skill_btn.text() == "✕ repo-digest"
    assert bool(p._skill_btn.property("active")) is True
    assert p._input.toPlainText() == ""
    p._clear_skill()
    assert p._skill_btn.text() == "技能"
    assert bool(p._skill_btn.property("active")) is False


def test_skill_prefix_injected_in_payload(page, monkeypatch):
    """挂载技能后，每条消息自动带「用 xx 技能」前缀（用户看不到，模型看得到）。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, _fake = page
    p._use_skill("repo-digest")
    p._send_with_session({"id": p._session_id}, "看看这个项目")
    payload = _FakeStream.instances[-1].payload
    assert payload["message"] == "用 repo-digest 技能：看看这个项目"
    assert payload["provider"] == "local"
    assert payload["stream"] is True
    p._end_streaming()


# ---------------------------------------------------------------------------
# 管理面板（原右侧 tab 面板）
# ---------------------------------------------------------------------------
def test_manager_dialog_has_five_tabs(page):
    from ui.pages.assistant_page import ManagerDialog
    p, _fake = page
    dlg = ManagerDialog(p)
    assert dlg.tabs.count() == 5
    assert [dlg.tabs.tabText(i) for i in range(5)] == \
        ["会话", "技能", "工具", "MCP", "审计"]
    dlg.show_tab("mcp")
    assert dlg.tabs.currentIndex() == 3
    dlg.deleteLater()


def test_manager_lists_fill_from_state(page):
    from ui.pages.assistant_page import ManagerDialog
    p, _fake = page
    dlg = ManagerDialog(p)
    p._fill_skills(p._state["skills"])
    p._fill_tools(p._state["tools"])
    p._fill_mcp(p._state["mcp"])
    p._fill_sessions()
    assert p._skill_list.count() == 1
    assert p._tool_list.count() == 2
    assert p._mcp_list.count() == 1
    assert _pump(lambda: p._all_sess_list.count() == 1)
    dlg.deleteLater()


# ---------------------------------------------------------------------------
# 会话删除（问题反馈：会话无法删除）
# ---------------------------------------------------------------------------
class _FakeMB:
    """替代 QMessageBox：不弹真模态框，只记录调用并返回预设结果。"""

    StandardButton = QMessageBox.StandardButton
    answer = QMessageBox.StandardButton.Yes
    calls = []

    @classmethod
    def question(cls, parent, title, text, *a, **kw):
        cls.calls.append(("question", parent, title, text))
        return cls.answer

    @classmethod
    def information(cls, parent, title, text, *a, **kw):
        cls.calls.append(("information", parent, title, text))
        return QMessageBox.StandardButton.Ok

    @classmethod
    def warning(cls, parent, title, text, *a, **kw):
        cls.calls.append(("warning", parent, title, text))
        return QMessageBox.StandardButton.Ok


def _open_manager(p):
    from ui.pages.assistant_page import ManagerDialog
    if p._manager is None:
        p._manager = ManagerDialog(p)
    p._fill_sessions()
    assert _pump(lambda: p._all_sess_list.count() >= 1)
    return p._manager


def test_delete_without_selection_shows_hint(page, monkeypatch):
    """没选中就点删除：必须给提示，不能静默返回（否则看着像按钮坏了）。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    p, fake = page
    _open_manager(p)
    p._all_sess_list.setCurrentItem(None)
    p._sel_sid = ""
    p._delete_session()
    assert fake.deleted == []
    assert _FakeMB.calls and _FakeMB.calls[-1][0] == "information"


def test_delete_selected_session_calls_api(page, monkeypatch):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    _FakeMB.answer = QMessageBox.StandardButton.Yes
    p, fake = page
    _open_manager(p)
    p._all_sess_list.setCurrentRow(0)
    p._on_all_sess_clicked(p._all_sess_list.item(0))
    p._delete_session()
    assert _pump(lambda: fake.deleted == ["s1"])


def test_delete_confirm_uses_visible_dialog_parent(page, monkeypatch):
    """确认框的父窗口必须是可见的管理面板，否则会被模态层挡住 → 看起来「点了没反应」。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    p, _fake = page
    dlg = _open_manager(p)
    dlg.show()
    try:
        assert p._dialog_parent() is dlg
        p._all_sess_list.setCurrentRow(0)
        p._delete_session()
        kinds = [c[0] for c in _FakeMB.calls]
        assert "question" in kinds
        question = next(c for c in _FakeMB.calls if c[0] == "question")
        assert question[1] is dlg          # parent 是管理面板，不是页面
    finally:
        dlg.hide()


def test_delete_current_session_recreates(page):
    """删掉的正好是当前会话：清空对话并新开一个，界面不能停在已删除的会话上。"""
    p, fake = page
    before = p._session_id
    created = fake.created
    p._on_session_deleted({"deleted": True}, before)
    assert p._session_id != before
    assert _pump(lambda: fake.created == created + 1)


def test_delete_failure_reports_instead_of_silent(page, monkeypatch):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    p, fake = page
    _open_manager(p)
    before = list(fake.deleted)
    p._on_session_deleted({"deleted": False}, "s1")
    assert fake.deleted == before
    assert _FakeMB.calls and _FakeMB.calls[-1][0] == "warning"


def test_selection_survives_list_refresh(page):
    """列表被 refresh 重建后仍保持选中：否则刚选中就刷新，点删除会像没反应。"""
    p, _fake = page
    _open_manager(p)
    rows = [{"id": "a", "title": "A", "message_count": 1},
            {"id": "b", "title": "B", "message_count": 2}]
    p._apply_sessions(rows)
    p._sel_sid = "b"
    p._apply_sessions(rows)          # 模拟后台刷新
    cur = p._all_sess_list.currentItem()
    assert cur is not None
    assert cur.data(0x0100) == "b"


def test_sweep_empty_sessions_removes_only_empty(page, monkeypatch):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    _FakeMB.answer = QMessageBox.StandardButton.Yes
    p, fake = page
    p._session_id = "k1"
    p._session_rows = [
        {"id": "e1", "title": "新会话", "message_count": 0},
        {"id": "e2", "title": "新会话", "message_count": 0},
        {"id": "k1", "title": "有内容", "message_count": 3},
    ]
    p._sweep_empty_sessions()
    assert _pump(lambda: sorted(fake.deleted) == ["e1", "e2"])


def test_new_session_reuses_empty_current(page):
    """当前会话本来就空时，点「新会话」不再新建，避免攒一堆空会话。"""
    p, fake = page
    p._session_rows = [{"id": p._session_id, "title": "新会话",
                        "message_count": 0}]
    created = fake.created
    p._new_session()
    assert fake.created == created          # 复用，没新建


# ---------------------------------------------------------------------------
# 工作区切换（问题反馈：选工作区闪退 / 重开不接上次工作区）
#
# 两个真实坑：
#   1) 原生目录选择框（系统 shell 弹）在冻结包/远程桌面下会把主窗口一起带走，
#      且没有任何 Python 异常记录（crash.log 为空）→ 必须走 Qt 自绘对话框；
#   2) 选中后如果只发后端 PUT，请求没落地进程就没了 → 下次打开接不上。
#      所以选中要「先写本机 config，再同步后端」。
# ---------------------------------------------------------------------------
class _FakeFD:
    """替代 QFileDialog：返回可编程的假对话框，不真的弹窗。"""

    Option = QFileDialog.Option
    DialogLabel = QFileDialog.DialogLabel
    FileMode = QFileDialog.FileMode
    AcceptMode = QFileDialog.AcceptMode
    result = QDialog.DialogCode.Accepted
    path = ""
    last = None

    def __init__(self, *a, **kw):
        self.opts = {}
        self.labels = {}
        self.mode = None
        self.accept_mode = None
        self.name_filter = ""
        self.selected = ""
        _FakeFD.last = self

    def setOption(self, opt, on=True):
        self.opts[opt] = on

    def setFileMode(self, mode):
        self.mode = mode

    def setAcceptMode(self, mode):
        self.accept_mode = mode

    def setNameFilter(self, flt):
        self.name_filter = flt

    def selectFile(self, name):
        self.selected = name

    def setLabelText(self, which, text):
        self.labels[which] = text

    def exec(self):
        return _FakeFD.result

    def selectedFiles(self):
        return [_FakeFD.path] if _FakeFD.path else []


@pytest.fixture(autouse=True)
def _reset_fd():
    """假对话框的类属性会被上一个用例污染，逐个用例重置。"""
    _FakeFD.result = QDialog.DialogCode.Accepted
    _FakeFD.path = ""
    _FakeFD.last = None
    yield


def _mkdir(tmp_path, name="newws"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def test_choose_workspace_saves_and_updates_button(page, monkeypatch, tmp_path):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    target = _mkdir(tmp_path)
    _FakeFD.path = target
    p, fake = page
    p._choose_workspace()
    # 按钮立刻跟着变，不用等后端 refresh 回来（避免「选完像没接上」）
    assert p._ws_btn.text() == "newws"
    assert _pump(lambda: fake.saved.get("workspace") == target)


def test_choose_workspace_uses_qt_dialog_not_native(page, monkeypatch, tmp_path):
    """必须显式关掉原生目录框：冻结包里原生框会把主窗口一起带走（无 crash.log）。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    _FakeFD.path = _mkdir(tmp_path)
    _FakeFD.result = QDialog.DialogCode.Accepted
    p, _fake = page
    p._choose_workspace()
    dlg = _FakeFD.last
    assert dlg.opts[QFileDialog.Option.DontUseNativeDialog] is True
    assert dlg.opts[QFileDialog.Option.ShowDirsOnly] is True
    assert dlg.mode == QFileDialog.FileMode.Directory
    # 自绘框文案写死中文，不依赖 Qt 翻译文件是否随包
    assert dlg.labels[QFileDialog.DialogLabel.Accept] == "选择此文件夹"
    assert dlg.labels[QFileDialog.DialogLabel.LookIn] == "位置："


def test_choose_workspace_writes_local_config_first(page, monkeypatch, tmp_path):
    """选中即写本机 config：后端同步失败 / 进程中途退出，下次仍能接上。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    target = _mkdir(tmp_path)
    _FakeFD.path = target
    p, fake = page
    fake.save_error = "后端未启动"
    before = p.config.saved
    p._choose_workspace()
    assert p.config.assistant_workspace == target      # 早已落盘
    assert p.config.saved == before + 1
    assert _pump(lambda: "未接受" in p._status.text())  # 同步失败要有说明
    assert any(c[0] == "warning" for c in _FakeMB.calls)


def test_choose_workspace_cancel_does_nothing(page, monkeypatch):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    _FakeFD.path = ""
    _FakeFD.result = QDialog.DialogCode.Rejected
    p, fake = page
    before = dict(fake.saved)
    p._choose_workspace()
    assert fake.saved == before
    assert p._ws_btn.text() == "ws"
    assert p.config.assistant_workspace in ("", None)


def test_choose_workspace_rejects_missing_dir(page, monkeypatch, tmp_path):
    """选到不可用目录（被删 / 盘拔了）时给提示，不能把坏路径同步给后端。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    monkeypatch.setattr(mod, "QMessageBox", _FakeMB)
    _FakeMB.calls = []
    _FakeFD.path = str(tmp_path / "not-exists")
    p, fake = page
    before = dict(fake.saved)
    p._choose_workspace()
    assert fake.saved == before
    assert _FakeMB.calls and _FakeMB.calls[-1][0] == "warning"


def test_workspace_restored_on_refresh(page):
    """重开/切回页面时，底栏工作区必须显示后端保存的那个，而不是默认值。"""
    p, _fake = page
    st = dict(p._state)
    st["workspace"] = r"E:\llm-service\newspace"
    p._apply_state(st)
    assert p._ws_btn.text() == "newspace"
    assert "newspace" in p._ws_btn.toolTip()


def test_workspace_falls_back_to_local_memory(page, tmp_path):
    """后端回报空工作区时用本机记忆顶住，并把这次选择补同步给后端。"""
    p, fake = page
    target = _mkdir(tmp_path)
    p.config.assistant_workspace = target
    st = dict(p._state)
    st["workspace"] = ""
    p._apply_state(st)
    assert p._ws_btn.text() == "newws"
    assert _pump(lambda: {"workspace": target} in fake.save_calls)


def test_workspace_sync_runs_only_once(page, tmp_path):
    """补同步只做一次，免得每次 refresh 都发一次 PUT。"""
    p, fake = page
    target = _mkdir(tmp_path)
    p.config.assistant_workspace = target
    st = dict(p._state)
    st["workspace"] = ""
    p._apply_state(st)
    assert _pump(lambda: fake.save_calls.count({"workspace": target}) == 1)
    p._apply_state(st)
    p._apply_state(st)
    assert fake.save_calls.count({"workspace": target}) == 1


def test_no_native_file_dialog_static_calls():
    """页面里禁止再出现原生文件对话框的静态调用。

    本机冻结包一调原生框就以 `0x8001010E RPC_E_WRONG_THREAD` 崩掉进程
    （crash.log 有栈：`_attach_file → QFileDialog.getOpenFileName`），
    表现就是「点 + / 点工作区直接闪退」。全部走 `_pick_file` 自绘框。
    """
    import inspect
    from ui.pages import assistant_page as mod
    src = inspect.getsource(mod)
    for bad in ("getOpenFileName(", "getSaveFileName(", "getExistingDirectory("):
        assert bad not in src, f"仍有原生文件对话框调用：{bad}"


def test_pick_file_sets_non_native_and_labels(page, monkeypatch, tmp_path):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    target = str(tmp_path / "a.md")
    _FakeFD.path = target
    p, _fake = page
    got = p._pick_file("把文件加进这次提问", "", "所有文件 (*.*)")
    assert got == target
    dlg = _FakeFD.last
    assert dlg.opts[QFileDialog.Option.DontUseNativeDialog] is True
    assert dlg.labels[QFileDialog.DialogLabel.Accept] == "选择"
    assert dlg.mode == QFileDialog.FileMode.ExistingFile


def test_pick_file_save_mode(page, monkeypatch, tmp_path):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    _FakeFD.path = str(tmp_path / "repo.zip")
    p, _fake = page
    got = p._pick_file("导出技能包", "", "技能包 (*.zip)", save=True,
                       default_name="repo.zip")
    assert got.endswith("repo.zip")
    dlg = _FakeFD.last
    assert dlg.accept_mode == QFileDialog.AcceptMode.AcceptSave
    assert dlg.selected == "repo.zip"
    assert dlg.labels[QFileDialog.DialogLabel.Accept] == "选择"


def test_attach_file_inserts_path(page, monkeypatch, tmp_path):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    target = str(tmp_path / "note.md")
    _FakeFD.path = target
    p, _fake = page
    p._attach_file()
    assert p._input.toPlainText().strip() == f"@{target}"


def test_attach_file_cancel_keeps_input(page, monkeypatch):
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "QFileDialog", _FakeFD)
    _FakeFD.path = ""
    _FakeFD.result = QDialog.DialogCode.Rejected
    p, _fake = page
    p._input.setPlainText("原有内容")
    p._attach_file()
    assert p._input.toPlainText() == "原有内容"


def test_workspace_error_from_backend_shows_status(page):
    """后端工作区不可创建（拔盘/无权限）时提示出来，而不是静默当作正常。"""
    p, _fake = page
    st = dict(p._state)
    st["workspace_error"] = "工作区不可用：Z:\\gone"
    p._apply_state(st)
    assert "工作区不可用" in p._status.text()



# ---------------------------------------------------------------------------
# 对话渲染
# ---------------------------------------------------------------------------
def test_sse_events_render(page):
    p, _fake = page
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
    p, _fake = page
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("历史会话")
    item.setData(0x0100, "s1")          # Qt.UserRole == 0x0100
    p._load_session_item(item)
    assert _pump(lambda: p._session_id == "s1")
    assert p._msg_layout.count() >= 3   # 2 条消息 + 末尾 stretch


def test_bubble_width_fits_content_and_caps(page):
    """气泡宽度：短句贴合文字，长段落封顶 MAX_WIDTH（否则中文被压成很窄一列）。"""
    from ui.widgets.assistant_cards import ChatBubble
    short = ChatBubble("user", "你好")
    assert short.label.maximumWidth() <= 200
    long_text = ("把工作区里散落的文档按类型归档到 docs / images / data 三个目录，"
                 "并保留原始目录结构。") * 4
    big = ChatBubble("assistant", long_text)
    assert big.label.maximumWidth() == ChatBubble.MAX_WIDTH
    big.append_text("继续追加一段")
    assert big.label.maximumWidth() == ChatBubble.MAX_WIDTH


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


def test_send_stop_swap_while_streaming(page):
    p, _fake = page
    assert not p._send_btn.isHidden()
    assert p._stop_btn.isHidden()
    assert p._busy_lbl.isHidden()
    p._begin_streaming()
    assert p._send_btn.isHidden()
    assert not p._stop_btn.isHidden()
    assert not p._busy_lbl.isHidden()       # 运行中显示旋转指示
    p._end_streaming()
    assert not p._send_btn.isHidden()
    assert p._stop_btn.isHidden()
    assert p._busy_lbl.isHidden()


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
def test_light_theme_covers_bottom_bar_and_nav(page):
    from core.theme import build_stylesheet
    css = build_stylesheet("#ffffff")
    for sel in ("#BarChip", "#ModelChip", "#BarBtn", "#BusyDot", "#SendBtn",
                "#NavSubItem", "#NavSessList", "#ApprovalCard"):
        assert sel in css, sel
    dark = build_stylesheet("")
    for sel in ("#BarChip", "#ModelChip", "#NavSubItem", "#NavSessList"):
        assert sel in dark, sel
