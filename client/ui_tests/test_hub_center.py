"""v1.0.8「技能 / 工具 / MCP 清单中心」客户端验收测试（T03 + T04）。

对应文档：
- `docs/智能助理-技能工具MCP清单-架构设计.md` §4（底栏宽度门禁）、§6（StepLine 裁定）、§8（REST 表）
- `docs/智能助理-技能工具MCP清单-任务分解.md` T03 / T04 验收条目

这些用例刻意独立于 `test_assistant_page.py`：那边验证既有行为不被破坏，
这边验证 1.0.8 新增的四入口、宽度门禁、自动加载提示与本地目录导入。
"""
from __future__ import annotations

import inspect
import os
import sys
import time

# 让 `ui.*` 可导入（与其它 ui_tests 一致）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from ui.widgets import assistant_cards as cards_mod  # noqa: E402
from ui.widgets.assistant_cards import AutoLoadPrompt, StepLine  # noqa: E402
from ui.widgets.online_catalog_dialog import OnlineMcpDialog  # noqa: E402
from ui.pages import assistant_page as mod  # noqa: E402
from ui.pages.assistant_page import (  # noqa: E402
    CATALOG_CLASS_BADGE, CATALOG_HIDDEN_CLASSES, MCP_COMMAND_WHITELIST,
    ManagerDialog, _ellipsis, parse_mcp_json)

app = QApplication.instance() or QApplication(sys.argv)

#: 底栏宽度门禁：单一来源——直接取被测页面类的常量，禁止在测试里硬编码
#: 字面量（否则实现悄悄改预算、测试仍按旧值放行/误报，二者漂移）。
#: 预算口径（2026-06 重算）：主窗口最小宽 1000 → 左栏 200 → 右栏最窄约 770；
#: 空底栏实测 542 / 忙碌态 614，均 ≤720 门禁。测试改用「真实最窄面板 770」
#: 的实测可用宽（composer.width-16）做硬断言，BAR_BUDGET_W 仅作文档锚点。
BAR_BUDGET_W = mod.AssistantPage.BAR_BUDGET_W


# --------------------------------------------------------------------------- #
# 极简假对象（只提供页面用到的字段/方法）
# --------------------------------------------------------------------------- #
class _Cfg:
    """配置桩：助理工作区 + 保存计数。"""

    def __init__(self) -> None:
        self.assistant_workspace = ""
        self.saved = 0
        self.api_key = ""

    def gateway_url(self) -> str:
        return "http://127.0.0.1:8000"

    def save(self) -> None:
        self.saved += 1


class _Api:
    """模型列表桩。"""

    def list_models(self):
        return [{"id": "qwen3-4b", "loaded": True}]


class _FakeAssistApi:
    """AssistantApi 桩：覆盖 §8 表里 12 个清单中心接口 + 页面基础接口。"""

    def __init__(self, *a, **kw) -> None:
        self.saved = {}
        self.save_calls = []
        self.added_mcp = []
        self.tool_prefs_calls = 0
        self.saved_prefs = []
        self.profiles_calls = 0
        self.saved_profiles = []
        self.catalog_refreshed = 0
        self.installed_dir = []
        self.installed_url = []
        self.catalog_fail = False
        #: GET /tools/prefs 返回（架构 §8）
        self.tool_prefs_data = {
            "disabled": ["shell_run"], "max_tools": 24, "total": 6,
            "active": 5, "over": [],
        }
        #: GET /profiles 返回
        self.profiles_data = {
            "auto_load": True,
            "profiles": [
                {"id": "file-organize", "name": "文件整理",
                 "skills": ["workspace-organizer"], "tools": 7},
            ],
        }
        #: GET /catalog/mcp 返回（dict + items；free_class 由后端分类）
        self.catalog_mcp_data = {
            "items": [
                {"registry_name": "filesystem", "title": "Filesystem",
                 "transport": "stdio", "version": "1.0",
                 "description": "本地文件读写", "free_class": "zero_config"},
            ],
            "fetched_at": 1.0, "stale": False, "error": "",
        }
        #: GET /catalog/repos 返回
        self.catalog_repos_data = {
            "items": [
                {"full_name": "acme/awesome-skills", "stars": 120,
                 "description": "技能合集", "default_branch": "main"},
            ],
            "fetched_at": 1.0, "stale": False, "error": "",
        }

    # -------- 页面基础 --------
    def state(self):
        return {
            "session_id": "s1",
            "workspace": "",
            "policy": "readonly_auto",
            "model": "qwen3-4b",
            "skills": [
                {"slug": "repo-digest", "name": "仓库摘要", "version": "1.2",
                 "description": "把仓库结构摘要成一段说明", "source": "builtin"},
                {"slug": "workspace-organizer", "name": "文件整理",
                 "version": "1.0", "description": "按类型整理工作区文件",
                 "source": "builtin"},
            ],
            "tools": [
                {"name": "fs_list", "description": "列目录", "enabled": True,
                 "source": "builtin", "risk": "low"},
                {"name": "shell_run", "description": "执行命令",
                 "enabled": False, "source": "builtin", "risk": "high"},
                {"name": "mcp__filesystem__read", "description": "读文件",
                 "enabled": True, "source": "mcp", "risk": "low"},
            ],
            "mcp": [
                {"id": "filesystem", "name": "Filesystem",
                 "transport": "stdio", "enabled": True,
                 "status": "connected", "tool_count": 12},
                {"id": "github", "name": "GitHub", "transport": "http",
                 "enabled": False, "status": "disconnected", "tool_count": 0},
            ],
            "profiles": [],
        }

    def sessions(self):
        return [{"id": "s1", "title": "历史任务", "message_count": 2,
                 "workspace": "", "pinned": False, "archived": False,
                 "created_at": 1.0, "updated_at": 2.0, "preview": "你好"}]

    def spaces(self):
        # 清单中心用例不测空间：返回空列表（与默认工作区等价）。
        return []

    def new_session(self, *a, **kw):
        def _make():
            return {"id": "s-new", "title": "新任务", "message_count": 0}
        return _make

    def session(self, sid):
        return self.sessions()[0]

    def delete_session(self, *a, **kw):
        return {"ok": True}

    def update_session(self, *a, **kw):
        return {"ok": True}

    def rename_session(self, *a, **kw):
        return {"ok": True}

    def queue(self, session_id=None):
        return {"items": []}

    def audit(self):
        return {"items": []}

    def save(self, patch):
        self.save_calls.append(patch)
        self.saved.update(patch)

    def save_settings(self, *a, **kw):
        return {"ok": True}

    def stream(self, *a, **kw):
        raise AssertionError("本测试不应发起真实流式请求")

    def stop(self, *a, **kw):
        return {"ok": True}

    def connect_mcp(self, *a, **kw):
        return {"ok": True}

    def disconnect_mcp(self, *a, **kw):
        return {"ok": True}

    def delete_mcp(self, *a, **kw):
        return {"ok": True}

    # -------- §8 清单中心 12 个接口 --------
    def tool_prefs(self):
        self.tool_prefs_calls += 1
        return dict(self.tool_prefs_data)

    def save_tool_prefs(self, disabled, max_tools):
        self.saved_prefs.append((list(disabled), max_tools))
        return {"ok": True, "active": max(0, 6 - len(disabled)),
                "over": [], "total": 6, "max_tools": max_tools,
                "disabled": list(disabled)}

    def profiles(self):
        self.profiles_calls += 1
        return dict(self.profiles_data)

    def save_profiles(self, auto_load, enabled):
        self.saved_profiles.append((auto_load, enabled))
        return {"ok": True, "auto_load": auto_load}

    def catalog_mcp(self):
        if self.catalog_fail:
            raise RuntimeError("在线目录不可用")
        return {**dict(self.catalog_mcp_data),
                "items": [dict(x) for x in self.catalog_mcp_data["items"]]}

    def catalog_repos(self):
        if self.catalog_fail:
            raise RuntimeError("在线目录不可用")
        return {**dict(self.catalog_repos_data),
                "items": [dict(x) for x in self.catalog_repos_data["items"]]}

    def catalog_repo_skills(self, repo_id):
        if self.catalog_fail:
            raise RuntimeError("在线目录不可用")
        return [{"id": "skill-a", "name": "技能 A", "version": "1.0",
                 "description": "演示技能", "risk": "medium", "files": 2}]

    def catalog_refresh(self):
        self.catalog_refreshed += 1
        return {"ok": True}

    def catalog_install_skill(self, ident):
        self.installed_url.append(ident)
        return {"ok": True, "slug": "skill-a"}

    def catalog_install_mcp(self, config):
        self.installed_dir.append(config)
        return {"ok": True, "id": config.get("id")}

    def catalog_from_dir(self, path):
        self.installed_dir.append(path)
        return {"ok": True, "slug": "from-dir"}

    def catalog_from_url(self, url):
        self.installed_url.append(url)
        return {"ok": True, "slug": "from-url"}

    def add_mcp(self, config):
        self.added_mcp.append(config)
        return {"ok": True}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def page(monkeypatch):
    """构造一个 AssistantPage，底栏按逻辑 720×680 摆好。"""
    fake = _FakeAssistApi()
    monkeypatch.setattr(mod, "AssistantApi", lambda *a, **kw: fake)
    p = mod.AssistantPage(_Api(), None, _Cfg())
    p.build_nav_panel()
    p.refresh()
    assert _pump(lambda: bool(p._state), 3.0)
    p.resize(720, 680)
    p.show()
    _settle()
    yield p, fake
    p.close()
    p.deleteLater()
    _settle()


def _settle() -> None:
    """跑两轮事件循环，让 layout / polish 生效。"""
    for _ in range(2):
        app.processEvents()
        time.sleep(0.01)


def _pump(cond, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def _menu_texts(menu) -> list:
    """已启用菜单项文案（跳过分隔符）。"""
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def _enabled_menu_texts(menu) -> list:
    return [a.text() for a in menu.actions()
            if not a.isSeparator() and a.isEnabled()]


# --------------------------------------------------------------------------- #
# T04 ①：底栏宽度门禁（架构 §4 —— 必须实测，不能靠隐藏）
# --------------------------------------------------------------------------- #
def _bar_width(p) -> int:
    """底栏真实需要宽度。

    `QHBoxLayout.sizeHint()` 会把子控件的 `sizeHint()` 相加，而 QPushButton
    的 `sizeHint()` 在全局 QSS 生效后会被字体 + padding 撑大（跨测试文件时
    QApplication 上的样式表已被 polish，导致同一控件在不同用例里读数不同）。
    真正决定「会不会溢出」的是各控件 **实际分配宽度** 之和，所以这里取
    `widget().width()` 的总和 —— 那才是用户在 720 窗口里看到的样子。
    """
    lay = p._bar_lay
    total = 0
    for i in range(lay.count()):
        w = lay.itemAt(i).widget()
        if w is not None and not w.isHidden() and w.isVisibleTo(p):
            total += w.width()
    total += max(0, lay.count() - 1) * max(0, lay.spacing())
    return total


def test_bottom_bar_fits_at_min_window(page):
    """App 最小宽 1000 → 右栏 ~770：空闲态底栏 ≤ 实际可用宽，且全部按钮恒可见带文字。

    按用户要求底栏去掉「📝 提示词」（提示词库入口改到输入框右键），
    ✨ 增强提示词挪到上下文占用左边。空底栏因此比 542 更窄。
    """
    p, _fake = page
    p.resize(770, 680)
    p._skill_slugs = []
    p._refresh_skill_chips()
    _settle()
    w = _bar_width(p)
    avail = p._composer.width() - 16
    assert w <= avail, f"空闲态底栏 {w}px 超实际可用宽 {avail}px"
    assert w <= BAR_BUDGET_W, f"空闲态底栏 {w}px 超门禁 {BAR_BUDGET_W}px"
    assert w <= BAR_BUDGET_W + 80, "空闲态底栏宽得离谱，疑似布局损坏"
    # 全部常驻功能按钮：恒可见 + 带文字（规则：绝不按宽度隐藏、不带空文案）
    for btn in (p._attach_btn, p._ws_btn, p._policy_btn, p._skill_btn,
                p._tool_btn, p._mcp_btn, p._model_btn,
                p._send_btn, p._enhance_btn):
        assert btn.isVisibleTo(p) and not btn.isHidden(), btn.objectName()
        assert btn.text().strip(), f"{btn.objectName()} 无文字"
    # 底栏不再有「提示词」按钮（功能仍在，入口改到输入框右键）
    assert not hasattr(p, "_prompt_btn")
    # ✨ 在上下文占用左边（用户指定位置）
    lay = p._bar_lay
    assert lay.indexOf(p._enhance_btn) >= 0
    assert lay.indexOf(p._enhance_btn) < lay.indexOf(p._ctx_meter)


def test_bottom_bar_fits_in_busy_state(page):
    """忙碌态多出 运行指示 + 停止 + 引导 三按钮，仍 ≤ 实际可用宽（770 右栏）。

    单跑也应通过：busy 实测 614 ≤ 770 右栏（不再依赖跨用例样式表 polish）。
    """
    p, _fake = page
    p.resize(770, 680)
    p._skill_slugs = []
    p._refresh_skill_chips()
    _settle()
    p._busy_lbl.setVisible(True)
    p._stop_btn.setVisible(True)
    p._guide_btn.setVisible(True)
    _settle()
    w = _bar_width(p)
    avail = p._composer.width() - 16
    assert w <= avail, f"忙碌态底栏 {w}px 超实际可用宽 {avail}px"
    assert w <= BAR_BUDGET_W, f"忙碌态底栏 {w}px 超门禁 {BAR_BUDGET_W}px"
    p._busy_lbl.setVisible(False)
    p._stop_btn.setVisible(False)
    p._guide_btn.setVisible(False)
    _settle()


def test_bottom_bar_fits_with_mounted_skill_chips(page):
    """挂载 1~5 个技能时底栏不得超实际可用宽（规则④：≥3 个折到输入框上方）。"""
    p, _fake = page
    p.resize(770, 680)
    for n, slugs in ((1, ["repo-digest"]),
                     (2, ["repo-digest", "code-review"]),
                     (3, ["a", "b", "c"]),
                     (5, ["a", "b", "c", "d", "e"])):
        p._skill_slugs = list(slugs)
        p._refresh_skill_chips()
        _settle()
        w = _bar_width(p)
        avail = p._composer.width() - 16
        assert w <= avail, f"{n} 个技能时底栏 {w}px 超实际可用宽 {avail}px"
        assert w <= BAR_BUDGET_W, f"{n} 个技能时底栏 {w}px 超门禁"
        # ≥3 个（或超预算）折行到输入框上方独立一行，而不是留在底栏
        if n >= 3:
            assert p._chips_row == "col", f"{n} 个技能未折行"


# --------------------------------------------------------------------------- #
# T04 ②：三个入口按钮恒可见、文案固定、BarChip 样式
# --------------------------------------------------------------------------- #
def test_three_entry_buttons_always_visible(page):
    """技能 / 工具 / MCP 三个按钮：文案固定中文、BarChip、恒可见（不按宽度隐藏）。"""
    p, _fake = page
    for btn, text in ((p._skill_btn, "技能"), (p._tool_btn, "工具"),
                      (p._mcp_btn, "MCP")):
        assert btn.text() == text
        assert btn.objectName() == "BarChip"
        assert btn.isVisibleTo(p), f"{text} 按钮被隐藏了"
        assert not btn.isHidden()
    # 顺序：技能 → 工具 → MCP
    for i in range(p._bar_lay.count()):
        w = p._bar_lay.itemAt(i).widget()
        if w is p._skill_btn:
            i_skill = i
        elif w is p._tool_btn:
            i_tool = i
        elif w is p._mcp_btn:
            i_mcp = i
    assert i_skill < i_tool < i_mcp


def test_three_buttons_text_never_changes_with_state(page):
    """无论是否挂载技能，按钮文案都保持「技能」（不换成数量）。"""
    p, _fake = page
    p._skill_slugs = ["repo-digest", "workspace-organizer"]
    p._refresh_skill_chips()
    _settle()
    assert p._skill_btn.text() == "技能"
    assert "已挂载 2 个技能" in p._skill_btn.toolTip()
    p._skill_slugs = []
    p._refresh_skill_chips()
    _settle()
    assert p._skill_btn.text() == "技能"


# --------------------------------------------------------------------------- #
# T04 ③：[技能] 菜单 —— 空态必须是三个可点的添加入口
# --------------------------------------------------------------------------- #
def test_skill_menu_empty_state_has_three_add_entries(page):
    """空态不再是死文案：必须给出三条能点的添加路径（PRD §5.1.1）。"""
    p, _fake = page
    p._skill_slugs = []
    p._refresh_skill_chips()
    menu = p._build_skill_menu()
    texts = _enabled_menu_texts(menu)
    joined = " | ".join(texts)
    assert "从在线清单安装" in joined, joined
    assert "从本地文件夹导入" in joined, joined
    assert "粘贴 URL" in joined, joined


def test_skill_menu_has_auto_load_toggle_and_mount_head(page):
    """顶部要有「自动加载：开/关」与「挂载技能 N/5」。"""
    p, fake = page
    p._profiles = dict(fake.profiles_data)
    menu = p._build_skill_menu()
    texts = _menu_texts(menu)
    assert any("自动加载" in t for t in texts), texts
    assert any(t.startswith("挂载技能") and "/5" in t for t in texts), texts


def test_skill_menu_lists_installed_skills_as_checkable(page):
    """已装技能是可勾选项，hover 带描述与工具数。"""
    p, _fake = page
    p._skill_slugs = ["repo-digest"]
    p._refresh_skill_chips()
    menu = p._build_skill_menu()
    checks = [a for a in menu.actions() if a.isCheckable() and a.text()]
    assert checks, "技能菜单里没有可勾选的技能项"
    hit = [a for a in checks if "repo-digest" in a.text()
           or "仓库摘要" in a.text()]
    assert hit, [a.text() for a in checks]
    assert hit[0].isChecked()
    assert hit[0].toolTip(), "技能项缺少描述 tooltip"


def test_skill_menu_toggle_auto_load_calls_save_profiles(page):
    """点「自动加载：开」→ PUT /profiles(auto_load=False)。"""
    p, fake = page
    p._profiles = dict(fake.profiles_data)
    menu = p._build_skill_menu()
    act = next(a for a in menu.actions() if "自动加载" in a.text())
    act.trigger()
    assert _pump(lambda: bool(fake.saved_profiles), 2.0)
    assert fake.saved_profiles[0][0] is False


# --------------------------------------------------------------------------- #
# T04 ④：[工具] 菜单 —— 下发表头 + 高危二次确认
# --------------------------------------------------------------------------- #
def test_tool_menu_head_shows_dispatched_and_limit(page):
    """顶部「当前下发 N / 上限 M」。"""
    p, fake = page
    p._tool_prefs = dict(fake.tool_prefs_data)
    menu = p._build_tool_menu()
    head = _menu_texts(menu)[0]
    assert "当前下发" in head and "上限" in head, head
    assert "5" in head and "24" in head, head


def test_tool_menu_toggles_builtin_tools(page):
    """内置工具是勾选项，状态来自 /tools/prefs 的 disabled。"""
    p, fake = page
    p._tool_prefs = dict(fake.tool_prefs_data)
    menu = p._build_tool_menu()
    checks = {a.text(): a for a in menu.actions() if a.isCheckable()}
    assert any("fs_list" in t for t in checks), list(checks)
    assert any("shell_run" in t for t in checks), list(checks)
    fs = next(a for t, a in checks.items() if "fs_list" in t)
    sh = next(a for t, a in checks.items() if "shell_run" in t)
    assert fs.isChecked() is True     # 未禁用
    assert sh.isChecked() is False    # disabled 里


def test_tool_menu_groups_mcp_servers(page):
    """MCP 工具按服务器归组，有「整组开启/整组关闭」。"""
    p, fake = page
    p._tool_prefs = dict(fake.tool_prefs_data)
    menu = p._build_tool_menu()
    subs = [a for a in menu.actions() if a.menu() is not None]
    assert subs, "工具菜单里没有 MCP 分组"
    names = " ".join(a.text() for a in subs)
    assert "filesystem" in names.lower(), names
    sub_texts = []
    for a in subs:
        sub_texts += _menu_texts(a.menu())
    joined = " | ".join(sub_texts)
    # 分组开关按当前状态二选一：全关 → 「整组开启」，有开着的 → 「整组关闭」
    assert ("整组开启" in joined) or ("整组关闭" in joined), joined


def test_tool_menu_has_center_and_reconnect_entries(page):
    """底部要有「工具清单与开关…」与「⟳ 重连已启用 MCP」。"""
    p, _fake = page
    menu = p._build_tool_menu()
    joined = " | ".join(_enabled_menu_texts(menu))
    assert "工具清单与开关" in joined, joined
    assert "重连" in joined, joined


def test_closing_high_risk_tool_needs_confirmation(page, monkeypatch):
    """关掉 shell_run 这类高危工具要二次确认（PRD §5.1.3）。"""
    p, fake = page
    p._tool_prefs = dict(fake.tool_prefs_data)
    p._tool_prefs["disabled"] = []      # 先让 shell_run 是开着的
    asked = {"n": 0}

    class _MB:
        class StandardButton:
            Yes = 1
            No = 2

        @staticmethod
        def question(*a, **kw):
            asked["n"] += 1
            return _MB.StandardButton.Yes

    monkeypatch.setattr(mod, "QMessageBox", _MB)
    assert p._tool_needs_confirm("shell_run") is True
    p._toggle_tool("shell_run", False)
    assert asked["n"] == 1, "关闭高危工具没有二次确认"
    assert _pump(lambda: bool(fake.saved_prefs), 2.0)


def test_low_risk_tool_toggles_without_confirmation(page, monkeypatch):
    """低危工具直接切，不弹确认。"""
    p, fake = page
    p._tool_prefs = dict(fake.tool_prefs_data)
    p._tool_prefs["disabled"] = ["fs_list"]
    asked = {"n": 0}

    class _MB:
        class StandardButton:
            Yes = 1
            No = 2

        @staticmethod
        def question(*a, **kw):
            asked["n"] += 1
            return _MB.StandardButton.Yes

    monkeypatch.setattr(mod, "QMessageBox", _MB)
    assert p._tool_needs_confirm("fs_list") is False
    p._toggle_tool("fs_list", True)
    assert asked["n"] == 0
    assert _pump(lambda: bool(fake.saved_prefs), 2.0)
    disabled, _mt = fake.saved_prefs[-1]
    assert "fs_list" not in disabled


# --------------------------------------------------------------------------- #
# T04 ⑤：[MCP] 菜单 —— 连接态表头 + 四条入口
# --------------------------------------------------------------------------- #
def test_mcp_menu_head_shows_configured_and_connected(page):
    """顶部「已配置 X 台 · 已连接 Y 台」。"""
    p, _fake = page
    menu = p._build_mcp_menu()
    head = _menu_texts(menu)[0]
    assert "已配置" in head and "已连接" in head, head
    assert "2" in head, head


def test_mcp_menu_lists_servers_with_connect_state(page):
    """每台服务器一行，子菜单里能连/断/删。"""
    p, _fake = page
    menu = p._build_mcp_menu()
    subs = [a for a in menu.actions() if a.menu() is not None]
    assert subs, "MCP 菜单里没有服务器行"
    names = " ".join(a.text() for a in subs).lower()
    assert "filesystem" in names and "github" in names, names
    sub_texts = []
    for a in subs:
        sub_texts += _menu_texts(a.menu())
    joined = " | ".join(sub_texts)
    # 已连接的那台给「断开」，未连接的给「连接」——按状态二选一
    assert "连接" in joined, joined
    assert "删除" in joined, joined


def test_mcp_menu_has_four_add_entries(page):
    """四条入口：在线清单 / 手动添加 / 粘贴 JSON / 管理服务器。"""
    p, _fake = page
    menu = p._build_mcp_menu()
    joined = " | ".join(_enabled_menu_texts(menu))
    assert "从在线清单安装 MCP" in joined, joined
    assert "手动添加" in joined, joined
    assert "粘贴 JSON" in joined, joined
    assert "管理 MCP 服务器" in joined, joined


# --------------------------------------------------------------------------- #
# T04 ⑥：[+] 附件菜单扩展
# --------------------------------------------------------------------------- #
def test_attach_menu_keeps_old_entries_and_adds_new(page):
    """[+] 菜单保留「添加附件…」与末尾四个跳转，并新增能力清单相关入口。"""
    p, _fake = page
    menu = p._build_attach_menu()
    texts = _menu_texts(menu)
    assert texts[0] == "添加附件…"
    assert texts[-4:] == ["技能", "工具", "MCP", "审计"]
    joined = " | ".join(texts)
    for want in ("能力清单", "从清单添加技能", "从清单添加 MCP",
                 "从本地文件夹导入技能", "导入技能 zip",
                 "粘贴 URL 安装技能", "粘贴 JSON 添加 MCP", "刷新在线目录"):
        assert want in joined, f"缺少入口：{want}\n{joined}"


def test_attach_menu_directory_import_uses_non_native_dialog(page, monkeypatch):
    """「从本地文件夹导入技能」必须走统一入口（DontUseNativeDialog）。"""
    calls = {}

    class _FD:
        class Option:
            DontUseNativeDialog = 1
            ShowDirsOnly = 2

        class FileMode:
            Directory = 3
            ExistingFile = 4
            AnyFile = 6

        class AcceptMode:
            AcceptOpen = 5
            AcceptSave = 7

        class DialogLabel:
            LookIn = "look"
            FileName = "name"
            FileType = "type"
            Accept = "accept"
            Reject = "reject"

        class DialogCode:
            Accepted = 1
            Rejected = 0

        def __init__(self, *a, **kw):
            calls["args"] = a
            calls["kwargs"] = kw

        def setOption(self, opt, on=True):
            calls.setdefault("opts", {})[opt] = on

        def setFileMode(self, mode):
            calls["file_mode"] = mode

        def setAcceptMode(self, mode):
            calls["accept_mode"] = mode

        def setLabelText(self, which, text):
            calls.setdefault("labels", {})[which] = text

        def setNameFilter(self, f):
            calls["filter"] = f

        def selectFile(self, p):
            calls["select"] = p

        def setDirectory(self, p):
            calls["dir"] = p

        def exec(self):
            return self.DialogCode.Rejected

        def selectedFiles(self):
            return []

    monkeypatch.setattr(mod, "QFileDialog", _FD)
    p, _fake = page
    p._import_skill_from_dir()
    assert calls["opts"][_FD.Option.DontUseNativeDialog] is True
    assert calls["file_mode"] == _FD.FileMode.Directory
    assert "accept_mode" not in calls, "目录模式不应设置 AcceptMode"
    labels = calls.get("labels", {})
    assert any("选择此文件夹" in v for v in labels.values()), labels


# --------------------------------------------------------------------------- #
# T03：ManagerDialog 尺寸（必须 ≤ 700×520 才装得进 720 逻辑宽）
# --------------------------------------------------------------------------- #
def test_manager_dialog_min_size_fits_720(page):
    """清单中心最小尺寸 700×520（原 760×560 在 720 逻辑宽下装不下）。"""
    p, _fake = page
    assert ManagerDialog.MIN_W == 700
    assert ManagerDialog.MIN_H == 520
    dlg = ManagerDialog(p)
    try:
        assert dlg.minimumWidth() == 700
        assert dlg.minimumHeight() == 520
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_manager_dialog_fit_to_parent_uses_86x84(page):
    """打开时按主窗口 86% × 84% 自适应，并夹到屏幕可用区。"""
    p, _fake = page
    p.resize(720, 680)
    dlg = ManagerDialog(p)
    try:
        dlg.fit_to_parent()
        # 小窗口：720×86% = 619 < MIN_W(700) → 取 700（下限优先）
        assert dlg.width() >= ManagerDialog.MIN_W
        assert dlg.height() >= ManagerDialog.MIN_H
        # 比例逻辑本身：不越出屏幕可用区，且不超父窗口 86%（取两者较小）
        avail = dlg.screen().availableGeometry() if dlg.screen() else None
        cap = int(p.width() * 0.86)
        if avail is not None:
            cap = min(cap, avail.width() - 24)
        assert dlg.width() <= max(ManagerDialog.MIN_W, cap) + 4
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_manager_dialog_has_four_tabs(page):
    """清单中心保留会话 + 技能 / 工具 / MCP 四页（会话页不删）。"""
    p, _fake = page
    dlg = ManagerDialog(p)
    try:
        names = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        joined = " | ".join(names)
        for want in ("技能", "工具", "MCP"):
            assert want in joined, joined
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_manager_dialog_folds_detail_below_720(page):
    """宽度 < 720 时详情列自动折起（右侧 38% 拿不到就还给列表）。"""
    p, _fake = page
    dlg = ManagerDialog(p)
    try:
        assert hasattr(p, "_skill_split")
        dlg.resize(700, 520)
        dlg._auto_fold_detail()
        sizes = p._skill_split.sizes()
        assert sizes[1] == 0, f"窄窗口下详情列未折起：{sizes}"
        dlg.resize(900, 600)
        dlg._auto_fold_detail()
        sizes2 = p._skill_split.sizes()
        assert sizes2[1] > 0, f"宽窗口下详情列未展开：{sizes2}"
        assert sizes2[0] / max(1, sum(sizes2)) > 0.5, "列表应占 62%"
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


# --------------------------------------------------------------------------- #
# T04 ⑦（1.0.9 修订）：自动加载细节不再在对话流里提示（参照 WorkBuddy）
# --------------------------------------------------------------------------- #
def test_step_line_signature_unchanged():
    """架构 §6 裁定：`StepLine.__init__` 签名不动，新组件用组合实现。"""
    sig = inspect.signature(StepLine.__init__)
    assert list(sig.parameters) == ["self", "text", "parent", "error"], sig


def test_auto_load_prompt_composes_step_line():
    """`AutoLoadPrompt` = StepLine + MiniBtn 并排（不改 StepLine）。"""
    prompt = AutoLoadPrompt("已自动加载：文件整理", "关闭自动加载")
    try:
        assert isinstance(prompt.step, StepLine)
        assert prompt.btn.text() == "关闭自动加载"
        assert prompt.btn.objectName() == "MiniBtn"
    finally:
        prompt.deleteLater()


def _auto_prompts(p) -> list:
    """当前对话流里的自动加载提示行。"""
    out = []
    for i in range(p._msg_layout.count()):
        w = p._msg_layout.itemAt(i).widget()
        if isinstance(w, AutoLoadPrompt):
            out.append(w)
    return out


def test_auto_loaded_event_renders_nothing(page):
    """1.0.9：start.auto_loaded 不再渲染任何提示行（加载细节不暴露）。"""
    p, _fake = page
    p._auto_tips.clear()
    before = p._msg_layout.count()
    p._on_event({"type": "start", "session_id": "s1",
                 "auto_loaded": {"profile": "file-organize", "name": "文件整理",
                                 "skills": ["workspace-organizer"], "tools": 7}})
    _settle()
    assert _auto_prompts(p) == []
    assert p._msg_layout.count() == before


def test_auto_loaded_repeated_events_still_silent(page):
    """重复收到 auto_loaded 同样不渲染（不刷屏、不累计）。"""
    p, _fake = page
    p._auto_tips.clear()
    ev = {"type": "start", "session_id": "s1",
          "auto_loaded": {"profile": "file-organize", "name": "文件整理",
                          "skills": [], "tools": 3}}
    p._on_event(dict(ev))
    p._on_event(dict(ev))
    _settle()
    assert _auto_prompts(p) == []


def test_disable_auto_load_calls_backend_and_silences(page):
    """点「关闭自动加载」→ PUT /profiles(auto_load=False)，本次会话不再提示。"""
    p, fake = page
    p._profiles = dict(fake.profiles_data)
    p._disable_auto_load()
    assert _pump(lambda: bool(fake.saved_profiles), 2.0)
    assert fake.saved_profiles[0][0] is False
    assert p._auto_off is True
    before = len(_auto_prompts(p))
    p._on_event({"type": "start", "session_id": "s1",
                 "auto_loaded": {"profile": "code-read", "name": "读代码",
                                 "skills": [], "tools": 2}})
    _settle()
    after = len(_auto_prompts(p))
    assert after == before, "关闭自动加载后仍在提示"


def test_send_payload_carries_mounted_skills_not_prefixed_text(page):
    """挂载技能走 `mounted_skills` 字段，message 保持用户原文。"""
    import ui.pages.assistant_page as ap
    p, _fake = page
    captured = {}

    class _Sig:
        def connect(self, *a, **kw):
            pass

    class _Stream:
        """拦下 payload，不真的起线程（信号用壳对象顶住 .connect）。"""

        event = _Sig()
        failed = _Sig()
        closed = _Sig()

        def __init__(self, base, key, payload, *a, **kw):
            captured.update(payload)

        def start(self):
            pass

    p._skill_slugs = ["repo-digest", "code-review"]
    p._refresh_skill_chips()
    orig = ap.AssistantStream
    ap.AssistantStream = _Stream
    try:
        p._send_with_session({"id": "s1", "message_count": 0}, "看看这个项目")
    finally:
        ap.AssistantStream = orig
    assert captured.get("message") == "看看这个项目", captured
    assert captured.get("mounted_skills") == ["repo-digest", "code-review"], captured


def test_no_mounted_skills_key_when_nothing_mounted(page):
    """没挂技能时 payload 不带 `mounted_skills` 字段。"""
    import ui.pages.assistant_page as ap
    p, _fake = page
    captured = {}

    class _Sig:
        def connect(self, *a, **kw):
            pass

    class _Stream:
        """拦下 payload，不真的起线程（信号用壳对象顶住 .connect）。"""

        event = _Sig()
        failed = _Sig()
        closed = _Sig()

        def __init__(self, base, key, payload, *a, **kw):
            captured.update(payload)

        def start(self):
            pass

    p._skill_slugs = []
    p._refresh_skill_chips()
    orig = ap.AssistantStream
    ap.AssistantStream = _Stream
    try:
        p._send_with_session({"id": "s1", "message_count": 0}, "你好")
    finally:
        ap.AssistantStream = orig
    assert captured.get("message") == "你好", captured
    assert "mounted_skills" not in captured, captured


# --------------------------------------------------------------------------- #
# T03：「粘贴 JSON 添加 MCP」解析（Claude Desktop 片段）
# --------------------------------------------------------------------------- #
def test_parse_mcp_json_claude_desktop_fragment():
    """支持 `{"mcpServers": {...}}` 写法，产出 /mcp 可 POST 的配置。"""
    text = ('{"mcpServers": {"filesystem": {"command": "npx", '
            '"args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}')
    out = parse_mcp_json(text)
    assert len(out) == 1
    cfg = out[0]
    assert cfg["id"] == "filesystem"
    assert cfg["name"] == "filesystem"
    assert cfg["transport"] == "stdio"
    assert cfg["command"] == "npx"
    assert cfg["args"][0] == "-y"
    assert cfg["enabled"] is True


def test_parse_mcp_json_http_server():
    """支持 http 传输写法（url 字段）。"""
    out = parse_mcp_json('{"github": {"url": "https://example/mcp", '
                         '"env": {"TOKEN": "x"}}}')
    assert len(out) == 1
    cfg = out[0]
    assert cfg["transport"] == "http"
    assert cfg["url"] == "https://example/mcp"


def test_parse_mcp_json_rejects_garbage():
    """非 JSON / 没有服务器对象 → 抛 ValueError（中文提示）。"""
    with pytest.raises(ValueError):
        parse_mcp_json("这不是 JSON")
    with pytest.raises(ValueError):
        parse_mcp_json('{"hello": "world"}')


def test_parse_mcp_json_dedupes_ids():
    """同名服务器自动加后缀，避免 id 冲突。"""
    import json
    out = parse_mcp_json(json.dumps([{"id": "x", "command": "npx"},
                                     {"id": "x", "command": "node"}]))
    ids = [c["id"] for c in out]
    assert len(ids) == len(set(ids)), ids
    assert ids == ["x", "x-2"], ids


# --------------------------------------------------------------------------- #
# T03：在线清单分类（零配置绿色 / 需密钥黄色默认收起）
# --------------------------------------------------------------------------- #
def test_catalog_class_badges_and_hidden_classes():
    """`零配置`/`需密钥` 分类常量存在，需密钥默认收起（PRD §5.3）。"""
    assert "zero_config" in CATALOG_CLASS_BADGE
    assert "need_key" in CATALOG_CLASS_BADGE
    assert "需密钥" in CATALOG_CLASS_BADGE["need_key"]
    assert "zero_config" not in CATALOG_HIDDEN_CLASSES
    assert "need_key" in CATALOG_HIDDEN_CLASSES


def test_mcp_command_whitelist_present():
    """`command` 白名单（架构 §9 风险项）。"""
    for cmd in ("npx", "uvx", "python", "node", "docker"):
        assert cmd in MCP_COMMAND_WHITELIST


# --------------------------------------------------------------------------- #
# T03：在线目录离线降级（不能白屏）
# --------------------------------------------------------------------------- #
def test_catalog_failure_shows_banner_not_blank(page):
    """在线目录拉失败时给黄色提示条，列表保留本地内容（不白屏）。"""
    p, fake = page
    # 在线区控件在清单中心里构建（open_manager 才建），先开一次
    dlg = ManagerDialog(p)
    try:
        fake.catalog_fail = True
        p._load_online_catalog(force=True)
        assert _pump(lambda: bool(p._mcp_online_banner.text()), 3.0)
        text = p._mcp_online_banner.text()
        # 没缓存时给「还没缓存，请检查网络/代理」；有缓存时给「刷新失败…显示缓存」
        assert ("刷新失败" in text) or ("还没有在线目录缓存" in text), text
        assert "刷新" in text, text          # 提示条给出重试指引
        assert p._mcp_online_banner.objectName() == "CatalogWarn"
        # 不白屏：失败时提示条承担全部说明（列表可以空，但界面必须有话）
        assert text.strip()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_catalog_empty_state_is_not_blank(page):
    """在线目录为空时列表给一句可读提示（P0-8 不白屏）。

    1.0.9：内嵌列表（限高 180px）已移入独立窗口 `OnlineMcpDialog`，
    断言目标随之改为窗口内的列表。
    """
    p, fake = page
    dlg = OnlineMcpDialog(p)
    try:
        fake.catalog_mcp_data = {"items": [], "fetched_at": 0,
                                 "stale": False, "error": ""}
        dlg._apply(dict(fake.catalog_mcp_data))
        _settle()
        assert dlg._list.count() == 1
        assert "为空" in dlg._list.item(0).text()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_online_repo_badge_shows_free_class(page):
    """在线条目按 free_class 打「[零配置]」/「[需密钥]」标签（PRD §5.3）。"""
    p, fake = page
    dlg = OnlineMcpDialog(p)
    try:
        dlg._apply(dict(fake.catalog_mcp_data))
        _settle()
        text = dlg._list.item(0).text()
        assert CATALOG_CLASS_BADGE["zero_config"] in text, text
        assert "Filesystem" in text, text
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


# --------------------------------------------------------------------------- #
# 其它：工作区标签宽度规则①（4 字截断）
# --------------------------------------------------------------------------- #
def test_workspace_label_truncated_to_four_chars(page):
    """规则①：工作区标签最多 4 个字，完整名进 tooltip。"""
    p, _fake = page
    st = dict(p._state)
    st["workspace"] = r"E:\llm-service\very-long-workspace-name"
    p._apply_state(st)
    _settle()
    assert p._ws_btn.text() == _ellipsis("very-long-workspace-name", 4)
    assert len(p._ws_btn.text()) <= 4
    assert "very-long-workspace-name" in p._ws_btn.toolTip()


def test_model_label_truncated_to_eight_chars(page):
    """规则②：模型短名 ≤8 字。"""
    p, _fake = page
    assert hasattr(p, "MODEL_LABEL_MAX")
    assert p.MODEL_LABEL_MAX == 8


# --------------------------------------------------------------------------- #
# T05（1.0.9）：工具列表行高裁切 + 刷新反馈
# --------------------------------------------------------------------------- #
def test_tool_rows_tall_enough_for_text(page):
    """工具行高不能写死 24px —— 截图里「只读 / 内置」被裁掉下半截就是这个。

    断言用「字高 + 8」而非固定像素：200% 缩放下字高本身就会超过 24。
    """
    p, _fake = page
    dlg = ManagerDialog(p)
    try:
        dlg.show_tab("tools")
        p._fill_tools(p._state.get("tools") or [])
        _settle()
        lst = p._tool_list
        assert lst.count() >= 2, lst.count()
        need = lst.fontMetrics().height() + 8
        for i in range(lst.count()):
            h = lst.item(i).sizeHint().height()
            assert h >= need, f"第 {i} 行高 {h} < {need}，文字会被裁"
        # 行与行一致（不能第一行矮、后面高）
        heights = {lst.item(i).sizeHint().height() for i in range(lst.count())}
        assert len(heights) == 1, heights
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_refresh_tools_gives_visible_feedback(page):
    """「刷新没反应」：必须能看出真的刷了（按钮态 + 上次刷新时间 + 真重拉）。"""
    p, fake = page
    dlg = ManagerDialog(p)
    try:
        dlg.show_tab("tools")
        before = fake.tool_prefs_calls
        p._refresh_tools()
        # 发起瞬间按钮进「刷新中」且不可重复点
        assert p._tool_refresh_btn.text() == "⟳ 刷新中…"
        assert not p._tool_refresh_btn.isEnabled()
        assert _pump(lambda: bool(p._tool_last_lbl.text()), 3.0)
        assert "上次刷新" in p._tool_last_lbl.text()
        # 弹窗未 show 时 isVisible() 恒为 False，这里只验「没被隐藏」
        assert not p._tool_last_lbl.isHidden()
        assert p._tool_refresh_btn.isEnabled()
        assert p._tool_refresh_btn.text() == "⟳ 刷新"
        assert fake.tool_prefs_calls > before, "刷新必须真的重拉 prefs"
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_refresh_tools_failure_resets_button(page):
    """刷新失败：按钮恢复可点 + 状态行说明失败（不能卡在「刷新中」）。"""
    p, fake = page
    dlg = ManagerDialog(p)
    try:
        dlg.show_tab("tools")

        def boom():
            raise RuntimeError("后端未就绪")

        fake.tool_prefs = boom
        p._refresh_tools()
        assert _pump(lambda: p._tool_refresh_btn.isEnabled(), 3.0)
        assert p._tool_refresh_btn.text() == "⟳ 刷新"
        assert "刷新失败" in p._tool_last_lbl.text()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_bottom_bar_fits_with_context_meter(page):
    """上下文圆环显示 + 忙碌态全按钮：底栏仍不超可用宽度。

    1.0.11 把「上下文 6% · 453/8.2k」从输入框下方小字改成输入条右侧的图标
    （圆环，数字进 tooltip），会真的占底栏宽度，所以要在最挤的组合下验一遍。

    宽度基准取**真实最窄面板**：主窗口最小宽 1000 - 左栏 200 - 页面边距 ≈ 770，
    而不是老的 720px 门禁（那是按 720 逻辑高推的保守场景，实际窗口到不了）。
    另外钉一个「图标宽度 ≤ 22px」的上限：一旦有人往圆环里塞文字就会先在这里失败。
    """
    p, _fake = page
    p.resize(770, 680)
    p._skill_slugs = []
    p._refresh_skill_chips()
    p._apply_context({"used": 7000, "limit": 8000, "ratio": 0.875})
    p._busy_lbl.setVisible(True)
    p._stop_btn.setVisible(True)
    p._guide_btn.setVisible(True)
    _settle()
    assert p._ctx_meter.is_hot() is True
    assert not p._ctx_meter.isHidden()
    assert p._ctx_meter.width() <= 22, "上下文圆环只能是图标（文字请放 tooltip）"
    w = _bar_width(p)
    avail = p._composer.width() - 16      # 减去 composer 左右内边距
    assert w <= avail, f"带上下文圆环的忙碌态底栏 {w}px 超可用宽 {avail}px"
