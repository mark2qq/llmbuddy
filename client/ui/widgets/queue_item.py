# -*- coding: utf-8 -*-
"""
引导会话队列车（ui/widgets/queue_item.py）——1.0.9 增量。

一行 = 序号 + 单行省略文本（hover tooltip 显示全文）+ 行内操作：
    ✎  行内编辑（QLineEdit 内联；**禁止模态框**——无头测试无法驱动，
       且项目有原生对话框 crash 前科）
    ↑ / ↓  排序（首条禁用 ↑、末条禁用 ↓，由页面 set_movable 控制）
    ✕  删除

引导消息（guided=True）是**只读行**：⚡ 标记 + 文本，没有任何操作按钮
——它已登记进对话流（客户端立即渲染了带 HH:MM 的用户气泡），不可改 /
删 / 排序，只能等出队；「进了对话流只读」与「队列内可改」由此分界。

信号（由页面接线到 REST，成功后的刷新统一等 queue 快照回来）：
    saved(str, str)        保存编辑（item_id, new_text）
    removed(str)           删除（item_id）
    moved(str, int)        上移/下移（item_id, delta）
    edit_cancelled()       取消编辑

全部样式走 objectName + QSS（#QueueRow/#QueueIdx/#QueueText/#QueueOp），
组件内不写死颜色，浅色主题由 theme.py 覆盖。
"""
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QSizePolicy)


class QueueItemRow(QFrame):
    """队列面板中的一条消息（普通行可改可删可排序，引导行只读）。"""

    saved = Signal(str, str)
    removed = Signal(str)
    moved = Signal(str, int)
    edit_cancelled = Signal()

    #: 文本省略宽度预算（与对话列同宽量级，留出序号与按钮位）
    TEXT_BUDGET = 560

    def __init__(self, item: Dict[str, Any], index: int, parent=None):
        super().__init__(parent)
        self.setObjectName("QueueRow")
        self._item_id = str(item.get("id") or "")
        self._guided = bool(item.get("guided"))
        self._full_text = str(item.get("text") or "")
        self._time_text = str(item.get("time") or "")
        self._editing = False
        self._editor: Optional[QLineEdit] = None
        self._ok_btn: Optional[QPushButton] = None
        self._cancel_btn: Optional[QPushButton] = None
        self._up_btn: Optional[QPushButton] = None
        self._down_btn: Optional[QPushButton] = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 6, 2)
        lay.setSpacing(6)

        idx = QLabel(f"{index + 1}")
        idx.setObjectName("QueueIdx")
        idx.setFixedWidth(16)
        idx.setAlignment(Qt.AlignCenter)
        lay.addWidget(idx)
        self._idx = idx          # 序号标签（快照重渲染时整体重建，不原地改）

        if self._guided:
            mark = QLabel("⚡")
            mark.setObjectName("QueueIdx")
            mark.setFixedWidth(16)
            mark.setAlignment(Qt.AlignCenter)
            mark.setToolTip("引导消息：已登记进对话流，只读（不可改 / 删 / 排序）")
            lay.addWidget(mark)

        self._text = QLabel(self._full_text)
        self._text.setObjectName("QueueText")
        self._text.setToolTip(self._tooltip())
        # 允许被布局横向压缩（窄窗口时省略号截断），不把面板撑宽
        self._text.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lay.addWidget(self._text, 1)

        if not self._guided:
            for label, tip, slot in (
                    ("✎", "编辑这条排队消息", self.begin_edit),
                    ("↑", "上移一位", lambda: self._emit_move(-1)),
                    ("↓", "下移一位", lambda: self._emit_move(1)),
                    ("✕", "删除这条排队消息", self._emit_remove)):
                btn = QPushButton(label)
                btn.setObjectName("QueueOp")
                btn.setFixedSize(22, 22)
                btn.setCursor(Qt.PointingHandCursor)
                btn.setToolTip(tip)
                btn.clicked.connect(slot)
                lay.addWidget(btn)
                if label == "↑":
                    self._up_btn = btn
                elif label == "↓":
                    self._down_btn = btn

        self._apply_elide()

    # ------------------------------------------------------------------ #
    # 对外
    # ------------------------------------------------------------------ #
    def set_movable(self, up: bool, down: bool) -> None:
        """首条禁用 ↑、末条禁用 ↓（引导行无按钮，调用无副作用）。"""
        if self._up_btn is not None:
            self._up_btn.setEnabled(bool(up))
        if self._down_btn is not None:
            self._down_btn.setEnabled(bool(down))

    def begin_edit(self) -> None:
        """进入行内编辑：文本位换成 QLineEdit + 保存/取消（非模态）。"""
        if self._guided or self._editing:
            return
        self._editing = True
        self._text.setVisible(False)
        for b in (self._up_btn, self._down_btn):
            if b is not None:
                b.setVisible(False)
        # 编辑与删除按钮也收起，避免误触（保存/取消后恢复）
        for b in self.findChildren(QPushButton):
            if b.objectName() == "QueueOp" and b.text() in ("✎", "✕"):
                b.setVisible(False)

        lay = self.layout()
        self._editor = QLineEdit(self._full_text)
        self._editor.setObjectName("QueueText")
        self._editor.setFrame(False)
        lay.insertWidget(1, self._editor, 1)

        self._ok_btn = QPushButton("保存")
        self._ok_btn.setObjectName("QueueOp")
        self._ok_btn.setFixedHeight(22)
        self._ok_btn.setCursor(Qt.PointingHandCursor)
        self._ok_btn.setToolTip("保存编辑（队列快照同步后生效）")
        self._ok_btn.clicked.connect(self._save)
        lay.insertWidget(2, self._ok_btn)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setObjectName("QueueOp")
        self._cancel_btn.setFixedHeight(22)
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.cancel_edit)
        lay.insertWidget(3, self._cancel_btn)
        self._editor.setFocus()

    def cancel_edit(self) -> None:
        """取消编辑：恢复文本与操作按钮，不发请求。"""
        if not self._editing:
            return
        self._exit_edit()
        self.edit_cancelled.emit()

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _save(self) -> None:
        if self._editor is None:
            return
        text = self._editor.text().strip()
        if not text:
            # 空文本不保存也不退出（留在编辑态，tooltip 说明）
            self._editor.setToolTip("消息不能为空")
            return
        self._exit_edit()
        self.saved.emit(self._item_id, text)

    def _exit_edit(self) -> None:
        self._editing = False
        for w in (self._editor, self._ok_btn, self._cancel_btn):
            if w is not None:
                self.layout().removeWidget(w)
                w.deleteLater()
        self._editor = None
        self._ok_btn = None
        self._cancel_btn = None
        self._text.setVisible(True)
        for b in self.findChildren(QPushButton):
            if b.objectName() == "QueueOp":
                b.setVisible(True)

    def _emit_move(self, delta: int) -> None:
        self.moved.emit(self._item_id, int(delta))

    def _emit_remove(self) -> None:
        self.removed.emit(self._item_id)

    def _tooltip(self) -> str:
        tip = self._full_text or "（空）"
        if self._time_text:
            tip += f"\n（{self._time_text} 入队）"
        if self._guided:
            tip += "\n引导消息：已登记进对话流，只读"
        else:
            tip += "\n可编辑 / 删除 / 排序；出队后只读"
        return tip

    def _apply_elide(self) -> None:
        """单行省略：按当前可用宽度截断，hover tooltip 始终给全文。

        未布局（无头测试）时 width() 为 0，退回自然宽度 → 不截断，
        测试可以直接断言整段文本。
        """
        full = self._full_text or ""
        if not full:
            self._text.setText("")
            return
        avail = self._text.width() or self._text.sizeHint().width()
        avail = max(60, min(avail, self.TEXT_BUDGET))
        self._text.setText(
            self._text.fontMetrics().elidedText(full, Qt.ElideRight, avail))

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if not self._editing:
            self._apply_elide()
