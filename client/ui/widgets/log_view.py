# -*- coding: utf-8 -*-
"""
实时日志视图（log_view.py）。

纯文本、只读、自动滚到底部；支持清空与追加。
"""
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit


class LogView(QWidget):
    """日志视图控件。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setObjectName("LogView")
        layout.addWidget(self.text)

    def append(self, line: str) -> None:
        """追加一行日志并滚动到底部。"""
        self.text.appendPlainText(line)
        sb = self.text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self) -> None:
        """清空日志。"""
        self.text.clear()
