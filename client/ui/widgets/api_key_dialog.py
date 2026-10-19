# -*- coding: utf-8 -*-
"""
API Key 生成展示对话框（api_key_dialog.py）。

展示 M7 生成的 API Key，并支持一键复制。
"""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLineEdit, QPushButton,
                               QHBoxLayout, QLabel, QApplication)


class ApiKeyDialog(QDialog):
    """API Key 展示对话框。"""

    def __init__(self, key: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("API Key")
        self.setMinimumWidth(480)
        self.setObjectName("ApiKeyDialog")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.addWidget(QLabel("请妥善保存以下 API Key（仅展示一次）："))

        self._edit = QLineEdit(key)
        self._edit.setReadOnly(True)
        self._edit.setObjectName("ApiKeyField")
        layout.addWidget(self._edit)

        h = QHBoxLayout()
        h.addStretch(1)
        copy = QPushButton("复制")
        copy.clicked.connect(self._copy)
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        h.addWidget(copy)
        h.addWidget(close)
        layout.addLayout(h)

    def _copy(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self._edit.text())
        self._edit.selectAll()
