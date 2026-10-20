# -*- coding: utf-8 -*-
"""
状态栏「服务状态」回归测试（2026-10-20 红字：服务已启动却显示「服务未启动」）。

缺陷：MainWindow._apply_config（设置页保存时调用）硬编码
set_status("stopped")，之后只有 5s 轮询回填「已加载模型」、不恢复服务状态，
于是出现「服务未启动 + 已加载模型 1 (xxx)」的自相矛盾显示。

修复：状态以 health() 是否可达为唯一事实来源——保存设置后立即重新探测，
5s 兜底轮询同样探测；「启动中」不会被探测失败误判为未启动。
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

from app_config import Config  # noqa: E402
from core.backend_launcher import BackendLauncher  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


class _FakeApi:
    """health 可达（返回含 pool 的响应）。"""
    host = "127.0.0.1"
    port = 8000
    api_key = ""

    def health(self):
        return {"status": "ok", "pool": {"loaded": [
            {"name": "qwen3-4b", "status": "ready"}]}}

    def pool(self):
        return {"loaded": [{"name": "qwen3-4b", "status": "ready"}]}


def _make_window():
    cfg = Config()
    launcher = BackendLauncher(cfg)
    win = MainWindow(_FakeApi(), launcher, cfg)
    # 关掉测试期间的后台定时器与托盘，避免干扰其它用例
    win._status_poll.stop()
    win.dashboard._mon_timer.stop()
    win.tray.hide()
    return win


def test_health_marks_running_and_models():
    win = _make_window()
    win._apply_status_health({"pool": {"loaded": [
        {"name": "qwen3-4b", "status": "ready"}]}})
    assert win._status_state == "running"
    assert win._status_bar._status.text() == "服务运行中"
    assert "qwen3-4b" in win._status_bar._models.text()


def test_probe_error_marks_stopped():
    win = _make_window()
    win._apply_status_health({"pool": {"loaded": []}})
    assert win._status_state == "running"
    win._on_status_probe_error("connection refused")
    assert win._status_state == "stopped"
    assert win._status_bar._status.text() == "服务未启动"


def test_starting_is_not_downgraded_by_probe_error():
    """启动中 health 必然失败，不应被误判成「服务未启动」。"""
    win = _make_window()
    win._on_state("starting")
    win._on_status_probe_error("connection refused")
    assert win._status_state == "starting"
    assert win._status_bar._status.text() == "启动中…"


def test_apply_config_does_not_force_stopped():
    """保存设置不应把运行中的服务显示成未启动（红字缺陷本体）。"""
    win = _make_window()
    win._apply_status_health({"pool": {"loaded": []}})
    assert win._status_state == "running"
    win.config.background_color = ""
    win._apply_config()
    # 同步阶段不能立刻改成 stopped；随后后台探测也会确认 running
    assert win._status_state == "running"
    assert win._status_bar._status.text() == "服务运行中"


def test_apply_status_health_updates_model_names_only():
    win = _make_window()
    win._apply_status_health({"pool": {"loaded": [
        {"name": "a", "status": "ready"},
        {"name": "b", "status": "loading", "progress": 0.4},
        {"name": "c", "status": "paused"}]}})
    text = win._status_bar._models.text()
    assert "a" in text and "b(加载中 40%)" in text
    assert "c" not in text  # paused 不计入运行中
