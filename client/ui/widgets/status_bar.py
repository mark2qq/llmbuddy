# -*- coding: utf-8 -*-
"""
底部状态栏控件（status_bar.py）。

展示：服务状态（彩色圆点）、网关地址、已加载模型数/名称、版本号。
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel

from app_config import APP_VERSION


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
        # 版本号取自 app_config.APP_VERSION（修复此前硬编码 v1.0.0 与实际版本不一致）
        self._ver = QLabel(f"v{APP_VERSION}")
        # 缓存当前已知「运行中」的模型名，避免 set_status 在未携带新模型信息时被重置为「0（无）」
        self._model_names = []

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
        """更新状态栏。

        红字修复：stopped/error 态明确无模型；running/starting 态若调用方
        未携带新的模型信息，则保留后台回填的模型名，避免被重置为「0（无）」。
        """
        self._apply_state(state)
        text = {
            "running": "服务运行中",
            "starting": "启动中…",
            "stopped": "服务未启动",
            "error": "服务异常",
        }.get(state, state)
        self._status.setText(text)
        self._url.setText(url)
        if state in ("stopped", "error"):
            # 服务已停/异常：明确没有运行中的模型
            self._model_names = []
            self._render_models([], fallback_count=0)
        else:
            # 运行中/启动中：无新信息则保留已回填的模型名
            self._render_models(model_names, fallback_count=loaded)

    def set_models(self, model_names: list) -> None:
        """只更新「已加载模型」段（红字：显示正在运行的模型名称）。

        供主窗口在后台拉取 pool 后回填，不改状态/URL 段。
        """
        self._model_names = list(model_names or [])
        self._render_models(self._model_names)

    def _render_models(self, model_names: list = None,
                       fallback_count: int = 0) -> None:
        # 调用方未提供任何模型信息（model_names=None 且 fallback_count=0）
        # → 保留缓存中已知运行中的模型名，避免被清空。
        if model_names is None and fallback_count == 0:
            names = self._model_names
        else:
            names = model_names or []
            self._model_names = names
        count = len(names) or fallback_count
        self._models.setText(
            f"已加载模型: {count}  ({', '.join(names) if names else '无'})")
