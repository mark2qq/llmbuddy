# -*- coding: utf-8 -*-
"""
原生对话框守卫（client/core/dialog_guard.py）。

背景：PyInstaller 冻结包里调用 **原生 Win32 对话框**会崩。

    - QFileDialog：早期就踩过，0x8001010E（原生窗口句柄在冻结包里不可用）
    - QMessageBox：2026-10-26 实测 0x8001010d —— 后端未运行时启动客户端，
      启动引导弹「启动本地服务」QMessageBox.question → 主线程挂在弹窗里崩掉

两种崩溃的共同点：Qt 默认把 QMessageBox / QColorDialog 交给 Windows 原生实现，
冻结包环境下这条路径不可用。QFileDialog 已在各处显式加 `DontUseNativeDialog`，
但 QMessageBox 有 40 多处调用点、QColorDialog 也散落各处，逐个加参数容易漏。

这里改成**启动时统一替换**：所有静态入口换成「构造实例 + setOption(DontUseNativeDialog)
+ exec()」，一处生效、全局覆盖，调用点一行都不用改。

放在 core/ 而不是 ui/：main.py 在建窗口前就要调用，且 ui_tests 也能直接测。

注意：PySide6 里 `Option` 与 `StandardButton` 是**不同枚举，不能直接 OR**
（`StandardButton.Yes | Option.DontUseNativeDialog` 会 TypeError），
所以只能走「实例 + setOption」这条路。
QInputDialog 没有 DontUseNativeDialog 选项（它默认就是 Qt 实现），保持原样。
"""
from typing import Any, Callable, Optional

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QColorDialog, QDialog, QMessageBox

#: 已经被替换过（重复调用本模块时不会叠加替换）
_PATCHED = False


def _make_msgbox(icon: "QMessageBox.Icon", parent: Any, title: str,
                 text: str, buttons: Any, default: Any):
    """构造一个禁用原生实现的消息框。"""
    box = QMessageBox(icon, title, text, buttons, parent)
    box.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    if default is not None:
        try:
            box.setDefaultButton(default)
        except Exception:  # noqa: BLE001 —— 默认按钮非法时忽略
            pass
    return box


def _wrap_msgbox(icon: "QMessageBox.Icon", default_buttons: Any) -> Callable:
    """把 QMessageBox 的静态方法换成「实例 + DontUseNativeDialog + exec」。"""

    def wrapper(parent: Any = None, title: str = "", text: str = "",
                buttons: Any = None, defaultButton: Any = None,
                *args: Any, **kwargs: Any) -> Any:
        if buttons is None:
            buttons = kwargs.pop("buttons", None) or default_buttons
        if defaultButton is None:
            defaultButton = kwargs.pop("defaultButton", None)
        box = _make_msgbox(icon, parent, title, text, buttons, defaultButton)
        return box.exec()

    return wrapper


def _wrap_get_color() -> Callable:
    """QColorDialog.getColor → 实例 + ColorDialogOption.DontUseNativeDialog。"""

    def wrapper(parent: Any = None, title: str = "",
                initial: Any = None) -> Any:
        dlg = QColorDialog(parent)
        if title:
            dlg.setWindowTitle(title)
        if initial is not None:
            try:
                dlg.setCurrentColor(initial)
            except Exception:  # noqa: BLE001
                pass
        dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            # 取消：返回无效颜色（与原生 getColor 的语义一致）
            return QColor()
        return dlg.currentColor()

    return wrapper


def patch_dialogs_non_native() -> None:
    """全局禁用原生对话框（幂等，可重复调用）。"""
    global _PATCHED
    if _PATCHED:
        return
    std = QMessageBox.StandardButton
    QMessageBox.question = _wrap_msgbox(
        QMessageBox.Icon.Question, std.Yes | std.No)
    QMessageBox.information = _wrap_msgbox(
        QMessageBox.Icon.Information, std.Ok)
    QMessageBox.warning = _wrap_msgbox(
        QMessageBox.Icon.Warning, std.Ok)
    QMessageBox.critical = _wrap_msgbox(
        QMessageBox.Icon.Critical, std.Ok)
    try:
        QColorDialog.getColor = _wrap_get_color()
    except Exception:  # noqa: BLE001 —— 个别版本签名不同，失败就算了
        pass
    _PATCHED = True


def is_patched() -> bool:
    """是否已打过补丁（测试用）。"""
    return _PATCHED
