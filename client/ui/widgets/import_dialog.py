# -*- coding: utf-8 -*-
"""
手动添加对话框（import_dialog.py）。

用途：国内网络直连 HuggingFace 常被引擎/中间设备重置（WinError 10054），
本对话框允许用户粘贴 ModelScope / hf-mirror / GitHub Releases 等 .gguf 直链，
由客户端直接流式下载到 models_dir，绕过后端 pull 的 HF 直连链路。
"""
import os

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton)


def derive_name_from_url(url: str) -> str:
    """从 URL 推导模型文件名。

    规则：去掉查询串（?...）与锚点（#...），取路径 basename，并确保以 .gguf 结尾。
    """
    path = (url or "").split("?", 1)[0].split("#", 1)[0]
    name = os.path.basename(path.rstrip("/"))
    if not name:
        name = "model"
    if not name.lower().endswith(".gguf"):
        name += ".gguf"
    return name


class ImportDialog(QDialog):
    """「手动添加」对话框：填写 .gguf 直链与（可选）模型名。"""

    def __init__(self, free_ram_gb: float = 0.0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("手动添加模型")
        self.setMinimumWidth(560)
        self.setObjectName("ImportDialog")

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        self._url_edit = QLineEdit()
        self._url_edit.setPlaceholderText(
            "粘贴 .gguf 直链（支持 ModelScope / hf-mirror / GitHub Releases / HuggingFace）")
        layout.addWidget(QLabel("下载链接:"))
        layout.addWidget(self._url_edit)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("留空则自动取链接中的文件名")
        layout.addWidget(QLabel("模型名（可留空）:"))
        layout.addWidget(self._name_edit)

        tip = QLabel(
            f"本机空闲内存 {free_ram_gb:.1f} GB，建议选择 ≤ 空闲内存/2 的模型；"
            f"国内网络推荐 ModelScope 直链（HF 直连可能被重置）")
        tip.setWordWrap(True)
        tip.setObjectName("InfoLabel")
        layout.addWidget(tip)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        self._ok_btn = QPushButton("确定")
        self._ok_btn.setObjectName("PrimaryBtn")
        self._ok_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------ #
    # 取值
    # ------------------------------------------------------------------ #
    def get_url(self) -> str:
        """返回用户填写的下载直链（已 strip）。"""
        return self._url_edit.text().strip()

    def get_name(self) -> str:
        """返回模型文件名：用户填写优先，留空则从 URL 推导。

        两种情况都保证以 .gguf 结尾且不含查询串。
        """
        name = self._name_edit.text().strip()
        if name:
            if not name.lower().endswith(".gguf"):
                name += ".gguf"
            return name
        return derive_name_from_url(self.get_url())
