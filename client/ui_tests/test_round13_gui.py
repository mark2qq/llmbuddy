# -*- coding: utf-8 -*-
"""
第十三轮 GUI 无头测试。

覆盖：
    - 托盘「启动/停止服务」互斥的**视觉**保证（QSS 禁用项必须变灰）
    - API 页只显示本程序下载的模型（内置目录模板不显示）
    - 控制台模型测试结果可滚动查看完整内容（不再被裁切）
    - 标题改为 llmBuddy + 版本号；窗口/托盘图标为自绘「模型伙伴」图标
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

from PySide6.QtWidgets import QApplication, QTextEdit, QListWidget  # noqa: E402
from PySide6.QtCore import QObject, Signal, Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    def pool(self):
        return {"loaded": [{"name": "m-loaded", "status": "ready"}],
                "max_concurrent": 1}

    def list_models(self):
        # m-builtin 未下载（不在 catalog local 中）-> 应被过滤掉
        return [{"id": "m-loaded", "loaded": True},
                {"id": "m-local2", "loaded": False},
                {"id": "m-builtin", "loaded": False}]

    def catalog(self, force=False, keyword=""):
        return {"items": [
            {"name": "m-loaded", "local": True},
            {"name": "m-local2", "local": True},
            {"name": "m-builtin", "local": False},
        ], "cached": True}

    def list_keys(self):
        return []

    def system_info(self):
        return {"free_ram_gb": 8.0}

    def concurrency(self):
        return {"max_concurrent": 1, "suggested": 1, "reason": ""}

    def test_model(self, name, prompt="", **kw):
        # 长样例：用于验证测试结果可滚动、不截断
        sample = "我是 llmBuddy 的伙伴模型，" + "很长的示例输出内容。" * 40
        return {"ok": True, "latency_ms": 100, "sample": sample}

    def health(self):
        return {"status": "ok"}


class _FakeConfig:
    models_dir = ""
    n_ctx = 8192
    n_threads = 4
    n_gpu_layers = 0
    api_key = ""
    auto_restart = False
    gateway_host = "127.0.0.1"
    gateway_port = 8000
    engine_base_port = 7999
    install_path = ""
    log_level = "info"
    max_concurrent_models = 1

    def openai_base_url(self):
        return "http://127.0.0.1:8000/v1"

    def gateway_url(self):
        return "http://127.0.0.1:8000"


class _FakeLauncher(QObject):
    state_changed = Signal(str)
    ready = Signal(bool)
    log = Signal(str)

    def __init__(self, running: bool = True):
        super().__init__()
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass

    def kill_hard(self) -> None:
        pass


def _spin(until=None, timeout=5.0):
    import time
    deadline = time.time() + timeout
    while True:
        app.processEvents()
        if until is not None and until():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.005)


def _wait_models_applied(p, timeout=5.0):
    """等待 ApiPage.refresh() 的后台模型聚合完成（刷新已异步化：16 轮）。"""
    ok = _spin(until=lambda: p._models.count() >= 2, timeout=timeout)
    assert ok, "refresh() 后台模型聚合未在限时内完成"


# ---------------------------------------------------------------------------
# 图1/2：托盘启停互斥的视觉保证
# ---------------------------------------------------------------------------
def test_tray_menu_has_disabled_style():
    """禁用项必须有明显的灰色样式，否则用户看到「两项都点亮」。"""
    qss_path = os.path.join(_CLIENT_DIR, "resources", "styles.qss")
    with open(qss_path, "r", encoding="utf-8") as f:
        qss = f.read()
    assert "QMenu#TrayMenu" in qss, "托盘菜单应有专项样式"
    assert "QMenu#TrayMenu::item:disabled" in qss, "禁用项必须有置灰样式"


def test_tray_menu_object_name_and_states():
    """托盘菜单对象名就位（样式才生效），且启停状态互斥。"""
    from ui.main_window import MainWindow

    launcher = _FakeLauncher(running=True)      # 服务运行中
    win = MainWindow(_FakeApi(), launcher, _FakeConfig())
    win._refresh_all = lambda: None
    menu = win.tray.contextMenu()
    assert menu is not None and menu.objectName() == "TrayMenu"
    assert not win._tray_start_action.isEnabled(), "运行中「启动服务」应置灰"
    assert win._tray_stop_action.isEnabled()

    launcher.state_changed.emit("stopped")
    app.processEvents()
    assert win._tray_start_action.isEnabled()
    assert not win._tray_stop_action.isEnabled(), "已停止「停止服务」应置灰"


# ---------------------------------------------------------------------------
# 图3：API 页只显示本程序下载的模型
# ---------------------------------------------------------------------------
def test_api_page_filters_non_local_models():
    from ui.pages.api_page import ApiPage

    p = ApiPage(_FakeApi(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    # 第 0 项为「全局 Key」入口；模型项从第 1 项开始
    ids = [p._models.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(1, p._models.count())]
    assert "m-builtin" not in ids, "不是本程序下载的模型不应显示"
    assert set(ids) == {"m-loaded", "m-local2"}
    # 已加载模型仍排在最前（全局项之后的第一项）
    assert ids[0] == "m-loaded"
    # 默认选中已加载模型（而非全局项）
    assert p._selected_model == "m-loaded"


def test_api_page_keeps_list_when_catalog_fails():
    """目录查询异常时不做过滤，避免列表被误清空。"""
    from ui.pages.api_page import ApiPage

    class _NoCatalog(_FakeApi):
        def catalog(self, force=False, keyword=""):
            raise RuntimeError("backend down")

    p = ApiPage(_NoCatalog(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    ids = [p._models.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(1, p._models.count())]
    # 目录失败 -> 以 pool 已加载名兜底；列表不应被清空
    assert ids, "接口异常时不应把列表清空"


# ---------------------------------------------------------------------------
# 图4：测试结果可滚动、不截断
# ---------------------------------------------------------------------------
def test_dashboard_test_result_scrollable():
    from ui.pages.dashboard_page import DashboardPage

    p = DashboardPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    assert isinstance(p._test_label, QTextEdit), "测试结果应为可滚动文本框"
    assert p._test_label.isReadOnly()
    assert p._test_label.height() >= 80, "应有足够高度展示多行"
    assert p._test_label.verticalScrollBarPolicy() in (
        Qt.ScrollBarPolicy.ScrollBarAsNeeded,
        Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

    p._model_combo.set_models(["m-loaded"])
    p._on_test()
    assert _spin(until=lambda: "通过" in p._test_label.toPlainText())
    text = p._test_label.toPlainText()
    assert len(text) > 400, "长样例应完整保留（不截断，可滚动查看）"
    assert text.rstrip().endswith("。"), "内容末尾应完整保留"


# ---------------------------------------------------------------------------
# 图5/6：标题与图标
# ---------------------------------------------------------------------------
def test_title_and_icon():
    from app_config import APP_NAME, APP_VERSION
    from ui.main_window import MainWindow

    win = MainWindow(_FakeApi(), _FakeLauncher(), _FakeConfig())
    win._refresh_all = lambda: None
    assert APP_NAME == "llmBuddy"
    assert "llmBuddy" in win.windowTitle() and APP_VERSION in win.windowTitle()
    assert "llmBuddy" in win.tray.toolTip()
    from PySide6.QtWidgets import QWidget as _QWidget, QLabel as _QLabel
    nav = win.findChild(_QWidget, "NavTitle")
    assert nav is not None
    name_lbl = nav.findChild(_QLabel, "NavTitleLabel")
    ver_lbl = nav.findChild(_QLabel, "NavVersionLabel")
    assert name_lbl is not None and APP_NAME in name_lbl.text()
    assert ver_lbl is not None and APP_VERSION in ver_lbl.text()
    # 图标来自资源文件（非纯色方块回退）
    assert not win._app_icon.isNull()
    png = os.path.join(_CLIENT_DIR, "resources", "llmbuddy.png")
    ico = os.path.join(_CLIENT_DIR, "resources", "llmbuddy.ico")
    assert os.path.exists(png) and os.path.getsize(png) > 3000
    assert os.path.exists(ico) and os.path.getsize(ico) > 3000
    # ICO 至少包含多种尺寸（16/32/48/256）
    import struct
    with open(ico, "rb") as f:
        head = f.read(6)
    _, typ, count = struct.unpack("<HHH", head)
    assert typ == 1 and count >= 4, "ICO 应包含多尺寸条目"
