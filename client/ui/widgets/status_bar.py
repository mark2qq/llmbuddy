# -*- coding: utf-8 -*-
"""
底部状态栏控件（status_bar.py）。

展示：服务状态（彩色圆点）、网关地址、已加载模型数/名称、版本号。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel


class StatusBar(QWidget):
    """主窗口底部状态栏。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 6, 14, 6)
        layout.setSpacing(14)

        self._dot = QLabel("●")
        self._dot.setObjectName("StatusDot")
        self._status = QLabel("服务未启动")
        self._url = QLabel("")
        self._models = QLabel("")
        self._ver = QLabel("v1.0.0")

        for w in (self._dot, self._status, self._url, self._models, self._ver):
            layout.addWidget(w)
        layout.addStretch(1)
        self._state_color = self._apply_state("stopped")

    # ------------------------------------------------------------------ #
    def _apply_state(self, state: str) -> str:
        color = {
            "running": "#3ddc84",
            "starting": "#ffb454",
            "stopped": "#ff6b6b",
            "error": "#ff6b6b",
        }.get(state, "#888888")
        self._dot.setStyleSheet(f"color:{color};font-size:14px;")
        return color

    def set_status(self, state: str = "stopped", url: str = "",
                   loaded: int = 0, model_names: list = None) -> None:
        """更新状态栏。"""
        self._apply_state(state)
        text = {
            "running": "服务运行中",
            "starting": "启动中…",
            "stopped": "服务未启动",
            "error": "服务异常",
        }.get(state, state)
        self._status.setText(text)
        self._url.setText(url)
        names = model_names or []
        self._models.setText(
            f"已加载模型: {loaded}  ({', '.join(names) if names else '无'})")
