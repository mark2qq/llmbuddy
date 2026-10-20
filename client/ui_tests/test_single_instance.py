# -*- coding: utf-8 -*-
"""
单实例锁回归测试（2026-10-20 用户红字需求锁定）。

需求：如果已有 llmBuddy 在运行，再次打开程序时不要开启新窗口/新进程，
保证只有一个程序运行。验证点：
    1. 第一个实例 try_lock() 成功（primary）；
    2. 第二个实例 try_lock() 失败（False），且会让第一个实例发出
       activate_requested 信号（激活已有窗口）；
    3. 信号语义正确：第二个实例每次接入都触发一次激活请求；
    4. release() 之后新的实例可以拿到锁（模拟旧实例正常退出）。
"""
import os
import sys
import uuid

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core.single_instance import SingleInstanceGuard  # noqa: E402


def _unique_key() -> str:
    """每个用例用独立锁名，避免用例间相互污染。"""
    return "llmBuddy-test-" + uuid.uuid4().hex[:8]


def test_first_instance_is_primary():
    g = SingleInstanceGuard(_unique_key())
    try:
        assert g.try_lock() is True
        assert g.is_primary is True
    finally:
        g.release()


def test_second_instance_fails_and_activates_first():
    key = _unique_key()
    first = SingleInstanceGuard(key)
    try:
        assert first.try_lock() is True

        # 记录激活信号
        fired = []
        first.activate_requested.connect(lambda: fired.append(1))

        # 第二个实例抢锁必须失败
        second = SingleInstanceGuard(key)
        try:
            assert second.try_lock() is False, "第二个实例不应拿到单实例锁"
            assert second.is_primary is False
        finally:
            second.release()

        # 处理事件让服务端收到连接并发出信号
        for _ in range(50):
            app.processEvents()
            if fired:
                break
            import time
            time.sleep(0.01)

        assert fired, "第二个实例启动应触发第一个实例的 activate_requested 信号"
    finally:
        first.release()


def test_release_allows_new_primary():
    key = _unique_key()
    g1 = SingleInstanceGuard(key)
    assert g1.try_lock() is True
    g1.release()

    g2 = SingleInstanceGuard(key)
    try:
        assert g2.try_lock() is True, "旧实例 release 后，新实例应能拿到锁"
    finally:
        g2.release()


def test_activate_message_is_tolerated():
    """第二个实例不发内容/发任意内容，第一个实例都应发出激活信号。"""
    key = _unique_key()
    first = SingleInstanceGuard(key)
    try:
        assert first.try_lock() is True
        fired = []
        first.activate_requested.connect(lambda: fired.append(1))

        from PySide6.QtNetwork import QLocalSocket
        sock = QLocalSocket()
        sock.connectToServer(key)
        assert sock.waitForConnected(300)
        sock.write(b"whatever")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.abort()

        for _ in range(50):
            app.processEvents()
            if fired:
                break
            import time
            time.sleep(0.01)
        assert fired, "任意内容的接入都应触发激活信号"
    finally:
        first.release()
