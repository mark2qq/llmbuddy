# -*- coding: utf-8 -*-
"""
原生对话框守卫回归（client/core/dialog_guard.py）。

背景：冻结包里调用原生 Win32 对话框会崩（QFileDialog 0x8001010E、
QMessageBox 0x8001010d）。守卫在启动时把静态入口换成
「实例 + DontUseNativeDialog + exec()」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import dialog_guard  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_patch_is_idempotent(app):
    """重复调用不会叠加替换。"""
    dialog_guard.patch_dialogs_non_native()
    first = QMessageBox.question
    dialog_guard.patch_dialogs_non_native()
    assert QMessageBox.question is first
    assert dialog_guard.is_patched() is True


def test_question_builds_box_with_non_native_option(app, monkeypatch):
    """question 必须构造实例并带上 DontUseNativeDialog。"""
    dialog_guard.patch_dialogs_non_native()
    seen = {}

    def _fake_exec(self):
        seen["option"] = self.testOption(QMessageBox.Option.DontUseNativeDialog)
        seen["title"] = self.windowTitle()
        seen["text"] = self.text()
        seen["buttons"] = self.standardButtons()
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "exec", _fake_exec, raising=False)
    out = QMessageBox.question(None, "标题", "内容",
                               QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
    assert seen["option"] is True          # 非原生实现
    assert seen["title"] == "标题"
    assert seen["text"] == "内容"
    assert seen["buttons"] & QMessageBox.StandardButton.Yes
    assert seen["buttons"] & QMessageBox.StandardButton.No
    assert out == QMessageBox.StandardButton.Yes


def test_warning_keeps_buttons_and_defaults(app, monkeypatch):
    """warning 也要带上非原生标记，且按钮集合不被改写。"""
    dialog_guard.patch_dialogs_non_native()
    seen = {}

    def _fake_exec(self):
        seen["option"] = self.testOption(QMessageBox.Option.DontUseNativeDialog)
        seen["buttons"] = self.standardButtons()
        seen["default"] = self.defaultButton().text()
        return QMessageBox.StandardButton.Ok

    monkeypatch.setattr(QMessageBox, "exec", _fake_exec, raising=False)
    QMessageBox.warning(None, "t", "c", QMessageBox.StandardButton.Yes
                         | QMessageBox.StandardButton.No,
                         QMessageBox.StandardButton.No)
    assert seen["option"] is True
    assert seen["buttons"] & QMessageBox.StandardButton.Yes
    assert "no" in (seen["default"] or "").lower()


def test_static_methods_replaced(app):
    """四个静态入口都被替换（不再是原生静态实现）。"""
    dialog_guard.patch_dialogs_non_native()
    for name in ("question", "information", "warning", "critical"):
        fn = getattr(QMessageBox, name)
        assert callable(fn)
        assert fn.__module__.startswith("core.dialog_guard") or \
            "dialog_guard" in (fn.__qualname__ or "")
