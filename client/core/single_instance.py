# -*- coding: utf-8 -*-
"""
单实例锁（single_instance.py）。

需求（2026-10-20 用户红字）：如果已有 llmBuddy 在运行，再次打开程序时
不要开启新窗口/新进程，保证只有一个程序运行。

实现（Qt 原生，跨平台，Windows 上是命名管道）：
    1. 首个实例 try_lock() 用 QLocalServer 监听固定名字，成功 → 持锁（primary）；
    2. 后续实例 try_lock() 用 QLocalSocket 连接该名字，连得上说明已有实例
       在跑 → 向其发送激活请求并返回 False（调用方随即退出，不开新窗口）；
    3. 已有实例收到新连接后发出 activate_requested 信号，主窗口把自身
       置前/还原，用户看到的就是「又打开了一次，窗口被带到前台」。

异常安全：
    - 上一个实例崩溃残留的 server 名：connectToServer 失败即认为无活实例，
      先 QLocalServer.removeServer 清理再监听（Windows 命名管道随进程消亡，
      此清理主要面向 Unix 域套接字文件，双保险）；
    - listen 失败（极小概率，如权限异常）→ 保守返回 False 让新实例退出，
      避免出现两个窗口。
"""
from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

# 锁名带产品名即可；Windows 命名管道按用户会话隔离，不需要加全局前缀。
DEFAULT_KEY = "llmBuddy-single-instance"

_ACTIVATE_MSG = b"activate\n"


class SingleInstanceGuard(QObject):
    """单实例守卫：try_lock() 返回 True 的一方继续运行，False 的一方退出。"""

    #: 第二个实例接入时发出（无论其发送了什么内容，语义都是「用户又打开了一次」）
    activate_requested = Signal()

    def __init__(self, key: str = DEFAULT_KEY, parent=None) -> None:
        super().__init__(parent)
        self._key = key
        self._server: QLocalServer | None = None
        self._primary = False

    # ------------------------------------------------------------------
    @property
    def is_primary(self) -> bool:
        return self._primary

    @property
    def key(self) -> str:
        return self._key

    def try_lock(self) -> bool:
        """尝试成为唯一实例。返回 True=持锁继续运行；False=已有实例，应退出。"""
        # 1) 先探测是否已有活实例
        sock = QLocalSocket()
        sock.connectToServer(self._key)
        if sock.waitForConnected(300):
            # 已有实例在跑：通知它激活窗口，然后让本进程退出
            try:
                sock.write(_ACTIVATE_MSG)
                sock.flush()
                sock.waitForBytesWritten(300)
            except Exception:  # noqa: BLE001  通知失败不影响「退出」决策
                pass
            sock.abort()
            self._primary = False
            return False

        # 2) 没有活实例：清理可能的残留名后开始监听
        QLocalServer.removeServer(self._key)
        self._server = QLocalServer()
        if not self._server.listen(self._key):
            # 监听失败（异常环境）：保守判定为「抢不到锁」，退出更安全
            self._server = None
            self._primary = False
            return False
        self._server.newConnection.connect(self._on_new_connection)
        self._primary = True
        return True

    # ------------------------------------------------------------------
    def _on_new_connection(self) -> None:
        """第二个实例接入 → 通知主窗口激活。"""
        while self._server is not None and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if sock is None:
                continue
            # 读掉对方数据（不强依赖内容），随后立即发出激活信号
            if sock.bytesAvailable() > 0:
                sock.readAll()
            self.activate_requested.emit()
            sock.disconnectFromServer()

    def release(self) -> None:
        """释放锁（进程退出时 Qt 会自动清理，显式提供便于测试）。"""
        if self._server is not None:
            self._server.close()
            self._server = None
        self._primary = False
