# -*- coding: utf-8 -*-
"""
后台任务基类（task_runner.py）。

基于 QThread，将耗时操作（网络请求、子进程等待等）放到子线程执行，
通过 result / error 信号把结果投递回主线程，避免阻塞 UI。
"""
from typing import Any, Callable, Optional
from PySide6.QtCore import QThread, Signal

# 运行中 Worker 的强引用持有池。
# PySide6 中 QThread 的 Python 包装对象若在 run() 期间被回收（调用方未持有引用），
# 会销毁仍在运行的底层 C++ QThread，导致原生崩溃（0xC0000409 STATUS_STACK_BUFFER_OVERRUN），
# 表现为 GUI 一刷新/一发起后台任务就闪退。故由本模块统一持有，finished 后再释放。
_ACTIVE: list = []


def _reap(worker: "Worker") -> None:
    """线程结束后释放强引用并安排删除（在主线程事件循环中安全销毁）。"""
    try:
        _ACTIVE.remove(worker)
    except ValueError:
        pass
    worker.deleteLater()


class Worker(QThread):
    """通用后台工作线程。"""

    result = Signal(object)                 # 任务返回结果
    error = Signal(str)                     # 异常信息（字符串）

    def __init__(self, fn: Callable[..., Any], *args, **kwargs):
        """
        参数：
            fn       要在子线程执行的可调用对象
            *args    位置参数
            **kwargs 关键字参数
        """
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        # 线程结束时释放 _ACTIVE 中的强引用（finished 在 QThread 对象所在线程/主线程触发，
        # 与 start() 中的 append 同在主线序，避免对 _ACTIVE 的并发读写）。
        self.finished.connect(self._on_finished)

    def start(self):  # noqa: N802 - 覆盖 QThread.start
        # 启动即登记强引用，防止运行期间被 Python GC 回收导致原生崩溃。
        _ACTIVE.append(self)
        super().start()

    def _on_finished(self, *args) -> None:
        _reap(self)

    def run(self) -> None:
        """执行任务并把结果/异常通过信号投递给主线程。"""
        try:
            out = self._fn(*self._args, **self._kwargs)
            self.result.emit(out)
        except Exception as exc:  # noqa: BLE001 - 统一以字符串回传
            self.error.emit(f"{type(exc).__name__}: {exc}")
