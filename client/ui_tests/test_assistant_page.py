# -*- coding: utf-8 -*-
"""
智能助理页面（1.0.8）无头烟雾测试。

1.0.8 结构调整（照 WorkBuddy 重排）后验证点：
    - 页面可构造、导航项出现在第一位
    - 底栏融合工具条：附件/工作区/审批/技能/模型/发送全在输入容器内，单行
    - 模型来源与模型合并进一个下拉菜单（不再两个 QComboBox 并排）
    - 左栏「任务 / 空间」两区：任务置顶优先、空间按工作区分组且可展开
    - 任务右键菜单：置顶 / 重命名 / 归档 / 打开所在空间 / 导出 / 删除
    - 状态回填：审批策略 / 工作区 / 技能 / 工具 / MCP / 新任务
    - SSE 事件渲染：delta → 气泡；tool_call → 工具卡片；approval_request → 审批卡
    - 技能 chip：挂载后发消息自动带「用 xx 技能」前缀，输入框不被改写
    - 浅色主题覆盖新增的选择器

1.0.9 引导会话（消息排队）增量验证点：
    - 忙碌态 ↑=排队入队（不再丢弃消息）、⚡=引导发送、状态机 tooltip/placeholder
    - 入队失败保留输入框文本；入队不新建 SSE 流
    - queue 快照驱动面板显隐 / 行数 / 序号；引导行只读（无操作按钮）
    - 行内编辑 / 删除 / 排序 / 全部清除的 REST 参数；首行禁 ↑、末行禁 ↓
    - guided → 用户气泡 + HH:MM；dequeued(queue) 补气泡、guided 不重复
    - done 不结束忙碌、end 才结束；end(paused) 显示「继续」
    - 停止 → 拉快照保留暂停态 → 「继续」发 mode=resume 新流
    - 切换任务重置队列并按新 sid 拉快照
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
                               QInputDialog, QLabel, QPushButton, QMessageBox)
from ui.widgets.assistant_cards import ChatBubble  # noqa: E402
from ui.pages.assistant_page import _ellipsis, parse_mcp_json  # noqa: E402

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
    #: 任务摘要（带所属空间，1.0.8 起任务归属于工作区目录）
    TASK = {"id": "s1", "title": "历史任务", "message_count": 2,
            "workspace": r"E://tmp//ws", "pinned": False, "archived": False,
            "created_at": 1.0, "updated_at": 2.0, "preview": "你好"}

    def __init__(self, *a, **kw):
        self.saved = {}
        self.save_calls = []
        self.save_error = ""      # 非空则 save_settings 抛错（模拟后端拒绝）
        self.approvals = []
        self.deleted = []
        self.created = 0
        self.created_ws = ""
        self.patches = []         # (sid, patch)：置顶/重命名/归档走这里
        self.tasks = [dict(self.TASK)]
        # 引导会话队列（1.0.9）
        self.enqueued = []        # (sid, text, guided)：入队/引导记账
        self.queue_error = ""     # 非空则 enqueue 抛错（模拟后端拒绝）
        self.q_items = []         # 队列快照条目（各变更方法同步维护）
        self.q_paused = False     # 队列暂停态（/stop 置 True）
        self.queue_calls = []     # GET /queue 拉过哪些 sid
        self.updated = []         # (sid, item_id, text)
        self.deleted_items = []   # (sid, item_id)
        self.moved = []           # (sid, item_id, delta)
        self.cleared = []         # sid
        # ---------------- 1.0.8 清单中心 / 工具开关 / 自动加载 ----------------
        self.tool_prefs_calls = 0
        # 注意：字典叫 _prefs_data，方法叫 tool_prefs()——同名属性会遮蔽方法，
        # 页面里 `self._assist.tool_prefs()` 就会 TypeError（1.0.9 踩过）
        self._prefs_data = {"disabled": [], "max_tools": 24, "total": 2,
                            "active": 2, "over": None}
        self.saved_prefs = []     # (disabled[], max_tools)
        self.profiles_calls = 0
        self.profiles_data = {"auto_load": True,
                              "profiles": [{"id": "file-organize",
                                            "name": "文件整理",
                                            "skills": ["workspace-organizer"]}]}
        self.saved_profiles = []  # (auto_load, profiles)
        self.catalog_refreshed = 0
        self.catalog_mcp_data = {"items": [], "cursor": None, "fetched_at": 0,
                                 "stale": False, "error": ""}
        self.catalog_repos_data = {"items": [], "total_count": 0,
                                   "fetched_at": 0, "stale": False, "error": ""}
        self.catalog_fail = ""    # 非空则在 catalog_* 上抛该错误（离线退化用例）
        self.installed_dir = []   # from_dir 的 path
        self.installed_url = []   # from_url 的 url
        self.installed_skill = []  # (repo, ref, dir, slug)
        self.installed_mcp = []   # (registry_name, env)
        self.added_mcp = []       # 粘贴 JSON 添加走的 add_mcp payload

    def spaces(self):
        return [{"path": r"E://tmp//ws", "name": "ws", "count": len(self.tasks),
                 "exists": True, "tasks": [dict(t) for t in self.tasks]}]

    def update_session(self, sid, patch):
        self.patches.append((sid, dict(patch)))
        for t in self.tasks:
            if t.get("id") == sid:
                t.update(patch)
                return dict(t)
        return {"id": sid, **patch}

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
            "sessions": self.sessions(),
            "spaces": self.spaces(),
        }

    def sessions(self):
        return [dict(t) for t in self.tasks]

    def new_session(self, workspace=""):
        self.created += 1
        self.created_ws = workspace
        return {"id": "s-new", "title": "新任务",
                "workspace": workspace or r"E://tmp//ws"}

    def session(self, sid):
        return {"id": sid, "workspace": r"E://tmp//ws", "messages": [
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

    # ---------------- 引导会话队列（1.0.9） ----------------
    def queue(self, sid):
        """GET /queue：返回内部维护的快照（页面变更后自动拉取会用）。"""
        self.queue_calls.append(sid)
        return {"items": [dict(i) for i in self.q_items],
                "paused": self.q_paused}

    def enqueue(self, sid, text, guided=False):
        if self.queue_error:
            raise RuntimeError(self.queue_error)
        self.enqueued.append((sid, text, guided))
        item = {"id": f"q{len(self.q_items) + 1}", "text": text,
                "guided": guided, "time": "12:00", "created_at": 1.0,
                "edited_at": None}
        self.q_items.append(item)
        return dict(item)

    def queue_update(self, sid, item_id, text):
        self.updated.append((sid, item_id, text))
        for it in self.q_items:
            if it["id"] == item_id:
                it["text"] = text
                it["edited_at"] = 2.0
                return dict(it)
        return {"id": item_id, "text": text, "guided": False, "time": "12:01",
                "created_at": 1.0, "edited_at": 2.0}

    def queue_delete(self, sid, item_id):
        self.deleted_items.append((sid, item_id))
        self.q_items = [i for i in self.q_items if i["id"] != item_id]
        return {"deleted": item_id}

    def queue_move(self, sid, item_id, delta):
        self.moved.append((sid, item_id, delta))
        ids = [i["id"] for i in self.q_items]
        if item_id in ids:
            i = ids.index(item_id)
            j = max(0, min(len(ids) - 1, i + int(delta)))
            if i != j:
                self.q_items[i], self.q_items[j] = \
                    self.q_items[j], self.q_items[i]
        return {"items": [dict(i) for i in self.q_items]}

    def queue_clear(self, sid):
        self.cleared.append(sid)
        n = len([i for i in self.q_items if not i.get("guided")])
        self.q_items = [i for i in self.q_items if i.get("guided")]
        return {"cleared": n}

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

    # ---------------- 1.0.8 工具偏好 / profile / 在线目录 ----------------
    def tool_prefs(self):
        self.tool_prefs_calls += 1
        return dict(self._prefs_data)

    def save_tool_prefs(self, disabled=None, max_tools=None):
        self.saved_prefs.append((list(disabled or []), max_tools))
        self._prefs_data = {"disabled": list(disabled or []),
                            "max_tools": int(max_tools or 24),
                            "total": 2,
                            "active": max(0, 2 - len(disabled or [])),
                            "over": None}
        return dict(self._prefs_data)

    def profiles(self):
        self.profiles_calls += 1
        return dict(self.profiles_data)

    def save_profiles(self, auto_load=None, profiles=None):
        self.saved_profiles.append((auto_load, profiles))
        if auto_load is not None:
            self.profiles_data = {**self.profiles_data, "auto_load": auto_load}
        return dict(self.profiles_data)

    def _catalog_guard(self):
        if self.catalog_fail:
            raise RuntimeError(self.catalog_fail)

    def catalog_mcp(self, q="", cursor="", force=False):
        self._catalog_guard()
        return dict(self.catalog_mcp_data)

    def catalog_repos(self, q="", force=False):
        self._catalog_guard()
        return dict(self.catalog_repos_data)

    def catalog_repo_skills(self, owner, repo, ref="", force=False):
        self._catalog_guard()
        return {"commit": "abc", "items": [
            {"slug": "pdf", "dir": "skills/pdf", "name": "pdf",
             "description": "PDF 处理", "files": ["SKILL.md"],
             "has_scripts": False}]}

    def catalog_install_skill(self, repo, ref="", dirpath="", slug="",
                              overwrite=False):
        self.installed_skill.append((repo, ref, dirpath, slug))
        return {"slug": slug, "files": 1, "bytes": 100, "has_scripts": False}

    def catalog_install_mcp(self, registry_name, server_id="", name="",
                            env=None, risk="", connect=True):
        self.installed_mcp.append((registry_name, dict(env or {})))
        return {"server": {"id": "x", "name": "Remote FS"},
                "connected": True, "error": ""}

    def catalog_refresh(self):
        self.catalog_refreshed += 1
        return {"ok": True, "counts": {"mcp": 12, "gh_repos": 3}, "errors": []}

    def catalog_from_dir(self, path, overwrite=False):
        self.installed_dir.append(path)
        return {"slug": "from-dir", "files": 2, "bytes": 200}

    def catalog_from_url(self, url, overwrite=False):
        self.installed_url.append(url)
        return {"slug": "from-url", "files": 1, "bytes": 100}

    def add_mcp(self, cfg):
        self.added_mcp.append(dict(cfg))
        return {"id": cfg.get("id"), "connected": True, "error": ""}


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
    assert _pump(lambda: "s1" in p._nav_task_btns)
    assert p._nav_spaces
    # 等初始队列快照拉回再交出场：否则这个空快照的 Worker 可能晚到，
    # 在用例中途把面板清成空（全量跑时线程调度更慢，必现）
    assert _pump(lambda: p._session_id in fake.queue_calls)
    return p, fake


# ---------------------------------------------------------------------------
# 导航
# ---------------------------------------------------------------------------
def test_nav_has_assistant_first():
    from ui.main_window import MainWindow
    assert MainWindow.NAV_ITEMS[0] == ("智能助理", 0)
    assert len(MainWindow.NAV_ITEMS) == 6


def test_nav_panel_title_says_tasks_not_sessions(page):
    """批注：左栏「会话」改成「任务」（与 WorkBuddy 的任务/空间一致）。"""
    p, _fake = page
    assert p._nav_task_title.text() == "任务 (1)"


def test_approval_card_uses_task_wording():
    """「本会话允许同类」跟着改成「本任务允许同类」，术语全链路一致。"""
    from PySide6.QtWidgets import QPushButton
    from ui.widgets.assistant_cards import ApprovalCard
    card = ApprovalCard("a1", "fs_write", "写文件 a.txt", "medium",
                        {"path": "a.txt"})
    texts = [b.text() for b in card.findChildren(QPushButton)]
    assert "本任务允许同类" in texts


def test_left_nav_no_manager_entries(page):
    """左栏只保留「任务 / 空间」两区，管理入口（技能/工具/MCP/审计）已迁到 [+]。"""
    p, _fake = page
    assert not hasattr(p, "_nav_entries"), "左栏不应再有二级管理入口"
    btn = p._nav_task_btns.get("s1")
    assert btn is not None
    assert "历史任务" in btn.text()


def test_nav_panel_empty_state(page):
    p, _fake = page
    p._apply_sessions([])
    assert p._nav_task_btns == {}
    texts = [w.text() for w in p._nav_body.findChildren(QLabel)]
    assert any("还没有任务" in t for t in texts)


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
    # 宽度压缩：底栏策略按钮只显示短名（「⊘ 自动」），完整名在 tooltip
    from ui.pages.assistant_page import POLICY_SHORT
    assert POLICY_SHORT["readonly_auto"] in p._policy_btn.text()
    assert "只读自动" in p._policy_btn.toolTip()
    assert p._ws_btn.text() == "ws"          # E:\tmp\ws -> 目录名 ws
    assert "LangGraph" in p._subtitle.text()
    assert "工作区" in p._subtitle.text()


def test_policy_change_persists(page):
    p, fake = page
    p._set_policy("ask_all")
    assert _pump(lambda: fake.saved.get("policy") == "ask_all")
    from ui.pages.assistant_page import POLICY_SHORT
    assert POLICY_SHORT["ask_all"] in p._policy_btn.text()
    assert "全部确认" in p._policy_btn.toolTip()


def test_switch_model_persists_provider(page):
    p, fake = page
    p._set_provider("siliconflow")
    assert _pump(lambda: fake.saved.get("active_provider") == "siliconflow")
    assert p._active_provider == "siliconflow"


def test_multi_skill_mount_renders_chips_without_touching_input(page):
    """多技能挂载：每个技能一个可移除 chip，且不改写输入框。"""
    p, _fake = page
    p._add_skill("repo-digest")
    p._add_skill("code-review")
    assert p._skill_slugs == ["repo-digest", "code-review"]
    chips = [b for b in p._skill_chip_cont.findChildren(QPushButton)]
    assert len(chips) == 2
    assert p._skill_chip_cont.isVisibleTo(p)         # 显式可见（头less 下父链未 show）
    assert p._input.toPlainText() == ""              # 输入框保持干净
    assert bool(p._skill_btn.property("active")) is True
    p._remove_skill("repo-digest")
    assert p._skill_slugs == ["code-review"]
    chips = [b for b in p._skill_chip_cont.findChildren(QPushButton)]
    assert len(chips) == 1
    p._clear_skills()
    assert p._skill_slugs == []
    assert not p._skill_chip_cont.isVisibleTo(p)
    assert p._skill_btn.text() == "技能"
    assert bool(p._skill_btn.property("active")) is False


def test_multi_skill_prefix_injected_in_payload(page, monkeypatch):
    """多技能挂载后走 `mounted_skills` 字段，**不再污染 message 正文**（1.0.8 A4）。

    老实现把「用 xx、yy 技能：<text>」拼进 message，落库后历史正文被污染
    （用户复制 / 回看会看到前缀）。新协议：message 是原文，挂载列表走
    `mounted_skills`，后端写进 system 段。
    """
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, _fake = page
    p._add_skill("repo-digest")
    p._add_skill("code-review")
    p._send_with_session({"id": p._session_id}, "看看这个项目")
    payload = _FakeStream.instances[-1].payload
    assert payload["message"] == "看看这个项目"          # 原文，无前缀
    assert payload["mounted_skills"] == ["repo-digest", "code-review"]
    assert payload["provider"] == "local"
    assert payload["stream"] is True
    p._end_streaming()


def test_no_mounted_skills_key_when_nothing_mounted(page, monkeypatch):
    """没挂载技能时不发 `mounted_skills`（老后端不认识该字段也不受影响）。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, _fake = page
    p._skill_slugs = []
    p._send_with_session({"id": p._session_id}, "裸消息")
    assert "mounted_skills" not in _FakeStream.instances[-1].payload
    p._end_streaming()


def test_attach_plus_menu_has_five_entries(page):
    """底栏 [+]：首项添加附件，含 4 类添加入口 + 汇总清单入口，以「审计」结尾（4 段）。"""
    p, _fake = page
    menu = p._build_attach_menu()
    texts = [a.text() for a in menu.actions() if a.text()]
    seps = sum(1 for a in menu.actions() if a.isSeparator())
    assert texts[0] == "添加附件…"
    # 1.0.8：只保留「添加类」+ 一个汇总入口，管理类压成末尾一组（架构 §9.2 R-S5）
    for must in ("能力清单（技能 / 工具 / MCP）…",
                 "从清单添加技能…", "从清单添加 MCP…",
                 "从本地文件夹导入技能…", "导入技能 zip…",
                 "粘贴 URL 安装技能…", "粘贴 JSON 添加 MCP…",
                 "刷新在线目录"):
        assert must in texts, must
    assert texts[-1] == "审计"
    assert texts[-4:] == ["技能", "工具", "MCP", "审计"]
    assert seps == 5          # 4 个分组段 + 末尾管理类前的一段
    assert len(texts) == 13


def test_sixth_skill_rejected(page):
    """已挂载 5 个时，第 6 个技能被拒（上限 5，对标 WorkBuddy 多技能）。"""
    p, _fake = page
    for i in range(5):
        p._add_skill(f"skill-{i}")
    assert len(p._skill_slugs) == 5
    p._add_skill("skill-5")          # 第 6 个应被拒
    assert len(p._skill_slugs) == 5
    assert "skill-5" not in p._skill_slugs


def test_skill_menu_hover_shows_detail(page):
    """技能菜单悬停显示详细介绍：description + 工具数（setToolTipsVisible）。"""
    p, _fake = page
    menu = p._build_skill_menu()
    assert menu.toolTipsVisible() is True
    act = next(a for a in menu.actions() if a.text() == "repo-digest")
    tip = act.toolTip()
    assert "代码摘要" in tip              # SKILL.md description
    assert "1 个工具" in tip              # 工具数


def test_skill_btn_has_sparkle_icon(page):
    """「技能」按钮带 WorkBuddy 风格星芒图标（随状态回填生成，非空图标）。"""
    p, _fake = page
    assert not p._skill_btn.icon().isNull()
    icon = p._skill_icon()
    assert not icon.isNull()
    assert not icon.pixmap(32, 32).isNull()


def test_model_menu_overflow_into_submenu(page):
    """超过 20 个模型收进「还有 N 个模型 ▸」可点开的子菜单（不再是禁用的一行字）。"""
    p, _fake = page
    p._model_cache["local"] = [f"model-{i:02d}" for i in range(25)]
    menu = p._build_model_menu()
    texts = [a.text() for a in menu.actions() if a.text()]
    more_text = next(t for t in texts if t.startswith("还有"))
    assert more_text == "还有 5 个模型 ▸"
    more_action = next(a for a in menu.actions() if a.text() == more_text)
    assert more_action.isEnabled()       # 可点开（之前 setEnabled(False) 找不到入口）
    sub = more_action.menu()
    assert sub is not None
    sub_names = [a.text() for a in sub.actions()]
    # 1.0.10：本地模型标「本地·免费」，比对时只取模型名部分
    sub_models = [t.split()[0] for t in sub_names]
    assert sub_models == [f"model-{i:02d}" for i in range(20, 25)]
    assert all("本地" in t for t in sub_names)


def test_model_menu_has_custom_model_entry(page):
    """模型菜单带「添加自定义模型…」入口；custom 启用后变为「自定义模型配置…」。"""
    p, fake = page
    menu = p._build_model_menu()
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "添加自定义模型…" in texts

    # 走一遍配置流程：填端点 + 模型 → 启用 custom 并切换
    from ui.pages.assistant_page import ProviderDialog

    class _Dlg:
        def __init__(self, *_a, **_kw):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def payload(self):
            return {"base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat", "api_key": "sk-test"}

    orig = ProviderDialog
    try:
        import ui.pages.assistant_page as mod
        mod.ProviderDialog = _Dlg
        p._configure_custom_provider()
    finally:
        mod.ProviderDialog = orig
    assert p._active_provider == "custom"
    assert p._active_model == "deepseek-chat"
    prov = next(x for x in p._state["providers"] if x.get("id") == "custom")
    assert prov["enabled"] is True
    assert prov["base_url"] == "https://api.deepseek.com/v1"
    assert _pump(lambda: fake.saved.get("active_provider") == "custom")
    assert _pump(lambda: bool(fake.saved.get("providers")))
    saved = {x["id"]: x for x in fake.saved["providers"]}
    assert saved["custom"]["enabled"] is True

    # 菜单文案切换为「自定义模型配置…」，且模型列表来自提供商配置本身
    assert _pump(lambda: p._model_cache.get("custom") == ["deepseek-chat"])
    menu2 = p._build_model_menu()
    texts2 = [a.text() for a in menu2.actions() if a.text()]
    assert "自定义模型配置…" in texts2
    assert "deepseek-chat" in texts2


def test_local_models_filtered_by_downloaded(page, monkeypatch):
    """本地模型清单只认真的在模型目录里的模型（downloaded/loaded 任一为真）。"""
    p, _fake = page

    class _Api2:
        def list_models(self):
            return [
                {"id": "in-dir", "loaded": False, "downloaded": True},
                {"id": "running", "loaded": True, "downloaded": True},
                {"id": "ghost-builtin", "loaded": False, "downloaded": False},
            ]

    p.api = _Api2()
    p._fill_models("local")
    assert _pump(lambda: p._model_cache.get("local") == ["in-dir", "running"])


# ---------------------------------------------------------------------------
# 思考过程呈现（WorkBuddy 形态：平铺不折叠）+ 流式防闪
# ---------------------------------------------------------------------------
def test_thinking_block_is_flat_not_collapsible(page):
    """思考过程按 WorkBuddy 方式平铺：小标题「深度思考」+ 正文一直可见，不折叠。"""
    from ui.widgets.assistant_cards import ThinkingBlock
    p, _fake = page
    p._begin_streaming()
    p._on_event({"type": "reasoning", "text": "先看看工作区里有什么"})
    assert isinstance(p._thinking, ThinkingBlock)
    assert p._thinking.head.text().startswith("深度思考")
    # 不是 _Collapsible：没有「展开/收起」按钮，正文直接可见
    assert not hasattr(p._thinking, "toggle_btn")
    assert p._thinking.body.isVisibleTo(p)
    p._end_streaming()


def test_reasoning_text_accumulates_and_flushes(page):
    """多段思考内容按节流窗口合并刷新，结束后全部可见。"""
    p, _fake = page
    p._begin_streaming()
    p._on_event({"type": "reasoning", "text": "第一段思考"})
    p._on_event({"type": "reasoning", "text": "，第二段思考"})
    p._thinking.finish()
    body = p._thinking.body.text()
    assert "第一段思考" in body
    assert "第二段思考" in body
    assert "深度思考" in p._thinking.head.text()
    p._end_streaming()


def test_bubble_width_fixed_during_stream(page):
    """流式输出时气泡宽度固定：宽度不变才不会反复折行闪屏。"""
    p, _fake = page
    p._begin_streaming()
    bubble = p._ensure_bubble()
    w0 = bubble.label.width()
    for i in range(20):
        bubble.append_text(f"增量{i} ")
    assert bubble.label.width() == w0        # 宽度不随内容变化
    bubble.end_stream()
    p._end_streaming()


def test_scroll_requests_coalesced(page):
    """滚动请求被合并：连续调用只滚一次（防整屏闪动）。"""
    p, _fake = page
    p._scroll_bottom()
    p._scroll_bottom()
    p._scroll_bottom()
    assert p._scroll_timer.isActive() is True
    assert _pump(lambda: not p._scroll_timer.isActive())


def test_scrolled_up_user_not_forced_to_bottom(page):
    """用户自己往上翻之后，流式输出不再硬拽回底部。"""
    p, _fake = page
    p._scroll_timer.stop()          # 清掉之前遗留的滚动请求
    p._follow = False
    p._scroll_bottom()
    assert p._scroll_timer.isActive() is False
    p._follow = True
    p._scroll_bottom(force=True)
    assert p._scroll_timer.isActive() is True


# ---------------------------------------------------------------------------
# 管理面板（原右侧 tab 面板）
# ---------------------------------------------------------------------------
def test_manager_dialog_has_five_tabs(page):
    from ui.pages.assistant_page import ManagerDialog
    p, _fake = page
    dlg = ManagerDialog(p)
    assert dlg.tabs.count() == 5
    assert [dlg.tabs.tabText(i) for i in range(5)] == \
        ["任务", "技能", "工具", "MCP", "审计"]
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
# 任务 / 空间（批注：「会话」改成「任务」，按 WorkBuddy 的任务+空间实现）
#
# 语义对齐 WorkBuddy：
#   任务 = 一次对话（后端仍叫 session，数据格式不变，老数据无需迁移）
#   空间 = 任务所属的工作区目录；任务挂在空间下，点空间展开
# ---------------------------------------------------------------------------
def _two_tasks(nav_page):
    p, fake = nav_page
    fake.tasks = [
        {"id": "a", "title": "旧任务", "message_count": 3, "workspace": r"E://ws1",
         "pinned": False, "archived": False, "updated_at": 100.0},
        {"id": "b", "title": "新任务", "message_count": 1, "workspace": r"E://ws2",
         "pinned": False, "archived": False, "updated_at": 200.0},
    ]
    p._apply_sessions([dict(t) for t in fake.tasks])
    return p, fake


def test_pinned_task_sorts_first(page):
    """置顶的任务永远排最前（WorkBuddy 的「置顶」语义），其余按最近活动。"""
    p, fake = page
    p._apply_sessions([
        {"id": "a", "title": "旧", "updated_at": 100.0, "pinned": False},
        {"id": "b", "title": "置顶的", "updated_at": 50.0, "pinned": True},
        {"id": "c", "title": "新", "updated_at": 200.0, "pinned": False},
    ])
    order = [s["id"] for s in p._session_rows]
    assert order == ["b", "c", "a"]


def test_space_groups_by_workspace(page):
    """任务按所属工作区分组成「空间」，同名目录只算一个空间。"""
    p, fake = _two_tasks(page)
    spaces = {sp["name"]: sp["count"] for sp in p._nav_spaces}
    assert spaces == {"ws1": 1, "ws2": 1}


def test_space_path_normalized_so_no_duplicate_groups(page):
    """E:////x 与 E:/x/ 是同一个空间，否则左栏会出现两条同名目录。"""
    p, _fake = page
    p._apply_sessions([
        {"id": "a", "title": "t1", "workspace": r"E://llm-service", "updated_at": 1},
        {"id": "b", "title": "t2", "workspace": "E:/llm-service/", "updated_at": 2},
    ])
    assert len(p._nav_spaces) == 1
    assert p._nav_spaces[0]["count"] == 2


def test_space_expands_to_show_its_tasks(page):
    """展开空间会多出一组该空间下的任务行（收起后复原）。"""
    from PySide6.QtWidgets import QPushButton
    p, fake = _two_tasks(page)
    path = [sp["path"] for sp in p._nav_spaces if sp["name"] == "ws1"][0]
    before = len(p._nav_body.findChildren(QPushButton))
    p._toggle_space(path)
    assert p._open_space == path
    assert len(p._nav_body.findChildren(QPushButton)) > before
    p._toggle_space(path)                        # 再点收起
    assert p._open_space == ""
    assert len(p._nav_body.findChildren(QPushButton)) == before


def test_space_accordion_only_one_open(page):
    """手风琴：展开第二个空间时第一个自动收起，避免左栏被撑爆。"""
    p, fake = _two_tasks(page)
    p1 = [sp["path"] for sp in p._nav_spaces if sp["name"] == "ws1"][0]
    p2 = [sp["path"] for sp in p._nav_spaces if sp["name"] == "ws2"][0]
    p._toggle_space(p1)
    p._toggle_space(p2)
    assert p._open_space == p2


def test_new_task_in_space_passes_workspace(page, tmp_path):
    """空间右键「在此空间新建任务」→ 新任务带上该空间，且工作区切过去。"""
    ws = tmp_path / "ws1"
    ws.mkdir()
    p, fake = _two_tasks(page)
    before = fake.created
    p._new_task_in_space(str(ws))
    assert _pump(lambda: fake.created == before + 1)
    assert fake.created_ws == str(ws)
    assert _pump(lambda: str(p._state.get("workspace") or "").endswith("ws1"))


def test_open_task_switches_workspace_to_its_space(page, monkeypatch, tmp_path):
    """打开任务时工作区跟到它所属空间，否则文件工具会改到上一个目录。"""
    ws = tmp_path / "ws2"
    ws.mkdir()
    p, fake = page
    monkeypatch.setattr(
        fake, "session",
        lambda sid: {"id": sid, "workspace": str(ws),
                     "messages": [{"role": "user", "content": "hi"}]})
    p._open_task("b")
    assert _pump(lambda: any(str(c.get("workspace") or "").endswith("ws2")
                             for c in fake.save_calls))


def test_open_task_skips_missing_space(page, monkeypatch):
    """空间目录不存在（移动盘拔了）时不切工作区，免得把工作区指到死路径。"""
    p, fake = page
    monkeypatch.setattr(
        fake, "session",
        lambda sid: {"id": sid, "workspace": r"Z://gone",
                     "messages": []})
    before = len(fake.save_calls)
    p._open_task("b")
    assert _pump(lambda: p._session_id == "b")
    assert len(fake.save_calls) == before


def test_rename_task_patches_title(page, monkeypatch):
    from ui.pages import assistant_page as mod
    p, fake = page
    monkeypatch.setattr(mod.QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("改好的名字", True)))
    p._rename_task("s1", "历史任务")
    assert _pump(lambda: fake.patches)
    assert fake.patches[0] == ("s1", {"title": "改好的名字"})


def test_rename_task_cancel_does_nothing(page, monkeypatch):
    from ui.pages import assistant_page as mod
    p, fake = page
    monkeypatch.setattr(mod.QInputDialog, "getText",
                        staticmethod(lambda *a, **kw: ("", False)))
    p._rename_task("s1", "历史任务")
    assert fake.patches == []


def test_archive_task_patches_flag(page):
    p, fake = page
    p._patch_task("s1", {"archived": True})
    assert _pump(lambda: fake.patches)
    assert fake.patches[0] == ("s1", {"archived": True})


def _menu_texts(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_task_context_menu_has_workbuddy_actions(page):
    """右键菜单对齐 WorkBuddy：置顶 / 重命名 / 打开文件夹 / 归档 / 删除。"""
    p, _fake = page
    texts = _menu_texts(p.build_task_menu("s1"))
    for want in ("置顶", "重命名…", "打开所在空间", "导出为 Markdown…",
                 "归档", "删除任务"):
        assert want in texts, want


def test_pinned_task_menu_offers_unpin(page):
    p, fake = page
    fake.tasks[0]["pinned"] = True
    p._apply_sessions([dict(t) for t in fake.tasks])
    assert "取消置顶" in _menu_texts(p.build_task_menu("s1"))


def test_space_menu_has_new_task_action(page):
    p, _fake = page
    menu = p.build_space_menu(r"E://ws1")
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "在此空间新建任务" in texts
    assert "在资源管理器中打开" in texts


def test_delete_task_from_nav_asks_confirm(page, monkeypatch):
    from ui.pages import assistant_page as mod
    p, fake = page
    asked = []

    class _MB:
        StandardButton = QMessageBox.StandardButton

        @staticmethod
        def question(parent, title, text, *a, **kw):
            asked.append((title, text))
            return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(mod, "QMessageBox", _MB)
    p._delete_task("s1", "历史任务")
    assert asked and "删除任务" in asked[0][0]
    assert "历史任务" in asked[0][1]
    assert _pump(lambda: fake.deleted == ["s1"])


def test_startup_reuses_idle_task_instead_of_creating(monkeypatch):
    """重启时复用「0 条消息」的空任务，不要每启动一次就多一个空任务。"""
    from ui.pages import assistant_page as mod
    fake = _FakeAssistApi()
    fake.tasks = [{"id": "idle1", "title": "新任务", "message_count": 0,
                   "workspace": r"E://tmp//ws", "pinned": False,
                   "archived": False, "updated_at": 5.0}]
    monkeypatch.setattr(mod, "AssistantApi", lambda *a, **kw: fake)
    p = mod.AssistantPage(_Api(), None, _Cfg())
    p.build_nav_panel()
    p.refresh()
    assert _pump(lambda: p._session_id == "idle1")
    assert fake.created == 0


def test_space_path_missing_marks_warning(page):
    """空间目录被删/移动盘拔出：图标变 ⚠，任务仍能看到（不是数据丢失）。"""
    p, _fake = page
    p._apply_sessions([{"id": "a", "title": "t", "workspace": r"Z://gone",
                        "updated_at": 1}])
    sp = p._nav_spaces[0]
    assert sp["exists"] is False
    from PySide6.QtWidgets import QPushButton
    texts = [b.text() for b in p._nav_body.findChildren(QPushButton)]
    assert any("⚠" in t for t in texts)


def test_empty_tasks_hidden_from_nav_but_kept_for_manager(page):
    """0 条消息的空任务不进左栏，但仍留在管理面板的任务列表里。

    反复点「+」新建任务会攒出一串「新任务（0 条）」，如果左栏也列出来，
    「任务」区那几个位置全被空任务占满，真正在聊的任务反而看不见了。
    但空任务不能删——管理面板要能列出来给用户自己清理（清理空任务按钮）。
    """
    p, _fake = page
    p._apply_sessions([
        {"id": "a", "title": "聊过的", "message_count": 3,
         "workspace": r"E://ws1", "updated_at": 300.0},
        {"id": "b", "title": "新任务", "message_count": 0,
         "workspace": r"E://ws1", "updated_at": 200.0},
        {"id": "c", "title": "新任务", "message_count": 0,
         "workspace": r"E://ws1", "updated_at": 100.0},
    ])
    assert [s["id"] for s in p._session_rows] == ["a", "b", "c"]   # 全量快照不丢
    assert [s["id"] for s in p._nav_rows] == ["a"]                 # 左栏只留非空
    assert p._nav_task_title.text() == "任务 (1)"
    assert p._nav_spaces[0]["count"] == 1                          # 空间计数同步


def test_current_empty_task_still_shows_in_nav(page):
    """刚新建、还没发消息的任务要看得见（否则点完「+」左栏没反应）。"""
    p, _fake = page
    p._session_id = "b"
    p._apply_sessions([
        {"id": "a", "title": "聊过的", "message_count": 3, "updated_at": 300.0},
        {"id": "b", "title": "新任务", "message_count": 0, "updated_at": 200.0},
    ])
    assert [s["id"] for s in p._nav_rows] == ["a", "b"]
    assert "b" in p._nav_task_btns


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
    # 底栏宽度预算规则①：工作区名最多显示 4 个字，超出用「…」截断（完整名在 tooltip）
    assert p._ws_btn.text() == _ellipsis("newws", 4)
    assert target in p._ws_btn.toolTip()
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
    # 底栏宽度预算规则①：按钮只显示 4 个字，完整名放在 tooltip
    assert p._ws_btn.text() == _ellipsis("newspace", 4)
    assert "newspace" in p._ws_btn.toolTip()


def test_workspace_falls_back_to_local_memory(page, tmp_path):
    """后端回报空工作区时用本机记忆顶住，并把这次选择补同步给后端。"""
    p, fake = page
    target = _mkdir(tmp_path)
    p.config.assistant_workspace = target
    st = dict(p._state)
    st["workspace"] = ""
    p._apply_state(st)
    assert p._ws_btn.text() == _ellipsis("newws", 4)
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
    """流式中止按钮就地出现。

    1.0.9 起忙碌态 ↑ 不再隐藏——语义变为「排队入队」（Enter/↑ 插话不
    打断当前轮），同时新增 ⚡ 引导按钮（仅忙碌可见）。
    """
    p, _fake = page
    assert not p._send_btn.isHidden()
    assert p._stop_btn.isHidden()
    assert p._busy_lbl.isHidden()
    assert p._guide_btn.isHidden()
    p._begin_streaming()
    assert not p._send_btn.isHidden()        # ↑ 变成排队入队（不再隐藏）
    assert not p._stop_btn.isHidden()
    assert not p._busy_lbl.isHidden()       # 运行中显示旋转指示
    assert not p._guide_btn.isHidden()      # ⚡ 引导发送（仅忙碌可见）
    p._end_streaming()
    assert not p._send_btn.isHidden()
    assert p._stop_btn.isHidden()
    assert p._busy_lbl.isHidden()
    assert p._guide_btn.isHidden()


# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
def test_light_theme_covers_bottom_bar_and_nav(page):
    from core.theme import build_stylesheet
    css = build_stylesheet("#ffffff")
    for sel in ("#BarChip", "#ModelChip", "#BarBtn", "#BusyDot", "#SendBtn",
                "#NavSubItem", "#NavSessList", "#ApprovalCard",
                "#QueuePanel", "#QueueRow", "#QueueOp", "#GuideBtn",
                "#BubbleTime", "#QueueResumeBtn"):
        assert sel in css, sel
    dark = build_stylesheet("")
    for sel in ("#BarChip", "#ModelChip", "#NavSubItem", "#NavSessList",
                "#QueuePanel", "#QueueRow", "#QueueOp", "#GuideBtn",
                "#BubbleTime"):
        assert sel in dark, sel


# ---------------------------------------------------------------------------
# 引导会话（1.0.9）：排队插话 / 引导发送 / 队列面板 / 自动消费 / 停止暂停
# ---------------------------------------------------------------------------
def _queue_items(n: int = 3):
    """构造 n 条队列快照条目（最后一条是引导只读行）。"""
    items = [{"id": f"q{i}", "text": f"排队消息{i}", "guided": False,
              "time": f"09:3{i}", "created_at": float(i), "edited_at": None}
             for i in range(n - 1)]
    items.append({"id": f"q{n - 1}", "text": "引导消息", "guided": True,
                  "time": "09:40", "created_at": 99.0, "edited_at": None})
    return items


def test_busy_send_queues_without_new_stream(page, monkeypatch):
    """P0-1：忙碌态 Enter/↑ 入队——不新建流、不打断忙碌、消息不丢。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, fake = page
    _FakeStream.instances.clear()
    p._input.setPlainText("插一句话")
    p._begin_streaming()
    p._on_send_clicked()
    assert _pump(lambda: fake.enqueued ==
                 [(p._session_id, "插一句话", False)]
                 and p._input.toPlainText() == "")
    assert p._busy is True                       # 忙碌态不中断
    assert _FakeStream.instances == []           # 未新建 SSE 流
    p._end_streaming()


def test_busy_guide_click_or_shift_enter_enqueues_guided(page):
    """P0-4：⚡ / Shift+Enter 引导发送（立即登记进对话流，不打断当前轮）。"""
    p, fake = page
    p._input.setPlainText("立即引导")
    p._begin_streaming()
    p._on_guide_clicked()
    assert _pump(lambda: fake.enqueued ==
                 [(p._session_id, "立即引导", True)]
                 and p._input.toPlainText() == "")
    p._end_streaming()


def test_guide_button_hidden_when_idle(page):
    """空闲态 ⚡ 隐藏（Shift+Enter 仍是换行），忙碌态才可见。"""
    p, _fake = page
    assert p._guide_btn.isHidden()
    p._begin_streaming()
    assert not p._guide_btn.isHidden()
    p._end_streaming()
    assert p._guide_btn.isHidden()


def test_enqueue_failure_keeps_input(page):
    """入队失败（后端拒绝）：输入框文本保留、状态栏报错。"""
    p, fake = page
    fake.queue_error = "排队过多（上限 20 条）"
    p._input.setPlainText("别丢了我")
    p._begin_streaming()
    p._on_send_clicked()
    assert _pump(lambda: "排队失败" in p._status.text())
    assert p._input.toPlainText() == "别丢了我"
    p._end_streaming()


def test_busy_controls_state_machine(page):
    """P1-3：空闲/忙碌两套 tooltip + placeholder（↑ 排队 / ⚡ 引导语义明示）。"""
    p, _fake = page
    tip = p._send_btn.toolTip()
    assert "发送" in tip and "Enter" in tip
    assert "今天帮你做些什么" in p._input.placeholderText()
    assert "Shift+Enter 换行" in p._input.placeholderText()
    p._begin_streaming()
    assert "排队" in p._send_btn.toolTip()
    assert "不打断当前生成" in p._send_btn.toolTip()
    assert "引导" in p._guide_btn.toolTip()
    assert "Shift+Enter" in p._guide_btn.toolTip()
    ph = p._input.placeholderText()
    assert "Enter 排队" in ph and "Shift+Enter 引导发出" in ph
    p._end_streaming()
    assert "发送" in p._send_btn.toolTip()
    assert "今天帮你做些什么" in p._input.placeholderText()


def test_queue_snapshot_drives_panel_visibility(page):
    """P0-5/P0-6：queue 快照驱动面板显隐与行数；空快照隐藏不占布局。"""
    from ui.widgets.queue_item import QueueItemRow
    p, _fake = page
    items = [
        {"id": "q0", "text": "排队消息0", "guided": False, "time": "09:30"},
        {"id": "g1", "text": "引导消息", "guided": True, "time": "09:35"},
        {"id": "q2", "text": "排队消息2", "guided": False, "time": "09:40"},
    ]
    p._on_event({"type": "queue", "items": items, "paused": False})
    assert p._queue_panel.isVisibleTo(p)
    rows = p._queue_body.findChildren(QueueItemRow)
    assert len(rows) == 3
    assert [r._idx.text() for r in rows] == ["1", "2", "3"]
    assert "排队中（3）" in p._queue_head_lbl.text()
    # 首条禁 ↑、末条禁 ↓（与引导行位置无关，按快照位次）
    assert not p._queue_rows["q0"]._up_btn.isEnabled()
    assert p._queue_rows["q0"]._down_btn.isEnabled()
    assert p._queue_rows["q2"]._up_btn.isEnabled()
    assert not p._queue_rows["q2"]._down_btn.isEnabled()
    # 引导行只读：⚡ 标记 + 文本，无任何操作按钮
    guided = p._queue_rows["g1"]
    assert guided._guided is True
    assert guided.findChildren(QPushButton) == []
    assert "引导消息" in guided._text.toolTip()
    # 空快照 -> 面板隐藏
    p._on_event({"type": "queue", "items": [], "paused": False})
    assert not p._queue_panel.isVisibleTo(p)
    assert p._queue_rows == {}


def test_queue_row_edit_calls_rest(page):
    """P0-3：行内 ✎ 编辑 -> 保存触发 PATCH，参数正确。"""
    from PySide6.QtWidgets import QLineEdit
    p, fake = page
    fake.q_items = _queue_items(2)      # fake 与页面快照同源（保存后自动拉回）
    p._on_event({"type": "queue", "items": _queue_items(2), "paused": False})
    row = p._queue_rows["q0"]
    row.begin_edit()
    editor = row.findChild(QLineEdit)
    assert editor is not None and editor.text() == "排队消息0"
    editor.setText("改过的消息")
    row._ok_btn.click()
    assert _pump(lambda: fake.updated ==
                 [(p._session_id, "q0", "改过的消息")])
    # 保存后快照拉回：文本已更新（单一事实来源）
    assert _pump(lambda: p._queue_rows["q0"]._full_text == "改过的消息")
    # 取消编辑不发请求
    row2 = p._queue_rows["q0"]
    row2.begin_edit()
    row2.cancel_edit()
    assert fake.updated == [(p._session_id, "q0", "改过的消息")]


def _settle_queue(p, fake):
    """等 REST 结果 + 随后的自动快照拉回都落地（防异步竞态）。"""
    return _pump(lambda: [i["id"] for i in p._queue_items] ==
                 [i["id"] for i in fake.q_items])


def test_queue_row_delete_move_and_clear_call_rest(page):
    """P0-3/P1-1：✕/↑/↓/全部清除触发对应 REST 且参数正确。"""
    p, fake = page
    fake.q_items = _queue_items(3)      # [q0, q1, q2(引导只读)]
    p._on_event({"type": "queue", "items": _queue_items(3), "paused": False})
    # ↓：q0 下移一位（同步后顺序变为 [q1, q0, q2]）
    p._queue_rows["q0"]._down_btn.click()
    assert _pump(lambda: fake.moved == [(p._session_id, "q0", 1)])
    assert _settle_queue(p, fake)
    # ↑：此时 q0 位于索引 1，其上移可用（q1 是首条，↑ 禁用）
    p._queue_rows["q0"]._up_btn.click()
    assert _pump(lambda: (p._session_id, "q0", -1) in fake.moved)
    assert _settle_queue(p, fake)
    assert [i["id"] for i in p._queue_items] == ["q0", "q1", "q2"]
    # 删除：引导行没有 ✕，删普通行 q1
    del_btn = next(b for b in p._queue_rows["q1"].findChildren(QPushButton)
                   if b.text() == "✕")
    del_btn.click()
    assert _pump(lambda: fake.deleted_items == [(p._session_id, "q1")])
    assert _settle_queue(p, fake)
    p._queue_clear_btn.click()
    assert _pump(lambda: fake.cleared == [p._session_id])
    assert _settle_queue(p, fake)


def test_queue_clear_disabled_when_only_guided(page):
    """只剩引导消息时「全部清除」禁用（引导不可清，tooltip 明示）。"""
    p, _fake = page
    items = [{"id": "g1", "text": "引导消息", "guided": True, "time": "09:40"}]
    p._on_event({"type": "queue", "items": items, "paused": False})
    assert not p._queue_clear_btn.isEnabled()
    assert "不可清除" in p._queue_clear_btn.toolTip()
    # 来一条普通消息就可清了
    p._on_event({"type": "queue", "items": items + [
        {"id": "q9", "text": "普通", "guided": False, "time": "09:41"}],
        "paused": False})
    assert p._queue_clear_btn.isEnabled()


def test_guided_event_renders_bubble_with_time(page):
    """P0-4/P1-2：guided -> 对话流末尾 user 气泡 + HH:MM 时间 + StepLine。"""
    from PySide6.QtWidgets import QLabel
    p, _fake = page
    p._begin_streaming()
    p._on_event({"type": "guided", "id": "q1", "text": "先看这个",
                 "time": "09:47"})
    bubbles = [w for w in p._col.findChildren(ChatBubble)
               if w.role == "user"]
    assert bubbles and bubbles[-1].label.text() == "先看这个"
    assert bubbles[-1].time_text == "09:47"
    time_lbls = bubbles[-1].findChildren(QLabel, "BubbleTime")
    assert time_lbls and time_lbls[0].text() == "09:47"
    # 文本可选中复制（既有 TextSelectableByMouse）
    from PySide6.QtCore import Qt
    assert bubbles[-1].label.textInteractionFlags() & \
        Qt.TextSelectableByMouse
    p._end_streaming()


def test_dequeued_queue_adds_bubble_guided_not_repeated(page):
    """P0-2：dequeued(mode=queue) 补用户气泡；guided 不重复气泡。"""
    p, _fake = page
    p._begin_streaming()
    p._on_event({"type": "guided", "id": "q1", "text": "引导消息",
                 "time": "09:47"})
    n_user = len([w for w in p._col.findChildren(ChatBubble)
                  if w.role == "user"])
    p._on_event({"type": "dequeued", "id": "q1", "text": "引导消息",
                 "mode": "guided", "time": "09:47"})
    assert len([w for w in p._col.findChildren(ChatBubble)
                if w.role == "user"]) == n_user
    p._on_event({"type": "dequeued", "id": "q2", "text": "排队消息",
                 "mode": "queue", "time": "09:48"})
    bubbles = [w for w in p._col.findChildren(ChatBubble)
               if w.role == "user"]
    assert len(bubbles) == n_user + 1
    assert bubbles[-1].label.text() == "排队消息"
    assert bubbles[-1].time_text == ""       # 排队消息不带时间标签
    p._end_streaming()


def test_done_keeps_busy_until_end(page):
    """P0-2 前端关键点：done 只收尾单轮气泡，end 才解除忙碌态。"""
    p, _fake = page
    p._begin_streaming()
    p._on_event({"type": "done", "content": "第一轮答复"})
    assert p._busy is True
    assert "完成" in p._status.text()
    p._on_event({"type": "end", "content": "第一轮答复", "error": "",
                 "paused": False})
    assert p._busy is False
    assert "就绪" in p._status.text()


def test_queue_state_paused_toggles_resume_button(page):
    """P0-6：queue_state 暂停/恢复 -> StepLine + 「继续」按钮显隐切换。"""
    p, _fake = page
    p._apply_queue(_queue_items(2), False)
    p._on_event({"type": "queue_state", "paused": True, "reason": "stopped"})
    assert p._queue_paused is True
    assert p._queue_resume_btn.isVisibleTo(p)
    p._on_event({"type": "queue_state", "paused": False})
    assert p._queue_paused is False
    assert not p._queue_resume_btn.isVisibleTo(p)


def test_end_with_paused_keeps_queue_and_shows_resume(page):
    """停止后续跑被暂停：end(paused=true) 后队列保留、「继续」可见。"""
    p, _fake = page
    p._begin_streaming()
    p._apply_queue(_queue_items(2), False)
    p._on_event({"type": "end", "content": "", "error": "", "paused": True})
    assert p._busy is False
    assert "继续" in p._status.text()
    assert p._queue_panel.isVisibleTo(p)
    assert p._queue_resume_btn.isVisibleTo(p)


def test_stop_pauses_then_resume_starts_new_stream(page, monkeypatch):
    """时序图 4：停止 -> 拉快照保留暂停态 -> 「继续」发 mode=resume 新流。"""
    from ui.pages import assistant_page as mod
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, fake = page
    fake.q_items = [{"id": "q9", "text": "留着", "guided": False,
                     "time": "10:00"}]
    fake.q_paused = True
    p._session_id = "s1"
    p._begin_streaming()
    p._stop()
    assert _pump(lambda: p._queue_resume_btn.isVisibleTo(p))
    assert not p._busy
    assert "已停止" in p._status.text()
    # 点「继续」-> 新 SSE 流（mode=resume）并重新进入忙碌态
    _FakeStream.instances.clear()
    p._resume_queue()
    assert _pump(lambda: _FakeStream.instances and
                 _FakeStream.instances[-1].payload.get("mode") == "resume")
    assert _FakeStream.instances[-1].payload["session_id"] == "s1"
    assert p._busy is True
    p._end_streaming()


def test_resume_with_empty_queue_reports(page):
    """空队列点「继续」：不新建流，状态栏说明。"""
    p, _fake = page
    p._session_id = "s1"
    p._apply_queue([], False)
    p._resume_queue()
    assert "队列是空的" in p._status.text()
    assert p._busy is False


def test_switch_task_resets_and_fetches_queue(page):
    """切换任务：队列面板先重置，再按新 sid 拉快照（不串任务）。"""
    p, fake = page
    p._apply_queue(_queue_items(2), True)
    assert p._queue_panel.isVisibleTo(p)
    p._apply_session_detail({"id": "sX", "workspace": r"E://tmp//ws",
                             "messages": []})
    assert _pump(lambda: p._session_id == "sX")
    assert not p._queue_panel.isVisibleTo(p)     # 旧任务队列已清
    assert _pump(lambda: fake.queue_calls
                 and fake.queue_calls[-1] == "sX")


# ---------------------------------------------------------------------------
# 1.0.9：模型菜单「免费」标识 + 悬停提示 / reset 事件 / 工具页刷新
# ---------------------------------------------------------------------------
def _menu_actions(menu):
    """递归收集菜单（含子菜单）里的 action。"""
    out = []
    for act in menu.actions():
        sub = act.menu()
        if sub is not None:
            out.extend(_menu_actions(sub))
        else:
            out.append(act)
    return out


def test_model_menu_marks_free_and_has_tooltip(page):
    """线上免费模型标「免费」；每项都带「能力 / 收费」悬停提示。"""
    p, _fake = page
    p._active_provider = "siliconflow"
    p._model_cache["siliconflow"] = ["Qwen/Qwen3-8B",
                                     "deepseek-ai/DeepSeek-V4-Flash"]
    menu = p._build_model_menu()
    assert menu.toolTipsVisible() is True

    by_text = {a.text().strip(): a for a in _menu_actions(menu)}
    free = [t for t in by_text if t.startswith("Qwen/Qwen3-8B")]
    assert free, "菜单里找不到 Qwen/Qwen3-8B"
    assert "免费" in free[0]
    tip = by_text[free[0]].toolTip()
    assert "能力：" in tip and "收费：" in tip

    paid = [t for t in by_text if t.startswith("deepseek-ai/DeepSeek-V4-Flash")]
    assert paid and "免费" not in paid[0]
    assert "按量计费" in by_text[paid[0]].toolTip()


def test_model_menu_provider_has_tooltip(page):
    """模型来源子项也要有悬停说明（类型 / 端点 / 默认模型）。"""
    p, _fake = page
    menu = p._build_model_menu()
    acts = [a for a in _menu_actions(menu) if a.text() in ("硅基流动", "本地模型")]
    assert acts
    assert any("类型：" in a.toolTip() for a in acts)


def test_reset_event_clears_partial_bubble(page):
    """provider 自动放大 max_tokens 重试时下发 reset：半截内容要清掉。"""
    p, _fake = page
    p._on_event({"type": "delta", "text": "半截正文"})
    p._on_event({"type": "reasoning", "text": "半截思考"})
    p._on_event({"type": "reset"})
    assert p._bubble is not None and p._bubble.label.text() == ""
    if p._thinking is not None:
        assert p._thinking.body.text() == ""


def test_tools_tab_refresh_pulls_state_and_prefs(page):
    """「⟳ 刷新」必须同时重拉 state 与 tool prefs（旧实现只拉 prefs）。"""
    from ui.pages import assistant_page as mod
    p, fake = page
    # 工具列表在清单中心弹窗里懒创建：先建弹窗才有 _tool_list
    mod.ManagerDialog(p)
    before = fake.tool_prefs_calls
    p._refresh_tools()
    assert _pump(lambda: fake.tool_prefs_calls > before)
    assert _pump(lambda: "工具清单已刷新" in p._status.text())
    assert p._tool_list.count() >= 2


def test_tool_list_fit_does_not_crash_without_dialog(page):
    """弹窗未打开时 _fit_tools_list 直接返回（不能在无父窗口时调尺寸）。"""
    p, _fake = page
    p._fill_tools(p._state.get("tools") or [])
    p._fit_tools_list()      # 不应抛异常


def test_siliconflow_key_sync_only_once(page):
    """1.0.9 修复 PUT settings 死循环：state 接口不下发 api_key（后端脱敏），
    所以 `not provider["api_key"]` 恒为真。原实现每次 `_fill_models("siliconflow")`
    都回写对话页 Key，而回写成功后又 refresh() → _apply_state → _fill_models，
    形成每 ~1.6s 一轮的 save→refresh 死循环（审计日志被 providers 条目刷满、
    后台线程不停创建）。现在本页生命周期内只同步一次。"""
    p, fake = page

    class _StubSettings:
        def __init__(self, data):
            self._data = data

        def value(self, key, default=None):
            return self._data.get(key, default)

    p._settings = _StubSettings({"chat/cloud_api_key": "sk-test-123",
                                 "chat/cloud_models": "[]"})
    p._active_provider = "siliconflow"
    p._sf_key_synced = False
    before = len(fake.save_calls)

    for _ in range(3):                      # 模拟三轮 refresh
        p._fill_models("siliconflow")
        _pump(lambda: False, timeout=0.15)  # 放行 Worker

    key_saves = [c for c in fake.save_calls[before:]
                 if any(pr.get("api_key") == "sk-test-123"
                        for pr in (c.get("providers") or [])
                        if isinstance(pr, dict))]
    assert len(key_saves) == 1, f"Key 回写应只发生一次，实际 {len(key_saves)} 次"
    assert p._sf_key_synced is True


# ---------------------------------------------------------------------------
# 1.0.10 免费/收费标识 + 免 Key 端点
# ---------------------------------------------------------------------------
def test_model_menu_shows_price_tag_for_paid_model(page):
    """收费模型在菜单里带真实单价，免费模型标「免费」。

    用户反馈「Qwen3-14B 收费却标免费，我看这个不是免费的呀」——修法是把
    「免费」改成由官方价格页推导，并在菜单里直接显示单价。
    """
    p, _fake = page
    p._active_provider = "siliconflow"
    p._model_cache["siliconflow"] = ["Qwen/Qwen3-8B", "Qwen/Qwen3-14B",
                                     "some-vendor/unknown-model"]
    menu = p._build_model_menu()
    texts = [a.text() for a in menu.actions()]
    free_item = next(t for t in texts if t.startswith("Qwen/Qwen3-8B"))
    paid_item = next(t for t in texts if t.startswith("Qwen/Qwen3-14B"))
    assert "免费" in free_item
    assert "免费" not in paid_item
    assert "¥0.5" in paid_item and "2" in paid_item
    # 价格未收录：既不标免费也不编价格
    unknown = next(t for t in texts if t.startswith("some-vendor/unknown-model"))
    assert "免费" not in unknown and "¥" not in unknown


def test_model_menu_has_pricing_refresh_entry(page):
    """菜单里有「更新价格与免费标识…」入口（服务商调价后不用等新版本）。"""
    p, _fake = page
    menu = p._build_model_menu()
    texts = [a.text() for a in menu.actions() if a.text()]
    assert "更新价格与免费标识…" in texts
    assert "检测本机免 Key 端点…" in texts


def test_refresh_pricing_result_reports_counts(page, monkeypatch):
    """价格刷新回来后在状态栏给「N 个模型 / M 个免费 + 时间」，不静默。"""
    p, _fake = page
    p._apply_pricing_result({"count": 62, "free": ["a", "b"],
                             "fetched_at": "2026-10-26 16:00"})
    msg = p._status.text()
    assert "62" in msg and "2" in msg and "2026-10-26 16:00" in msg


def test_keyless_provider_shows_no_key_needed(page):
    """免 Key 提供商：来源菜单标「（免 Key）」，提示里写「无需 API Key」。"""
    p, _fake = page
    provs = p._state.setdefault("providers", [])
    provs.append({"id": "pollinations", "label": "Pollinations 免 Key",
                  "kind": "openai", "enabled": True, "keyless": True,
                  "base_url": "https://text.pollinations.ai",
                  "model": "openai-fast", "has_key": False})
    menu = p._build_model_menu()
    src = next(a for a in menu.actions() if a.text() == "模型来源")
    labels = [a.text() for a in src.menu().actions()]
    assert any("Pollinations" in t and "免 Key" in t for t in labels)
    tip = p._provider_tooltip(provs[-1])
    assert "无需 API Key" in tip
    assert "尚未配置 API Key" not in tip


def test_keyless_models_labelled_in_menu(page):
    """免 Key 来源下，模型项标「免 Key」而不是「免费 / 价格」。"""
    p, _fake = page
    p._active_provider = "pollinations"
    p._state.setdefault("providers", []).append(
        {"id": "pollinations", "label": "Pollinations 免 Key", "kind": "openai",
         "enabled": True, "keyless": True, "model": "openai-fast"})
    p._model_cache["pollinations"] = ["openai-fast"]
    menu = p._build_model_menu()
    item = next(a.text() for a in menu.actions() if a.text().startswith("openai-fast"))
    assert "免 Key" in item


def test_detect_local_endpoints_none_gives_actionable_hint(page, monkeypatch):
    """本机没装/没启动 → 状态栏给出可执行提示，而不是静默。"""
    p, _fake = page
    p._apply_local_endpoints([])
    msg = p._status.text()
    assert "未发现" in msg and ("Ollama" in msg or "ollama" in msg)


def test_detect_local_endpoints_saves_into_providers(page):
    """发现本机端点 → 以同一次 save 写入 enabled/base_url/model（免 Key）。"""
    p, fake = page
    before = len(fake.save_calls)
    p._apply_local_endpoints([
        {"id": "ollama", "label": "Ollama", "base_url": "http://127.0.0.1:11434/v1",
         "models": ["qwen3:4b", "llama3:8b"]}])
    assert _pump(lambda: len(fake.save_calls) > before)
    saved = fake.save_calls[-1]
    provs = {x["id"]: x for x in saved.get("providers") or []}
    assert provs["ollama"]["enabled"] is True
    assert provs["ollama"]["keyless"] is True
    assert provs["ollama"]["model"] == "qwen3:4b"


# ---------------------------------------------------------------------------
# 1.0.11：粘贴图片（截图直接进输入框）
# ---------------------------------------------------------------------------
def _clear_stream_instances():
    _FakeStream.instances.clear()


def _paste_image(p):
    """模拟剪贴板里有一张截图后按 Ctrl+V（走 QTextEdit 的 MIME 入口）。"""
    from PySide6.QtCore import QMimeData
    from PySide6.QtGui import QImage

    img = QImage(8, 6, QImage.Format_RGB32)
    img.fill(0xFF3366CC)
    md = QMimeData()
    md.setImageData(img)
    p._input.insertFromMimeData(md)


def test_paste_image_saves_file_and_adds_chip(page, tmp_path, monkeypatch):
    """Ctrl+V 一张截图 → 落盘成 PNG + 输入框上方出现缩略图条。"""
    from ui.widgets import image_composer as ic

    monkeypatch.setattr(ic, "attachments_dir", lambda: str(tmp_path))
    p, _fake = page
    assert not p._attach_bar.isVisible() or p._attach_bar.count() == 0

    _paste_image(p)

    paths = p._attach_bar.paths()
    assert len(paths) == 1
    assert os.path.isfile(paths[0])
    assert paths[0].lower().endswith(".png")
    assert "已添加图片" in p._status.text()


def test_paste_same_image_twice_keeps_one_chip(page, tmp_path, monkeypatch):
    """同一张图重复粘贴只留一张（否则发出去会是重复附件）。"""
    from ui.widgets import image_composer as ic

    monkeypatch.setattr(ic, "attachments_dir", lambda: str(tmp_path))
    p, _fake = page
    path = os.path.join(str(tmp_path), "one.png")
    from PySide6.QtGui import QImage
    QImage(4, 4, QImage.Format_RGB32).save(path, "PNG")

    assert p._attach_bar.add(path) is True
    assert p._attach_bar.add(path) is False
    assert p._attach_bar.count() == 1


def test_attachment_chip_can_be_removed(page, tmp_path):
    p, _fake = page
    from PySide6.QtGui import QImage
    path = os.path.join(str(tmp_path), "x.png")
    QImage(4, 4, QImage.Format_RGB32).save(path, "PNG")
    p._attach_bar.add(path)
    assert p._attach_bar.count() == 1
    p._attach_bar.remove(path)
    assert p._attach_bar.count() == 0


def test_send_with_pasted_image_carries_attachment_payload(page, monkeypatch,
                                                          tmp_path):
    """发送时 attachments 进 payload、气泡下出现缩略图、附件条清空。"""
    from ui.pages import assistant_page as mod
    from PySide6.QtGui import QImage

    _clear_stream_instances()
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, _fake = page
    path = os.path.join(str(tmp_path), "shot.png")
    QImage(4, 4, QImage.Format_RGB32).save(path, "PNG")

    p._input.setPlainText("看这张图")
    p._attach_bar.add(path)
    p._on_send_clicked()

    assert _FakeStream.instances, "没有发起对话流"
    assert _FakeStream.instances[-1].payload.get("attachments") == [path]
    assert p._attach_bar.count() == 0        # 发出去之后就清空
    # 对话区里应该有缩略图行（AttachThumb 的 QLabel 带 imagePath 属性）
    thumbs = [w for w in p._col.findChildren(QLabel)
              if w.property("imagePath") == path]
    assert thumbs, "气泡下方没有渲染缩略图"


def test_send_image_only_uses_placeholder_text(page, monkeypatch, tmp_path):
    """只贴图不打字也能发：正文用占位说明，不会因为「消息为空」发不出去。"""
    from ui.pages import assistant_page as mod
    from PySide6.QtGui import QImage

    _clear_stream_instances()
    monkeypatch.setattr(mod, "AssistantStream", _FakeStream)
    p, _fake = page
    path = os.path.join(str(tmp_path), "only.png")
    QImage(4, 4, QImage.Format_RGB32).save(path, "PNG")

    p._input.clear()
    p._attach_bar.add(path)
    p._on_send_clicked()

    assert _FakeStream.instances
    payload = _FakeStream.instances[-1].payload
    assert payload["message"].strip()          # 非空
    assert payload.get("attachments") == [path]


def test_enqueue_with_image_keeps_path_in_text(page):
    """忙碌时贴图入队：队列条目没有附件字段，路径必须拼进正文，不能丢。"""
    p, fake = page
    p._busy = True
    path = os.path.join(os.path.dirname(__file__), "..", "resources", "llmbuddy.png")
    path = os.path.abspath(path)
    p._attach_bar.add(path)
    p._input.setPlainText("排队这条带图")
    p._on_send_clicked()

    assert _pump(lambda: bool(fake.enqueued))
    _sid, text, _guided = fake.enqueued[-1]
    assert "排队这条带图" in text
    assert path in text


# ---------------------------------------------------------------------------
# 1.0.11：上下文占用 + 「上下文已压缩」提示
# ---------------------------------------------------------------------------
def test_context_event_updates_meter(page):
    """后端每轮上报的使用量 → 底部「上下文 33% · 2.6k/8k」。"""
    p, _fake = page
    p._apply_context({"used": 2640, "limit": 8000, "ratio": 0.33,
                      "compressed": False})
    text = p._ctx_label.text()
    assert "上下文" in text and "33%" in text
    assert "2.6k" in text and "8.0k" in text


def test_context_meter_marks_hot_when_near_full(page):
    p, _fake = page
    p._apply_context({"used": 7000, "limit": 8000, "ratio": 0.875})
    assert bool(p._ctx_label.property("hot")) is True
    p._apply_context({"used": 800, "limit": 8000, "ratio": 0.1})
    assert bool(p._ctx_label.property("hot")) is False


def test_context_compressed_event_renders_line(page):
    """压缩发生 → 对话流插一行「✓ 上下文已压缩」。"""
    p, _fake = page
    p._handle_event({"type": "context", "used": 1800, "limit": 4096,
                     "ratio": 0.44, "compressed": True, "folded": 7,
                     "saved": 5200, "via": "llm"})
    texts = [w.text() for w in p._msg_layout.parentWidget().findChildren(QLabel)]
    assert any("上下文已压缩" in t for t in texts)
    assert any("折叠 7 条" in t for t in texts)


def test_compact_start_shows_progress_step(page):
    p, _fake = page
    p._handle_event({"type": "compact", "phase": "start",
                     "message": "上下文接近上限，正在压缩较早的对话…"})
    texts = [w.text() for w in p._msg_layout.parentWidget().findChildren(QLabel)]
    assert any("正在压缩" in t for t in texts)


def test_context_usage_shown_when_loading_old_session(page):
    """切到旧任务时用会话详情里的 context_usage 显示占用（不必等再发一条）。"""
    p, _fake = page
    p._apply_session_detail({
        "id": "s-old", "workspace": r"E://tmp//ws",
        "messages": [{"role": "user", "content": "你好"},
                     {"role": "assistant", "content": "在"}],
        "context_usage": {"used": 1200, "limit": 8192, "ratio": 0.15,
                          "compressed": False}})
    assert "15%" in p._ctx_label.text()


def test_history_renders_pasted_image_thumbs(page):
    """回看历史：message 上的 images 要渲染成缩略图。"""
    p, _fake = page
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "resources", "llmbuddy.png"))
    p._apply_session_detail({
        "id": "s-img", "workspace": r"E://tmp//ws",
        "messages": [{"role": "user", "content": "看图", "images": [path]}]})
    thumbs = [w for w in p._col.findChildren(QLabel)
              if w.property("imagePath") == path]
    assert thumbs


# ---------------------------------------------------------------------------
# 1.0.11：输出时自动跟随到底部
# ---------------------------------------------------------------------------
def test_scroll_follows_on_range_change(page):
    """量程变化（内容变高）就贴底——这是「不用手动滚动」的核心契约。

    只验证信号契约，不去依赖无头平台的真实布局（offscreen 下视口高度固定，
    内容再长 maximum 也涨不起来，那种断言测的是平台不是代码）。
    """
    p, _fake = page
    bar = p._scroll.verticalScrollBar()
    p._follow = True
    bar.setRange(0, 500)
    bar.setRange(0, 900)          # 新内容撑高
    assert bar.value() == 900


def test_user_scrolling_up_stops_following(page):
    """用户主动往上翻过就不再硬拽回底部（看历史时不打扰）。"""
    p, _fake = page
    bar = p._scroll.verticalScrollBar()
    p._follow = True
    bar.setRange(0, 500)
    bar.setValue(0)               # 用户滚到顶部 → valueChanged → _follow=False
    assert p._follow is False
    bar.setRange(0, 900)          # 又来新内容
    assert bar.value() == 0       # 没有被拽下去


def test_force_scroll_bottom_after_send(page):
    """刚发消息时强制回到底部（哪怕用户之前在看历史）。"""
    p, _fake = page
    bar = p._scroll.verticalScrollBar()
    bar.setRange(0, 500)
    bar.setValue(0)
    assert p._follow is False
    p._scroll_bottom(force=True)
    p._do_scroll_bottom()
    assert bar.value() == 500
    assert p._follow is True


# ---------------------------------------------------------------------------
# 1.0.11：字号档位（参考 WorkBuddy）
# ---------------------------------------------------------------------------
def test_assistant_fonts_are_scaled_up():
    """正文 15px / 步骤行 12px：字号档位段必须存在且生效于助理界面选择器。"""
    from core.theme import read_base_stylesheet

    qss = read_base_stylesheet()
    assert "#UserBubble, #BotBubble { font-size: 15px; }" in qss
    assert "#StepLine { font-size: 12px; }" in qss
    assert "#NavButton { font-size: 15px; }" in qss
    assert "#Composer QTextEdit#ComposerInput" in qss
    # 字号覆盖段必须在文件末尾（后面定义的规则才压得住前面的旧值）
    assert qss.rindex("#PageTitle { font-size: 20px; }") > len(qss) // 2


def test_loading_compressed_session_adds_only_one_line(page):
    """切到已压缩的旧任务：压缩提示只出现一次（详情行 + usage 不重复插）。"""
    p, _fake = page
    p._apply_session_detail({
        "id": "s-c", "workspace": r"E://tmp//ws",
        "messages": [{"role": "user", "content": "你好"}],
        "context": {"summary": "之前的要点", "compressed": 9, "via": "llm"},
        "context_usage": {"used": 900, "limit": 8192, "ratio": 0.11,
                          "compressed": True, "compressed_count": 9}})
    labels = [w.text() for w in p._col.findChildren(QLabel)]
    assert sum("上下文已压缩" in t for t in labels) == 1


# ---------------------------------------------------------------------------
# 1.0.11：队列快照乱序防御
# ---------------------------------------------------------------------------
def test_stale_queue_snapshot_is_dropped(page):
    """旧快照晚到不能覆盖新快照（曾导致「编辑队列后面板突然空了」的偶发失败）。"""
    p, _fake = page
    p._on_event({"type": "queue", "items": _queue_items(2), "paused": False})
    assert set(p._queue_rows) == {"q0", "q1"}
    p._queue_seq = int(p._queue_seq) + 1          # 又有一次更新的请求发出
    p._on_queue_fetched({"items": []}, 1, p._session_id)   # seq 更小的旧结果
    assert set(p._queue_rows) == {"q0", "q1"}


def test_queue_snapshot_from_other_session_is_dropped(page):
    """切了任务之后，旧任务的队列快照不许画到新任务上。"""
    p, _fake = page
    p._on_event({"type": "queue", "items": _queue_items(2), "paused": False})
    p._on_queue_fetched({"items": []}, p._queue_seq, "another-session")
    assert set(p._queue_rows) == {"q0", "q1"}
