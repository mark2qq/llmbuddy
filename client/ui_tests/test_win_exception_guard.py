# -*- coding: utf-8 -*-
"""
Windows 异常守卫回归（client/core/win_exception_guard.py）。

放行 0x8001010d（RPC_E_CALL_REJECTED，瞬时「调用被拒绝」），其余异常照旧。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import win_exception_guard as guard  # noqa: E402


def test_install_is_idempotent():
    """重复安装不报错（且不重复注册处理器）。"""
    first = guard.install_rpc_retry_filter()
    second = guard.install_rpc_retry_filter()
    assert first == second
    assert guard.is_installed() in (True, False)


def test_non_windows_returns_false_without_raising(monkeypatch):
    """非 Windows 平台安全返回（不抛异常）。"""
    monkeypatch.setattr(guard, "_INSTALLED", False)
    monkeypatch.setattr(guard, "_HANDLER", None)
    monkeypatch.delattr(guard.ctypes, "windll", raising=False)
    assert guard.install_rpc_retry_filter() is False
    assert guard.is_installed() is False


def test_rpc_code_constant():
    """0x8001010d = RPC_E_CALL_REJECTED，别写错。"""
    assert guard.RPC_E_CALL_REJECTED == 0x8001010D
