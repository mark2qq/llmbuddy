# -*- coding: utf-8 -*-
"""
连接被拒的友好自愈处理（auto_service.py）。

后端网关未运行时，客户端请求会抛 `ConnectError: [WinError 10061]（目标计算机积极拒绝）`。
此前各页直接把原始错误显示给用户，生硬且无从下手。这里统一：
    1) 识别连接被拒类错误（10061 / ConnectError / Connection refused）
    2) 自动调用 launcher.start() 拉起后端（防重复触发）
    3) 延时后回调重试（后端就绪通常需数秒）
"""
from typing import Callable, Optional

from PySide6.QtCore import QTimer


def is_conn_refused(err: object) -> bool:
    """判断错误是否为『连不上本地后端』类。"""
    s = str(err)
    return ("10061" in s or "ConnectError" in s
            or "Connection refused" in s or "WinError 10061" in s)


class AutoService:
    """每页持有一个实例：连接被拒时自动启动服务并延时重试。"""

    def __init__(self, launcher, retry_cb: Callable[[], None], delay_ms: int = 8000):
        self._launcher = launcher
        self._retry_cb = retry_cb
        self._delay_ms = delay_ms
        self._pending = False

    def handle(self, err: object, on_refused: Callable[[str], None]) -> bool:
        """连接被拒返回 True（已自动启动并安排重试）；其他错误返回 False 走原逻辑。"""
        if not is_conn_refused(err):
            return False
        on_refused("本地服务未运行，正在自动启动…（约需数秒，完成后自动刷新）")
        if not self._pending:
            self._pending = True
            try:
                self._launcher.start()
            except Exception:
                pass
            QTimer.singleShot(self._delay_ms, self._fire)
        return True

    def _fire(self) -> None:
        self._pending = False
        try:
            self._retry_cb()
        except Exception:
            pass
