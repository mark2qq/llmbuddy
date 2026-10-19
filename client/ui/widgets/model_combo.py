# -*- coding: utf-8 -*-
"""
模型下拉选择控件（model_combo.py）。

数据来源：目录（catalog）中 local=True 的模型名（仅已下载模型）。
设计为纯下拉（不可编辑）：避免用户输入尚未下载的模型名，从根源上消除
加载时后端报「gguf 缺失」的问题。空列表时禁用并给出引导文案。
"""
from PySide6.QtWidgets import QComboBox
from PySide6.QtCore import Signal


class ModelComboBox(QComboBox):
    """模型选择下拉框（仅列已下载模型，纯下拉）。"""

    changed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ModelCombo")
        self.setMinimumWidth(240)
        # 纯下拉，禁止用户手动输入未下载的模型名（根源修复“gguf 缺失”）
        self.setEditable(False)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.currentTextChanged.connect(self.changed.emit)

    def set_models(self, names) -> None:
        """填充已下载模型名列表（纯下拉）。空列表时禁用并提示。"""
        current = self.currentText()
        self.clear()
        names = [n for n in (names or []) if n]
        if not names:
            # 无任何已下载模型：禁用下拉并给出引导
            self.addItem("（暂无已下载模型，请到模型库下载）")
            self.setEnabled(False)
            return
        self.setEnabled(True)
        for n in names:
            self.addItem(n)
        # 尽量保持之前选择；否则默认选中第一项
        idx = self.findText(current)
        if idx >= 0:
            self.setCurrentIndex(idx)
        elif self.count() > 0:
            self.setCurrentIndex(0)
