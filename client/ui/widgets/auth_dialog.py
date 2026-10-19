# -*- coding: utf-8 -*-
"""
GitHub 账号 / Token 输入对话框（auth_dialog.py）。

用于需要鉴权的模型下载（HuggingFace Gated 等）。
"""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QFormLayout, QLineEdit,
                               QPushButton, QLabel)


class AuthDialog(QDialog):
    """GitHub 认证输入对话框。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GitHub 认证")
        self.setMinimumWidth(400)
        self.setObjectName("AuthDialog")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        hint = QLabel("部分模型下载需要 GitHub Token（HuggingFace 账号授权后获取）。")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self._user = QLineEdit()
        self._token = QLineEdit()
        self._token.setEchoMode(QLineEdit.Password)
        form.addRow("账号:", self._user)
        form.addRow("Token:", self._token)
        layout.addLayout(form)

        h = QHBoxLayout()
        h.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        ok = QPushButton("确定")
        ok.clicked.connect(self.accept)
        h.addWidget(cancel)
        h.addWidget(ok)
        layout.addLayout(h)

    def get_auth(self) -> dict:
        """返回鉴权信息 {user, token}。"""
        return {
            "user": self._user.text().strip(),
            "token": self._token.text().strip(),
        }
