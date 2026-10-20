# -*- coding: utf-8 -*-
"""
设置页面（settings_page.py）。

功能映射（配置持久化到 config.json）：
    - 安装路径 / 网关端口 / 引擎基端口 / models_dir
    - n_ctx / n_threads / n_gpu_layers
    - max_concurrent / auto_restart / log_level

说明：端口类修改需重启后端生效；保存后通过 config_changed 信号通知主窗口。
"""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
                               QLineEdit, QSpinBox, QCheckBox, QComboBox,
                               QPushButton, QLabel, QMessageBox, QButtonGroup,
                               QColorDialog)
from PySide6.QtGui import QColor
from PySide6.QtCore import Signal

from core.task_runner import Worker
from core.theme import DEFAULT_DARK_BG, PRESET_BACKGROUNDS, normalize_color


class SettingsPage(QWidget):
    """设置页面。"""

    config_changed = Signal()

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._build_ui()
        self._load_from_config()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(16, 16, 16, 16)

        card = QGroupBox("运行设置")
        card.setObjectName("Card")
        form = QFormLayout(card)

        self._install = QLineEdit()
        self._install.setReadOnly(True)
        self._host = QLineEdit()
        self._port = QSpinBox()
        self._port.setRange(1, 65535)
        self._engine_port = QSpinBox()
        self._engine_port.setRange(1, 65535)
        self._models_dir = QLineEdit()
        self._n_ctx = QSpinBox()
        self._n_ctx.setRange(512, 131072)
        self._n_threads = QSpinBox()
        self._n_threads.setRange(1, 256)
        self._n_gpu = QSpinBox()
        self._n_gpu.setRange(0, 256)
        self._max_conc = QSpinBox()
        self._max_conc.setRange(0, 16)
        self._max_conc.setSpecialValueText("自动")
        self._auto_restart = QCheckBox("后端崩溃自动重启")
        self._log_level = QComboBox()
        self._log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])

        form.addRow("安装路径:", self._install)
        form.addRow("网关地址:", self._host)
        form.addRow("网关端口:", self._port)
        form.addRow("引擎基端口:", self._engine_port)
        form.addRow("模型目录:", self._models_dir)
        form.addRow("上下文长度 n_ctx:", self._n_ctx)
        form.addRow("线程数 n_threads:", self._n_threads)
        form.addRow("GPU 层数 n_gpu_layers:", self._n_gpu)
        form.addRow("并发上限(0=自动):", self._max_conc)
        form.addRow("日志级别:", self._log_level)
        form.addRow("", self._auto_restart)
        root.addWidget(card)

        # ---- 界面外观：背景色（保存后立即生效，无需重启） ----
        appear = QGroupBox("界面外观")
        appear.setObjectName("Card")
        aform = QFormLayout(appear)
        self._bg_color = DEFAULT_DARK_BG
        self._bg_group = QButtonGroup(self)
        self._bg_group.setExclusive(True)
        self._bg_btns = {}
        bg_row = QHBoxLayout()
        bg_row.setSpacing(8)
        for label, color in PRESET_BACKGROUNDS:
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setFixedSize(30, 24)
            btn.setToolTip(f"{label}（{color}）")
            btn.setStyleSheet(
                f"QPushButton {{ background-color: {color};"
                f" border: 1px solid #8a90a6; border-radius: 6px; }}"
                f"QPushButton:checked {{ border: 2px solid #6c8cff; }}")
            btn.clicked.connect(
                lambda _checked=False, col=color: self._select_bg(col))
            self._bg_group.addButton(btn)
            self._bg_btns[color] = btn
            bg_row.addWidget(btn)
        self._bg_custom = QPushButton("自定义…")
        self._bg_custom.clicked.connect(self._on_pick_color)
        bg_row.addWidget(self._bg_custom)
        bg_row.addStretch(1)
        self._bg_value = QLabel("默认深色")
        self._bg_value.setObjectName("MutedLabel")
        bg_row.addWidget(self._bg_value)
        aform.addRow("背景色:", bg_row)
        root.addWidget(appear)

        self._note = QLabel("提示：端口 / 引擎 / 模型目录 / 加载参数修改后需重启后端生效。")
        self._note.setWordWrap(True)
        root.addWidget(self._note)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        save = QPushButton("保存")
        save.setObjectName("PrimaryBtn")
        save.clicked.connect(self._on_save)
        btn_row.addWidget(save)
        root.addLayout(btn_row)
        root.addStretch(1)

    # ------------------------------------------------------------------ #
    # 界面外观（背景色）
    # ------------------------------------------------------------------ #
    def _select_bg(self, color: str) -> None:
        """选中背景色：仅更新预览，点「保存」后立即生效。"""
        c = normalize_color(color) or DEFAULT_DARK_BG
        self._bg_color = c
        preset_btn = self._bg_btns.get(c)
        if preset_btn is not None:
            preset_btn.setChecked(True)
            self._bg_custom.setStyleSheet("")
        else:
            # 自定义色：取消预设选中，并让「自定义…」按钮显示该颜色
            self._bg_group.setExclusive(False)
            for b in self._bg_btns.values():
                b.setChecked(False)
            self._bg_group.setExclusive(True)
            self._bg_custom.setStyleSheet(
                f"QPushButton {{ background-color: {c}; color: #ffffff;"
                f" border: 1px solid #8a90a6; border-radius: 8px;"
                f" padding: 4px 12px; }}")
        self._bg_value.setText("默认深色" if c == DEFAULT_DARK_BG else c)

    def _on_pick_color(self) -> None:
        """自定义背景色（取色器）。"""
        color = QColorDialog.getColor(
            QColor(self._bg_color or DEFAULT_DARK_BG), self, "选择背景色")
        if color.isValid():
            self._select_bg(color.name())

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def _load_from_config(self) -> None:
        c = self.config
        self._install.setText(c.install_path or "")
        self._host.setText(c.gateway_host)
        self._port.setValue(int(c.gateway_port))
        self._engine_port.setValue(int(c.engine_base_port))
        self._models_dir.setText(c.models_dir)
        self._n_ctx.setValue(int(c.n_ctx))
        self._n_threads.setValue(int(c.n_threads))
        self._n_gpu.setValue(int(c.n_gpu_layers))
        self._max_conc.setValue(int(c.max_concurrent_models))
        self._log_level.setCurrentText(c.log_level or "INFO")
        self._auto_restart.setChecked(bool(c.auto_restart))
        self._select_bg(getattr(c, "background_color", "") or DEFAULT_DARK_BG)

    def refresh(self) -> None:
        """切页/后端就绪时由主窗口调用：重新从配置加载显示。

        注意：MainWindow._switch_page 会对当前页直接调用 refresh()，
        缺失本方法会导致 AttributeError 原生崩溃（crash.log:
        'SettingsPage' object has no attribute 'refresh'）。
        """
        self._load_from_config()

    def _on_save(self) -> None:
        c = self.config
        c.gateway_host = self._host.text().strip() or "127.0.0.1"
        c.gateway_port = self._port.value()
        c.engine_base_port = self._engine_port.value()
        c.models_dir = self._models_dir.text().strip() or r"E:\llama-serve\models"
        c.n_ctx = self._n_ctx.value()
        c.n_threads = self._n_threads.value()
        c.n_gpu_layers = self._n_gpu.value()
        c.max_concurrent_models = self._max_conc.value()
        c.log_level = self._log_level.currentText()
        c.auto_restart = self._auto_restart.isChecked()
        # 背景色：默认深色存空串（与旧配置兼容）
        bg = self._bg_color or DEFAULT_DARK_BG
        c.background_color = "" if bg == DEFAULT_DARK_BG else bg
        try:
            c.save()
            # 先通知主窗口重新应用主题（立即生效），再弹保存成功提示
            self.config_changed.emit()
            QMessageBox.information(self, "已保存", "设置已保存并立即生效")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", f"保存失败: {e}")
