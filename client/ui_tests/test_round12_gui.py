# -*- coding: utf-8 -*-
"""
第十二轮 GUI 无头测试。

覆盖（红字）：托盘「启动服务 / 停止服务」互斥——
    - 服务运行中：启动置灰、停止可点
    - 服务已停止：启动可点、停止置灰
    - 状态随 launcher.state_changed 实时切换（含 starting 过渡态）
    - 两个动作点击后确实调用 launcher.start() / stop()
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

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import QObject, Signal  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    """覆盖各页 refresh 会用到的方法（返回良性空数据）。"""

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
    """带 Qt 信号的假 launcher：记录调用并按真实语义发状态信号。"""

    state_changed = Signal(str)
    ready = Signal(bool)
    log = Signal(str)

    def __init__(self, running: bool = False):
        super().__init__()
        self._running = running
        self.calls = []

    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        self.calls.append("start")
        self._running = True
        self.state_changed.emit("starting")
        return True

    def stop(self) -> None:
        self.calls.append("stop")
        self._running = False
        self.state_changed.emit("stopped")

    def kill_hard(self) -> None:
        self.calls.append("kill_hard")


def _make_window(running: bool = False):
    from ui.main_window import MainWindow
    launcher = _FakeLauncher(running=running)
    win = MainWindow(_FakeApi(), launcher, _FakeConfig())
    # 隔离：不触发各页 refresh 的后台任务（本测试只关心托盘状态）
    win._refresh_all = lambda: None
    return win, launcher


def _apply(win):
    app.processEvents()


def test_tray_initial_state_stopped():
    """未运行：启动可点、停止置灰。"""
    win, _launcher = _make_window(running=False)
    _apply(win)
    assert win._tray_start_action.isEnabled(), "已停止时应可点「启动服务」"
    assert not win._tray_stop_action.isEnabled(), "已停止时「停止服务」应置灰"


def test_tray_initial_state_running():
    """已运行（GUI 重启但后端仍在）：启动置灰、停止可点。"""
    win, _launcher = _make_window(running=True)
    _apply(win)
    assert not win._tray_start_action.isEnabled(), "运行中「启动服务」应置灰"
    assert win._tray_stop_action.isEnabled(), "运行中应可点「停止服务」"


def test_tray_actions_follow_state_changes():
    """状态变化实时切换：running -> 启动灰/停止亮；stopped -> 反之。"""
    win, launcher = _make_window(running=False)
    _apply(win)

    launcher.state_changed.emit("running")
    _apply(win)
    assert not win._tray_start_action.isEnabled()
    assert win._tray_stop_action.isEnabled()

    launcher.state_changed.emit("stopped")
    _apply(win)
    assert win._tray_start_action.isEnabled()
    assert not win._tray_stop_action.isEnabled()


def test_tray_actions_starting_and_error():
    """starting 视为运行侧（可点停止中止）；error 回到可启动。"""
    win, launcher = _make_window(running=False)
    _apply(win)

    launcher.state_changed.emit("starting")
    _apply(win)
    assert not win._tray_start_action.isEnabled(), "启动中「启动服务」应置灰"
    assert win._tray_stop_action.isEnabled(), "启动中应可点「停止服务」中止"

    launcher.state_changed.emit("error")
    _apply(win)
    assert win._tray_start_action.isEnabled(), "启动失败后应恢复可启动"
    assert not win._tray_stop_action.isEnabled()


def test_tray_actions_invoke_launcher():
    """排查功能是否实现：两个动作确实调用 launcher.start()/stop()。"""
    win, launcher = _make_window(running=False)
    _apply(win)

    win._tray_start_action.trigger()
    assert "start" in launcher.calls, "「启动服务」应调用 launcher.start()"
    assert launcher.is_running()
    _apply(win)
    # start() 内已发 starting -> 互斥已切换，此时可点停止
    assert win._tray_stop_action.isEnabled()

    launcher.state_changed.emit("running")
    _apply(win)
    win._tray_stop_action.trigger()
    assert "stop" in launcher.calls, "「停止服务」应调用 launcher.stop()"
    assert not launcher.is_running()
    _apply(win)
    assert win._tray_start_action.isEnabled()
