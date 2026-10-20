# -*- coding: utf-8 -*-
"""
外部后端停止回归测试（2026-10-20 红字：服务停止了仍显示「运行中」）。

缺陷链条：
    1. 后端由上一个 GUI 实例/手动启动时，新 GUI 的 launcher.proc 为 None；
    2. 托盘「停止服务」-> BackendLauncher.stop() 命中
       `if not self._running and self.proc is None: return` 分支，只发了个
       stopped 信号，**后端进程继续运行**；
    3. health 依然可达 -> 状态栏显示「服务运行中」（此时其实没停）。

修复：无 proc 时按网关端口定位 llmBuddy 后端 PID 并清理（先 M15 优雅停机，
再杀进程树）；同时校验进程身份，避免误杀恰好占用同一端口的其它程序。
"""
import os
import socket
import subprocess
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

from app_config import Config  # noqa: E402
from core.backend_launcher import BackendLauncher  # noqa: E402


def test_find_backend_pids_ignores_non_backend_process():
    """占用端口的非 llmBuddy 进程不会被识别（防误杀）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    try:
        port = s.getsockname()[1]
        assert BackendLauncher._find_backend_pids(port) == []
    finally:
        s.close()


def test_stop_without_proc_when_unreachable():
    """未持有句柄且服务不可达：stop() 安全返回并发出 stopped。"""
    cfg = Config()
    cfg.gateway_port = 8099       # 无服务监听
    launcher = BackendLauncher(cfg)
    assert launcher.proc is None
    states = []
    launcher.state_changed.connect(lambda s: states.append(s))
    launcher.stop()
    assert "stopped" in states
    assert launcher.proc is None


def test_kill_process_tree_accepts_pid_int():
    """_kill_process_tree 支持直接传 PID（外部后端只能按 PID 定位）。"""
    p = subprocess.Popen([sys.executable, "-c",
                          "import time; time.sleep(30)"])
    try:
        assert p.poll() is None
        BackendLauncher._kill_process_tree(p.pid)
        p.wait(timeout=10)
        assert p.returncode is not None
    finally:
        if p.poll() is None:
            p.kill()
