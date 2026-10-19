# -*- coding: utf-8 -*-
"""
加载 / 拉取进度弹窗（progress_dialog.py）。

通用进度对话框：显示标签 + 进度条 +（可选）取消按钮。
取消按钮通过 set_cancel_callback 注册回调，由调用方执行取消逻辑。
"""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QProgressBar,
                               QHBoxLayout, QPushButton)


class ProgressDialog(QDialog):
    """通用进度弹窗。"""

    def __init__(self, title: str = "进度", cancellable: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(440)
        self.setObjectName("ProgressDialog")
        self._cancel_cb = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        self._label = QLabel("准备中…")
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)

        layout.addWidget(self._label)
        layout.addWidget(self._bar)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setVisible(cancellable)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._close_btn = QPushButton("关闭")
        self._close_btn.setVisible(False)
        self._close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._close_btn)
        layout.addLayout(btn_row)

    def set_cancel_callback(self, cb) -> None:
        """注册取消回调（点击「取消」时调用）。"""
        self._cancel_cb = cb

    def _on_cancel(self) -> None:
        if self._cancel_cb:
            self._cancel_cb()

    def set_progress(self, frac: float, text: str = "") -> None:
        """设置进度（frac 0~1）与说明文本。"""
        pct = int(min(1.0, max(0.0, frac)) * 100)
        self._bar.setValue(pct)
        if text:
            self._label.setText(text)

    def set_indeterminate(self, text: str = "处理中…") -> None:
        """进入不确定进度模式（滚动条）。"""
        self._bar.setRange(0, 0)
        self._label.setText(text)

    def finish(self, success: bool, message: str = "") -> None:
        """任务结束：定格进度并露出「关闭」按钮。"""
        self._bar.setRange(0, 100)
        self._bar.setValue(100 if success else 0)
        self._label.setText(message or ("完成" if success else "失败"))
        self._cancel_btn.setVisible(False)
        self._close_btn.setVisible(True)
