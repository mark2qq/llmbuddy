# -*- coding: utf-8 -*-
"""
状态栏「运行中模型」显示回归测试（需求 C / 需求 E 修复锁定）。

核心 bug：状态变化（_on_state / _on_ready）调用 set_status(loaded=0) 会把
后台 set_models() 回填的模型名重新清成「0（无）」，导致状态栏永远看不到
正在运行的模型。此测试直接验证：
    1. set_models 写入模型名后，随后无模型信息的 set_status(running) 必须保留；
    2. set_status(stopped) 必须清空为「0（无）」；
    3. 切页/状态变化不触发 set_status 时，已显示模型名不丢失。
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


def _bar():
    from ui.widgets.status_bar import StatusBar
    return StatusBar()


def test_set_models_then_running_status_preserves():
    """set_models 回填后，running 态 set_status 必须保留模型名（修复点）。"""
    bar = _bar()
    bar.set_models(["qwen3-4b"])
    assert "qwen3-4b" in bar._models.text(), bar._models.text()

    # 模拟 _on_ready / _on_state(running) 不带模型信息的调用
    bar.set_status("running", url="http://127.0.0.1:8000")
    assert "qwen3-4b" in bar._models.text(), \
        f"running set_status 不应清空模型名，实际: {bar._models.text()}"


def test_stopped_status_clears_models():
    """stopped 态必须清空为「0（无）」。"""
    bar = _bar()
    bar.set_models(["qwen3-4b"])
    bar.set_status("stopped", url="http://127.0.0.1:8000")
    assert "0" in bar._models.text() and "无" in bar._models.text(), \
        f"stopped 应清空模型，实际: {bar._models.text()}"


def test_starting_status_preserves():
    """starting 过渡态（无模型信息）也应保留已显示模型名。"""
    bar = _bar()
    bar.set_models(["qwen3-4b"])
    bar.set_status("starting", url="")
    assert "qwen3-4b" in bar._models.text(), \
        f"starting 不应清空模型名，实际: {bar._models.text()}"


def test_no_overwrite_when_already_empty():
    """初始/空态 running 显示「0（无）」，不报错。"""
    bar = _bar()
    bar.set_status("running", url="http://127.0.0.1:8000")
    assert "无" in bar._models.text(), bar._models.text()


# ---------------------------------------------------------------------- #
# 主窗口级联：控制台页 pool 快照 -> 状态栏即时回填（需求 E 第二轮修复）
# ---------------------------------------------------------------------- #
from PySide6.QtCore import QObject, Signal  # noqa: E402


class _FakeApi:
    def pool(self):
        return {"loaded": [], "max_concurrent": 1}

    def list_models(self):
        return []

    def list_keys(self):
        return []

    def system_info(self):
        return {"free_ram_gb": 8.0, "total_ram_gb": 16.0}

    def concurrency(self):
        return {"max_concurrent": 1, "suggested": 1, "reason": ""}

    def catalog(self, force=False, keyword=""):
        return {"items": [], "cached": False}

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

    def __init__(self, running: bool = False):
        super().__init__()
        self._running = running

    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        self._running = True
        return True

    def stop(self) -> None:
        self._running = False

    def kill_hard(self) -> None:
        pass


def _make_window(running: bool = False):
    from ui.main_window import MainWindow
    launcher = _FakeLauncher(running=running)
    win = MainWindow(_FakeApi(), launcher, _FakeConfig())
    win._refresh_all = lambda: None  # 隔离各页刷新
    return win, launcher


def _apply(win):
    app.processEvents()


def test_dashboard_pool_signal_updates_status_bar():
    """控制台页拿到 pool 快照（ready+paused）→ 状态栏只显示 ready 模型名。"""
    win, _launcher = _make_window(running=True)
    win.dashboard._apply_pool({"loaded": [
        {"name": "qwen2.5-coder-7b", "status": "ready"},
        {"name": "llama-3.2-3b", "status": "paused"},
    ]})
    _apply(win)
    text = win._status_bar._models.text()
    assert "qwen2.5-coder-7b" in text, f"状态栏应显示运行中模型，实际: {text}"
    assert "llama-3.2-3b" not in text, f"paused 模型不应计入，实际: {text}"


def test_loading_model_is_shown_with_progress():
    """加载中的模型也要显示（带进度），避免加载期间状态栏空着「0（无）」。"""
    win, _launcher = _make_window(running=True)
    win.dashboard._apply_pool({"loaded": [
        {"name": "qwen3-4b", "status": "loading", "progress": 0.45},
    ]})
    _apply(win)
    text = win._status_bar._models.text()
    assert "qwen3-4b" in text, f"加载中的模型应显示，实际: {text}"
    assert "45%" in text, f"应显示加载进度，实际: {text}"
    assert "0（无）" not in text, f"加载中不应显示为无模型，实际: {text}"


def test_poll_always_refreshes_regardless_of_launcher_state():
    """5s 兜底轮询：无论后端是否由本进程拉起，都必须发请求。

    历史缺陷：曾以 launcher.is_running() 作门槛，而后端由看门狗重启 /
    手动启动时该值为 False → 轮询永不运行 → 状态栏永远「0（无）」。
    """
    win, _launcher = _make_window(running=False)
    calls = []
    win._refresh_status_models = lambda: calls.append(1)
    win._poll_status_models()
    assert calls, "后端非本进程拉起时轮询也必须发请求，否则状态栏不再更新"

    win2, _launcher2 = _make_window(running=True)
    calls2 = []
    win2._refresh_status_models = lambda: calls2.append(1)
    win2._poll_status_models()
    assert calls2, "运行态轮询应发请求"
