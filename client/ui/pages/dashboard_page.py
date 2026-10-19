# -*- coding: utf-8 -*-
"""
控制台页面（dashboard_page.py）。

功能映射：
    - 启动/停止服务      -> launcher.start() / launcher.stop()
    - 系统资源概览        -> M1 system_info()
    - 加载模型            -> M9 load_model() 后轮询 M10 model_status()
    - 取消加载            -> M11 unload_model()
    - 测试模型            -> M12 test_model()
    - 并发上限显示/设置   -> M13 concurrency() / M14 set_concurrency()
    - 运行日志            -> LogView（后端 stdout 实时转发）
"""
import html
import time
from typing import Optional

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QLabel, QPushButton, QSpinBox, QScrollArea,
                               QTableWidgetItem, QHeaderView, QMessageBox,
                               QFrame, QSizePolicy, QTextEdit)
from PySide6.QtCore import Qt, QTimer

from core.task_runner import Worker
from core.auto_service import AutoService
from ui.widgets.model_combo import ModelComboBox
from ui.widgets.progress_dialog import ProgressDialog
from ui.widgets.log_view import LogView


class DashboardPage(QWidget):
    """控制台页面。"""

    # 通知主窗口状态变化（可选）
    status_message = None

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._poll_timer: Optional[QTimer] = None
        self._poll_model: str = ""
        self._progress: Optional[ProgressDialog] = None
        self._row_widgets: dict = {}
        # 连接被拒（服务未运行）时自动启动后端并延时重试
        self._auto = AutoService(launcher, retry_cb=self.refresh)
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI 构建
    # ------------------------------------------------------------------ #
    def _card(self, title: str) -> QGroupBox:
        box = QGroupBox(title)
        box.setObjectName("Card")
        box.setLayout(QVBoxLayout())
        box.layout().setSpacing(10)
        return box

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        top = QHBoxLayout()
        top.setSpacing(16)

        # 左列：系统 + 并发（服务启停已移至系统托盘菜单）
        left = QVBoxLayout()
        left.setSpacing(16)
        left.addWidget(self._build_system_card())
        left.addWidget(self._build_concurrency_card())
        left.addStretch(1)

        # 右列：加载/测试 + 已加载列表
        right = QVBoxLayout()
        right.setSpacing(16)
        right.addWidget(self._build_load_card())
        right.addWidget(self._build_loaded_card(), 1)

        top.addLayout(left, 1)
        top.addLayout(right, 2)
        # 红字要求：已加载模型区域要大一些 -> 上半区占满剩余空间
        root.addLayout(top, 1)

        # 底部：运行日志（红字要求：日志区可以小一些，限制最大高度）
        log_card = QGroupBox("运行日志")
        log_card.setObjectName("Card")
        log_card.setLayout(QVBoxLayout())
        self._log = LogView()
        log_card.layout().addWidget(self._log)
        log_card.setMinimumHeight(110)
        log_card.setMaximumHeight(180)
        root.addWidget(log_card)

    def append_log(self, line: str) -> None:
        """追加一行后端日志（供主窗口转发 launcher.log）。"""
        if hasattr(self, "_log"):
            self._log.append(line)

    def _build_system_card(self) -> QGroupBox:
        card = self._card("系统资源")
        self._sys_label = QLabel("加载中…")
        self._sys_label.setObjectName("InfoLabel")
        self._sys_label.setWordWrap(True)
        card.layout().addWidget(self._sys_label)
        return card

    def _build_concurrency_card(self) -> QGroupBox:
        card = self._card("并发上限")
        self._conc_label = QLabel("加载中…")
        self._conc_label.setWordWrap(True)
        card.layout().addWidget(self._conc_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("设置:"))
        self._conc_spin = QSpinBox()
        self._conc_spin.setRange(1, 16)
        self._conc_spin.setValue(1)
        row.addWidget(self._conc_spin)
        self._conc_apply = QPushButton("应用")
        self._conc_apply.clicked.connect(self._on_set_concurrency)
        row.addWidget(self._conc_apply)
        row.addStretch(1)
        card.layout().addLayout(row)
        return card

    def _build_load_card(self) -> QGroupBox:
        card = self._card("模型加载 / 测试")
        row = QHBoxLayout()
        row.addWidget(QLabel("模型:"))
        self._model_combo = ModelComboBox()
        row.addWidget(self._model_combo, 1)
        card.layout().addLayout(row)

        btn_row = QHBoxLayout()
        self._load_btn = QPushButton("加载")
        self._load_btn.setObjectName("PrimaryBtn")
        self._test_btn = QPushButton("测试")
        self._load_btn.clicked.connect(self._on_load)
        self._test_btn.clicked.connect(self._on_test)
        btn_row.addWidget(self._load_btn)
        btn_row.addWidget(self._test_btn)
        btn_row.addStretch(1)
        card.layout().addLayout(btn_row)

        # 测试结果：内容可能很长（延迟 + 样例全文），用只读文本框自带滚动条，
        # 避免 QLabel 被裁切看不全（红字：测试时没有显示全，建议加滚动）
        self._test_label = QTextEdit()
        self._test_label.setObjectName("TestResult")
        self._test_label.setReadOnly(True)
        self._test_label.setFixedHeight(96)
        self._test_label.setPlaceholderText(
            "测试结果将显示在这里（内容较长时可滚动查看）")
        card.layout().addWidget(self._test_label)
        return card

    def _build_loaded_card(self) -> QGroupBox:
        card = self._card("已加载模型")
        # 卡片式布局：每行一个模型卡片（名称 + 参数摘要 + 正常尺寸操作按钮），
        # 替代原 8 列密集表格（12px 微型按钮看不清）。
        self._loaded_scroll = QScrollArea()
        self._loaded_scroll.setWidgetResizable(True)
        self._loaded_scroll.setObjectName("LoadedScroll")
        self._loaded_container = QWidget()
        self._loaded_layout = QVBoxLayout(self._loaded_container)
        self._loaded_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._loaded_layout.setSpacing(8)
        self._loaded_layout.setContentsMargins(2, 2, 2, 2)
        self._loaded_scroll.setWidget(self._loaded_container)
        self._loaded_scroll.setMinimumHeight(120)
        card.layout().addWidget(self._loaded_scroll)
        self._empty_loaded = QLabel("暂无已加载模型")
        self._empty_loaded.setObjectName("MutedLabel")
        self._empty_loaded.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card.layout().addWidget(self._empty_loaded)
        return card

    def _render_loaded(self, loaded: list) -> None:
        """渲染已加载模型卡片列表。"""
        # 清空旧卡片
        self._row_widgets = {}
        while self._loaded_layout.count():
            item = self._loaded_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._empty_loaded.setVisible(not loaded)
        self._loaded_scroll.setVisible(bool(loaded))
        for it in loaded:
            name = str(it.get("name", ""))
            status = str(it.get("status", ""))
            progress = f"{int(float(it.get('progress', 0) or 0) * 100)}%"
            card = QFrame()
            card.setObjectName("LoadedCard")
            row = QHBoxLayout(card)
            row.setContentsMargins(12, 8, 12, 8)
            row.setSpacing(12)

            left = QVBoxLayout()
            left.setSpacing(2)
            title = QLabel(name)
            title.setObjectName("LoadedName")
            meta = QLabel(
                f"状态: {status} · 进度: {progress} · "
                f"上下文: {it.get('n_ctx', '-')} · 线程: {it.get('n_threads', '-')} · "
                f"GPU层: {it.get('n_gpu_layers', '-')}")
            meta.setObjectName("MutedLabel")
            left.addWidget(title)
            left.addWidget(meta)
            row.addLayout(left, 1)

            # 启停合并为单个切换按钮（红字要求：启动和停止不要同时出现，
            # 启动了显示停止，停止了显示启动）：
            #   ready    运行中   -> 「停止」（点击暂停运行，保留加载）
            #   paused   已加载未运行 -> 「启动」（点击恢复运行）
            #   loading  加载中   -> 「加载中…」置灰（仅可「取消加载」）
            #   error/其它 -> 「启动」（点击直接加载运行）
            running = status == "ready"
            loading = status in ("loading", "starting")
            toggle_btn = QPushButton("停止" if running else
                                     ("加载中…" if loading else "启动"))
            toggle_btn.setObjectName("RowDangerBtn" if running else "RowBtn")
            if running:
                toggle_btn.setToolTip("停止运行该模型（保留加载，可再次启动）")
                toggle_btn.clicked.connect(
                    lambda _checked=False, n=name: self._on_row_stop(n))
            elif loading:
                toggle_btn.setToolTip("模型加载中，可点击「取消加载」中止")
                toggle_btn.setEnabled(False)
            else:
                toggle_btn.setToolTip("运行该模型（占用一个运行名额）")
                toggle_btn.clicked.connect(
                    lambda _checked=False, n=name: self._on_row_start(n))
            cancel_btn = QPushButton("取消加载")
            cancel_btn.setObjectName("RowCancelBtn")
            cancel_btn.setToolTip("从内存池中移除该模型")
            cancel_btn.setEnabled(True)
            cancel_btn.clicked.connect(
                lambda _checked=False, n=name: self._on_row_cancel(n))
            row.addStretch(0)
            row.addWidget(toggle_btn)
            row.addWidget(cancel_btn)
            self._loaded_layout.addWidget(card)
            # 记录行控件与状态，便于操作时立即整行置灰（防止重复点击），
            # 恢复时按状态决定切换按钮是否可用（加载中保持置灰）
            self._row_widgets[name] = (card, toggle_btn, cancel_btn, loading)

    def _set_row_enabled(self, name: str, enabled: bool) -> None:
        """操作期间整行按钮置灰/恢复（红字要求：停止后按钮变灰不能操作）。

        恢复时按渲染时的状态决定：加载中的行切换按钮保持置灰。
        """
        row = self._row_widgets.get(name)
        if row is None:
            return
        _card, toggle_btn, cancel_btn, loading = row
        cancel_btn.setEnabled(enabled)
        toggle_btn.setEnabled(enabled and not loading)

    def _on_row_cancel(self, name: str) -> None:
        """行内「取消加载」：卸载该模型（终止加载/运行）。"""
        if not name:
            return
        self._set_row_enabled(name, False)

        def _do():
            return self.api.unload_model(name)

        def _ok(_d):
            self.append_log(f"已取消加载模型 {name}")
            self.refresh()

        def _err(e):
            self._set_row_enabled(name, True)
            self.append_log(f"[取消加载失败] {e}")

        self._bg(_do, _ok, _err)

    # ------------------------------------------------------------------ #
    # 后台调用辅助
    # ------------------------------------------------------------------ #
    def _bg(self, fn, on_ok, on_err=None):
        w = Worker(fn)
        w.result.connect(on_ok)
        if on_err:
            w.error.connect(on_err)
        else:
            def _default_err(e: str) -> None:
                # 连接被拒（服务未运行）→ 自动启动服务并延时重试，否则按原样记录
                if not self._auto.handle(e, lambda m: self.append_log(f"[服务] {m}")):
                    self.append_log(f"[错误] {e}")
            w.error.connect(_default_err)
        w.start()
        return w

    # ------------------------------------------------------------------ #
    # 并发上限
    # ------------------------------------------------------------------ #
    def _on_set_concurrency(self) -> None:
        n = self._conc_spin.value()

        def _do():
            self.api.set_concurrency(n)
            return self.api.concurrency()

        def _ok(d):
            self._fill_concurrency(d)
            self.append_log("并发上限已更新")

        self._bg(_do, _ok)

    def _fill_concurrency(self, d: dict) -> None:
        if not d:
            return
        cur = d.get("max_concurrent", "?")
        sug = d.get("suggested", "?")
        reason = d.get("reason", "")
        self._conc_label.setText(
            f"当前上限: <b>{cur}</b>    系统建议: <b>{sug}</b>\n{reason}")
        try:
            self._conc_spin.setValue(int(cur))
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # 模型加载 / 取消 / 测试
    # ------------------------------------------------------------------ #
    def _on_load(self) -> None:
        name = self._model_combo.currentText().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请选择或输入模型名")
            return
        cfg = self.config

        def _do():
            return self.api.load_model(name, n_ctx=cfg.n_ctx,
                                       n_threads=cfg.n_threads,
                                       n_gpu_layers=cfg.n_gpu_layers)

        def _ok(d):
            d = d or {}
            if d.get("note"):
                self.append_log(d["note"])
            self._start_poll(name)

        self._progress = ProgressDialog(title=f"加载模型 {name}", cancellable=True, parent=self)
        self._progress.set_cancel_callback(lambda: self._cancel_poll(name))
        self._progress.show()
        self._bg(_do, _ok, lambda e: (self._progress.finish(False, f"失败: {e}"),
                                      self.append_log(f"[加载失败] {e}")))

    def _start_poll(self, name: str) -> None:
        self._poll_model = name
        if self._progress:
            self._progress.set_indeterminate(f"正在加载 {name} …")
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_tick)
        self._poll_timer.start(1000)

    def _poll_tick(self) -> None:
        name = self._poll_model
        if not name:
            return
        try:
            st = self.api.model_status(name)
        except Exception as e:
            if self._progress:
                self._progress.finish(False, f"查询失败: {e}")
            self._stop_poll()
            return
        status = st.get("status", "")
        progress = float(st.get("progress", 0.0) or 0.0)
        if self._progress:
            self._progress.set_progress(progress, f"{name}: {status} ({int(progress*100)}%)")
        if status == "ready":
            if self._progress:
                self._progress.finish(True, f"{name} 加载完成")
            self._stop_poll()
            self.refresh()
        elif status == "paused":
            if self._progress:
                self._progress.finish(False, f"{name} 已加载但未运行"
                                                 "（运行名额已满，请先停止一个模型）")
            self._stop_poll()
            self.append_log(f"[运行名额已满] {name} 保持加载待命")
            self.refresh()
        elif status == "error":
            if self._progress:
                self._progress.finish(False, f"{name} 加载失败: {st.get('error', '')}")
            self._stop_poll()
            self.refresh()

    def _stop_poll(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._poll_model = ""

    def _cancel_poll(self, name: str) -> None:
        try:
            self.api.unload_model(name)
        except Exception:
            pass
        self._stop_poll()
        if self._progress:
            self._progress.finish(False, "已取消加载")

    def _on_cancel_load(self) -> None:
        name = self._model_combo.currentText().strip()
        if not name:
            return
        self._cancel_poll(name)

    def _on_test(self) -> None:
        name = self._model_combo.currentText().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请选择模型")
            return

        def _do():
            return self.api.test_model(name, prompt="你好，简单介绍一下你自己。")

        def _ok(d):
            ok = d.get("ok", False)
            latency = d.get("latency_ms", 0)
            # 不截断样例：文本框可滚动，完整展示模型输出（含特殊字符转义）
            sample = html.escape(str(d.get("sample") or ""))
            color = "#3ddc84" if ok else "#ff6b6b"
            self._test_label.setHtml(
                f'<span style="color:{color}"><b>{"通过" if ok else "失败"}</b></span>'
                f'&nbsp;&nbsp;延迟: {latency} ms<br>样例: {sample}')
            self.refresh()

        self._bg(_do, _ok,
                 lambda e: self._test_label.setPlainText(f"测试失败: {e}"))

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """刷新本页所有数据（后端就绪后调用）。"""
        def _sys_ok(d):
            if not d:
                self._sys_label.setText("无法获取系统信息")
                return
            gpu = d.get("gpu_name") or ("不可用" if not d.get("gpu_available") else "可用")
            self._sys_label.setText(
                f"CPU 核心: {d.get('cpu_count')} 核 / {d.get('cpu_threads')} 线程<br>"
                f"内存: {d.get('free_ram_gb')} / {d.get('total_ram_gb')} GB 空闲<br>"
                f"GPU: {gpu}（显存 {d.get('free_vram_gb')}/{d.get('total_vram_gb')} GB）<br>"
                f"系统建议并发模型数: {d.get('suggested_max_models')}"
            )

        self._bg(lambda: self.api.system_info(), _sys_ok)
        self._bg(lambda: self.api.concurrency(), self._fill_concurrency)
        # 下拉只列已下载模型：从目录（catalog）过滤 local=True 的名称，
        # 根源修复“gguf 缺失”（此前误用 list_models() 仅返回注册名，不代表已下载）。
        def _catalog_ok(d):
            items = (d or {}).get("items", []) or []
            local_names = [it.get("name") for it in items if it.get("local")]
            local_names = [n for n in local_names if n]
            self._refresh_models_and_table(local_names)
        self._bg(lambda: self.api.catalog(force=False), _catalog_ok)

    def _refresh_models_and_table(self, local_names) -> None:
        # 下拉只列已下载（local=True）的模型：根源修复“gguf 缺失”
        self._model_combo.set_models(local_names)

        try:
            pool = self.api.pool()
        except Exception:
            pool = {}
        loaded = pool.get("loaded", []) or []
        self._render_loaded(loaded)

    def _on_row_start(self, name: str) -> None:
        """行内「启动」：运行该模型（已加载未运行则恢复运行，否则直接加载运行）。"""
        if not name:
            return
        cfg = self.config
        # 操作期间整行置灰，防止重复点击
        self._set_row_enabled(name, False)
        self.append_log(f"正在启动模型 {name} …")

        def _do():
            try:
                st = (self.api.model_status(name) or {}).get("status", "")
            except Exception:
                st = ""
            if st == "paused":
                # 已加载未运行 -> 恢复运行（运行名额不足会返回 1005 明确提示）
                return self.api.resume_model(name)
            return self.api.load_model(
                name, n_ctx=cfg.n_ctx, n_threads=cfg.n_threads,
                n_gpu_layers=cfg.n_gpu_layers)

        def _ok(d):
            d = d or {}
            if d.get("status") == "paused" or d.get("note"):
                self.append_log(d.get("note") or
                                "已加载但未运行（运行名额已满）")
            self._start_poll(name)

        def _err(e):
            self._set_row_enabled(name, True)
            self._progress.finish(False, f"失败: {e}")
            self.append_log(f"[启动失败] {e}")
            self.refresh()

        self._progress = ProgressDialog(
            title=f"启动模型 {name}", cancellable=True, parent=self)
        self._progress.set_cancel_callback(lambda: self._cancel_poll(name))
        self._progress.show()
        self._bg(_do, _ok, _err)

    def _on_row_stop(self, name: str) -> None:
        """行内「停止」：暂停该模型运行（保留加载，可再次启动）。"""
        if not name:
            return
        # 红字要求：点停止后整行按钮立即变灰，防止重复操作
        self._set_row_enabled(name, False)
        self.append_log(f"正在停止模型 {name} …")

        def _do():
            return self.api.pause_model(name)

        def _ok(_d):
            self.append_log(f"已停止运行模型 {name}（仍保持加载，可再次启动）")
            self.refresh()

        def _err(e):
            self._set_row_enabled(name, True)
            self.append_log(f"[停止失败] {e}")

        self._bg(_do, _ok, _err)
