# -*- coding: utf-8 -*-
"""
自定义信号（PySide6 pyqtSignal 载体）。

所有信号载体均为 QObject 子类，便于跨线程（Worker 线程 -> 主线程）
安全发射，由主线程的槽函数更新 UI。
"""
# 注：PySide6 的信号名为 Signal（PyQt 兼容名为 pyqtSignal）。
# 这里用别名保留需求文档中的 pyqtSignal 命名，同时兼容 PySide6。
from PySide6.QtCore import QObject, Signal as pyqtSignal


class BackendSignals(QObject):
    """后端进程看护相关信号。"""

    state_changed = pyqtSignal(str)        # 状态文本: starting|running|stopped|error
    log = pyqtSignal(str)                   # 一行日志文本
    ready = pyqtSignal(bool)                # 后端是否就绪（可服务）
    error = pyqtSignal(str)                 # 错误信息


class TaskSignals(QObject):
    """通用异步任务（拉取 / 加载等）进度信号。"""

    progress = pyqtSignal(str, float, str)  # 任务名, 进度(0~1), 文本
    finished = pyqtSignal(str, bool, str)   # 任务名, 是否成功, 信息
    log = pyqtSignal(str)                   # 任务日志
