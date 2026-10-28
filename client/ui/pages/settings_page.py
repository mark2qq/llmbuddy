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
                               QColorDialog, QFileDialog, QDialog)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt, Signal

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
        # 上限放到 1M：本地引擎上下文窗口可随显存拉到 300K~1M（KV cache 随 n_ctx
        # 线性膨胀，需足够显存）；助理侧 context_window 预算默认 300K，会按本处
        # n_ctx 再封顶，二者配合即「默认 300K、在线 1M」。
        self._n_ctx.setRange(512, 1000000)
        self._n_threads = QSpinBox()
        # 下限必须允许 0：config 里 0 = 按 CPU 核数自动。曾写成 setRange(1, 256)，
        # 打开设置页再点保存就把「自动」悄悄改成 1 线程（性能腰斩，并且触发一次
        # 无谓的后端重启）。
        self._n_threads.setRange(0, 256)
        self._n_threads.setSpecialValueText("自动")
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

        # 模型目录：手动输入 + 「浏览…」+ 实时统计目录里的 .gguf 数量。
        # 「浏览」必须用自绘对话框（DontUseNativeDialog）——本机冻结包里系统原生
        # 文件对话框会以 0x8001010E（COM 套间错误）直接杀死进程。
        models_row = QWidget()
        mrow = QHBoxLayout(models_row)
        mrow.setContentsMargins(0, 0, 0, 0)
        mrow.setSpacing(8)
        mrow.addWidget(self._models_dir, 1)
        self._browse_btn = QPushButton("浏览…")
        self._browse_btn.setToolTip("选择本地模型（.gguf）所在目录")
        self._browse_btn.clicked.connect(self._browse_models_dir)
        mrow.addWidget(self._browse_btn)
        form.addRow("模型目录:", models_row)

        self._models_hint = QLabel("")
        self._models_hint.setWordWrap(True)
        form.addRow("", self._models_hint)
        self._models_dir.textChanged.connect(self._refresh_models_hint)

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
        """自定义背景色（取色器）。

        **必须 DontUseNativeDialog**：`QColorDialog.getColor()` 这类静态便捷方法
        在 Windows 上用系统原生取色器（COM ChooseColor）。冻结包里调用它会以
        `0x8001010E RPC_E_WRONG_THREAD`（COM 套间错误）直接把进程杀掉，
        与文件对话框是同一条坑（crash.log 里只剩一行 Windows fatal exception）。
        """
        try:
            dlg = QColorDialog(QColor(self._bg_color or DEFAULT_DARK_BG), self)
            dlg.setOption(QColorDialog.ColorDialogOption.DontUseNativeDialog, True)
            dlg.setWindowTitle("选择背景色")
            dlg.setOption(QColorDialog.ColorDialogOption.ShowAlphaChannel, False)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            color = dlg.selectedColor()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "取色失败", f"打开取色器失败：{e}")
            return
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

    # ------------------------------------------------------------------ #
    # 模型目录
    # ------------------------------------------------------------------ #
    @staticmethod
    def _count_gguf(path: str) -> list:
        """列出目录里的 .gguf 文件名（失败/不存在返回空列表）。"""
        try:
            import os as _os
            if not path or not _os.path.isdir(path):
                return []
            return sorted(fn for fn in _os.listdir(path)
                          if fn.lower().endswith(".gguf"))
        except Exception:  # noqa: BLE001
            return []

    def _refresh_models_hint(self) -> None:
        """在输入框下方实时提示「这个目录里到底有几个模型」。"""
        path = self._models_dir.text().strip()
        try:
            import os as _os
            if not path:
                self._models_hint.setText("")
            elif not _os.path.isdir(path):
                self._models_hint.setText("⚠ 目录不存在，保存后模型库将取不到任何本地模型")
            else:
                files = self._count_gguf(path)
                if files:
                    # 只提示数量，不列模型名（模型清单在「模型库」页看）
                    self._models_hint.setText(f"该目录已有 {len(files)} 个模型")
                else:
                    self._models_hint.setText("该目录下还没有 .gguf 模型文件")
        except Exception:  # noqa: BLE001
            self._models_hint.setText("")

    def _browse_models_dir(self) -> None:
        """选择模型目录（自绘对话框，绝不能用系统原生对话框）。

        原生 QFileDialog 的静态方法在冻结包里会触发
        `0x8001010E RPC_E_WRONG_THREAD`（COM 套间错误）直接把进程杀掉，
        症状是「点浏览就闪退、crash.log 里只有一行 Windows fatal exception」。
        """
        start = self._models_dir.text().strip()
        try:
            import os as _os
            if not start or not _os.path.isdir(start):
                start = _os.path.expanduser("~")
        except Exception:  # noqa: BLE001
            start = ""
        try:
            fd = QFileDialog(self, "选择模型目录", start)
            fd.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            fd.setFileMode(QFileDialog.FileMode.Directory)
            fd.setOption(QFileDialog.Option.ShowDirsOnly, True)
            fd.setLabelText(QFileDialog.DialogLabel.LookIn, "位置：")
            fd.setLabelText(QFileDialog.DialogLabel.Accept, "选择此目录")
            fd.setLabelText(QFileDialog.DialogLabel.Reject, "取消")
            if fd.exec() != QDialog.DialogCode.Accepted:
                return
            picked = fd.selectedFiles()
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "选择失败", f"打开目录选择框失败：{e}")
            return
        if picked:
            self._models_dir.setText(str(picked[0]))
            self._refresh_models_hint()

    def _on_save(self) -> None:
        c = self.config
        # 先记下影响后端进程（环境变量）的旧值：这些改动不重启服务不会生效。
        # 尤其是模型目录——后端启动时从 LLM_MODELS_DIR 读进模块常量，
        # 光改 config.json 模型库照样列旧目录的模型（红字缺陷：改了目录没反应）。
        old = (c.models_dir, c.gateway_host, int(c.gateway_port),
               int(c.engine_base_port), int(c.n_ctx), int(c.n_threads),
               int(c.n_gpu_layers), int(c.max_concurrent_models), c.log_level)

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

        new = (c.models_dir, c.gateway_host, int(c.gateway_port),
               int(c.engine_base_port), int(c.n_ctx), int(c.n_threads),
               int(c.n_gpu_layers), int(c.max_concurrent_models), c.log_level)
        restart_needed = old != new
        try:
            c.save()
            # 先通知主窗口重新应用主题（立即生效），再处理需要重启才生效的项
            self.config_changed.emit()
            if restart_needed:
                self._restart_backend(old[0] != new[0])
            else:
                QMessageBox.information(self, "已保存", "设置已保存并立即生效")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "失败", f"保存失败: {e}")

    def _restart_backend(self, models_dir_changed: bool = False) -> None:
        """重启后端让新的环境变量生效（后台线程执行，不卡界面）。"""
        extra = "，模型目录已切换" if models_dir_changed else ""
        self._browse_btn.setEnabled(False)
        QMessageBox.information(
            self, "正在重启服务",
            f"设置已保存{extra}。\n以下改动需要重启后端进程才能生效，正在自动重启…\n"
            "重启完成后模型库会自动刷新。")

        def _do():
            import time as _t
            try:
                self.launcher.stop()
            except Exception:  # noqa: BLE001
                pass
            _t.sleep(0.8)
            return bool(self.launcher.start())

        w = Worker(_do)
        w.result.connect(self._on_restart_done)
        w.error.connect(lambda e: self._browse_btn.setEnabled(True))
        w.start()

    def _on_restart_done(self, ok) -> None:
        self._browse_btn.setEnabled(True)
        if ok:
            self._models_hint.setText("服务已重启，新设置已生效")
        else:
            self._models_hint.setText("服务重启失败，请到控制台手动启动")

