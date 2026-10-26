# -*- coding: utf-8 -*-
"""
Windows 异常守卫（client/core/win_exception_guard.py）。

背景：2026-10-26 在冻结包（PyInstaller）里复现到一种崩溃——

    Windows fatal exception: code 0x8001010d
    当前线程停在 QMessageBox.exec()（模态对话框），同时有工作线程在
    httpx socket 连接（探测后端是否就绪）。

`0x8001010d` = RPC_E_CALL_REJECTED「调用被目标端拒绝」。这是**瞬时**错误：
Windows 的 OLE 消息过滤器遇到这种情况的标准做法就是让调用方**稍后重试**，
而不是终止进程。Qt 自己的消息过滤器也是这么处理的。

但 Windows 默认行为是「未处理异常 → 终止进程」，faulthandler 只能把栈记下来，
进程照样死。所以这里注册一个**向量异常处理器**（AddVectoredExceptionHandler），
只对 0x8001010d 返回 EXCEPTION_CONTINUE_EXECUTION（放行），其余异常码一律
EXCEPTION_CONTINUE_SEARCH（交回系统，保持原样）。

安全边界：
    - 只影响这一个异常码，其它崩溃行为完全不变（不会被「吞掉」）
    - 非 Windows 平台直接返回 False（调用方跳过）
    - 处理器对象必须被模块级变量持有，否则被 GC 后句柄失效
"""
import ctypes
from typing import Any, Optional

#: RPC_E_CALL_REJECTED —— 「调用被拒绝」，瞬时，可重试
RPC_E_CALL_REJECTED = 0x8001010D

#: AddVectoredExceptionHandler 的返回值（必须保留，否则句柄失效）
_HANDLER: Optional[Any] = None
_INSTALLED = False


class _EXCEPTION_RECORD(ctypes.Structure):
    _fields_ = [
        ("ExceptionCode", ctypes.c_ulong),
        ("ExceptionFlags", ctypes.c_ulong),
        ("ExceptionRecord", ctypes.c_void_p),
        ("ExceptionAddress", ctypes.c_void_p),
        ("NumberParameters", ctypes.c_ulong),
        ("ExceptionInformation", ctypes.c_void_p * 15),
    ]


class _EXCEPTION_POINTERS(ctypes.Structure):
    _fields_ = [
        ("ExceptionRecord", ctypes.POINTER(_EXCEPTION_RECORD)),
        ("ContextRecord", ctypes.c_void_p),
    ]


def install_rpc_retry_filter() -> bool:
    """放行 0x8001010d（Windows only，幂等）。成功返回 True。"""
    global _HANDLER, _INSTALLED
    if _INSTALLED:
        return True
    if not hasattr(ctypes, "windll"):          # 非 Windows
        return False
    try:
        kernel32 = ctypes.windll.kernel32
    except Exception:  # noqa: BLE001
        return False

    EXCEPTION_CONTINUE_EXECUTION = 0
    EXCEPTION_CONTINUE_SEARCH = 1

    @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
    def _handler(ptr: int) -> int:
        try:
            exc = ctypes.cast(ptr, ctypes.POINTER(_EXCEPTION_POINTERS)).contents
            code = exc.ExceptionRecord.contents.ExceptionCode
        except Exception:  # noqa: BLE001 —— 取不到就按原样交给系统
            return EXCEPTION_CONTINUE_SEARCH
        if code == RPC_E_CALL_REJECTED:
            # 瞬时拒绝：放行，让 Qt 稍后重试
            return EXCEPTION_CONTINUE_EXECUTION
        return EXCEPTION_CONTINUE_SEARCH

    try:
        # 1 = 最先处理的向量处理器
        kernel32.AddVectoredExceptionHandler(1, _handler)
    except Exception:  # noqa: BLE001
        return False
    _HANDLER = _handler          # 必须持有引用，防止被 GC
    _INSTALLED = True
    return True


def is_installed() -> bool:
    return _INSTALLED
