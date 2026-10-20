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

2026-10-20 改版（v3 布局）：四行纵向堆叠
    第 1 行 运行监控与参数：内存环形图 / CPU 线程柱状 / GPU / 并发上限滑块（图形化）
    第 2 行 模型加载 / 测试
    第 3 行 已加载模型（卡片列表，可滚动）
    第 4 行 运行日志（限高）
"""
import html
import time
from collections import deque
from typing import Optional

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QLabel, QPushButton, QScrollArea,
                               QTableWidgetItem, QHeaderView, QMessageBox,
                               QFrame, QSizePolicy, QTextEdit, QSlider)
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtCore import Qt, QTimer, QByteArray, Signal

from core.task_runner import Worker
from core.auto_service import AutoService
from ui.widgets.model_combo import ModelComboBox
from ui.widgets.progress_dialog import ProgressDialog
from ui.widgets.log_view import LogView


class DashboardPage(QWidget):
    """控制台页面。"""

    # 模型池快照变化（每次 refresh 拿到 pool 数据后发出，携带 loaded 列表）。
    # 主窗口据此即时更新状态栏「运行中模型」段（红字需求）。
    pool_changed = Signal(list)

    # 监控卡配色（沿用应用深色主题）
    _ACCENT = "#7C6FF0"
    _TRACK = "#2A3040"
    _GREEN = "#3DD68C"
    _MUTED = "#8A90A6"

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._poll_timer: Optional[QTimer] = None
        self._poll_model: str = ""
        self._progress: Optional[ProgressDialog] = None
        self._row_widgets: dict = {}
        # CPU 实时曲线历史（最近 60 个采样点，2s 一点）
        self._cpu_hist: deque = deque(maxlen=60)
        self._mon_busy = False
        # 连接被拒（服务未运行）时自动启动后端并延时重试
        self._auto = AutoService(launcher, retry_cb=self.refresh)
        self._build_ui()
        # 实时监控定时器：内存环形图 + CPU 曲线（红字要求「要实时运行」）
        self._mon_timer = QTimer(self)
        self._mon_timer.timeout.connect(self._mon_tick)
        self._mon_timer.start(2000)

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
        # 结构：上部「监控 + 加载 + 已加载模型」放进滚动区，运行日志固定在最底部。
        # 红字反馈：模型加载多了会把运行日志挤下去——日志固定后，模型再多也只在
        # 上方区域内部滚动（已加载模型卡片自带滚动），日志始终可见。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(0)
        self._page_scroll = QScrollArea()
        self._page_scroll.setWidgetResizable(True)
        self._page_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._page_scroll.setObjectName("PageScroll")
        # 只纵向滚动：横向由内容自适应宽度，避免出现横向滚动条
        self._page_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("PageContent")
        root = QVBoxLayout(content)
        root.setSpacing(10)
        root.setContentsMargins(0, 0, 0, 0)

        # ---- 第 1 行：运行监控与参数（四卡一排）----
        monitor = QHBoxLayout()
        monitor.setSpacing(12)
        self._mem_donut, self._mem_gb, self._mem_hint = (None, None, None)
        self._cpu_text = None
        self._gpu_text = None
        monitor.addWidget(self._build_memory_card())
        monitor.addWidget(self._build_cpu_card())
        monitor.addWidget(self._build_gpu_card())
        monitor.addWidget(self._build_concurrency_card())
        root.addLayout(monitor)

        # ---- 第 2 行：模型加载 / 测试 ----
        root.addWidget(self._build_load_card())

        # ---- 第 3 行：已加载模型（占满剩余空间，内部滚动）----
        root.addWidget(self._build_loaded_card(), 1)

        self._page_scroll.setWidget(content)
        outer.addWidget(self._page_scroll, 1)
        outer.addSpacing(12)

        # ---- 底部固定：运行日志（不参与上部滚动，模型再多也不会被挤走）----
        log_card = QGroupBox("运行日志")
        log_card.setObjectName("Card")
        log_card.setLayout(QVBoxLayout())
        self._log = LogView()
        log_card.layout().addWidget(self._log)
        log_card.setMinimumHeight(110)
        log_card.setMaximumHeight(124)
        outer.addWidget(log_card)

    def append_log(self, line: str) -> None:
        """追加一行后端日志（供主窗口转发 launcher.log）。"""
        if hasattr(self, "_log"):
            self._log.append(line)

    # ------------------------------------------------------------------ #
    # 第 1 行：监控卡（图形化）
    # ------------------------------------------------------------------ #
    def _render_donut(self, percent: float, color: str,
                      size: int = 78) -> QPixmap:
        """渲染内存使用环形图（含中心百分比），返回 QPixmap。"""
        r = 34
        cx = cy = size / 2
        circ = 2 * 3.14159265 * r
        pct = max(0.0, min(100.0, percent))
        dash = circ * pct / 100.0
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
            f'viewBox="0 0 {size} {size}">'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" '
            f'stroke="{self._TRACK}" stroke-width="8"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" '
            f'stroke-width="8" stroke-linecap="round" '
            f'stroke-dasharray="{dash:.1f} {circ:.1f}" '
            f'transform="rotate(-90 {cx} {cy})"/>'
            f'<text x="{cx}" y="{cy - 1}" text-anchor="middle" '
            f'fill="#E8EAF2" font-size="15" font-weight="500">{pct:.0f}%</text>'
            f'<text x="{cx}" y="{cy + 13}" text-anchor="middle" '
            f'fill="{self._MUTED}" font-size="10">已用内存</text>'
            f'</svg>'
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        return pm

    def _render_cpu_graph(self, values, w: int = 130, h: int = 46) -> QPixmap:
        """渲染 CPU 使用率实时面积折线图（任务管理器风格）。

        values 为历史采样序列（最多 60 点），不足部分左侧补 0。
        """
        n = 60
        vals = list(values)[-n:]
        vals = [0.0] * (n - len(vals)) + vals
        pts = []
        for i, v in enumerate(vals):
            x = i * (w - 2) / (n - 1) + 1
            v = max(0.0, min(100.0, float(v)))
            y = h - 1 - (v / 100.0) * (h - 3)
            pts.append(f"{x:.1f},{y:.1f}")
        poly = " ".join(pts)
        area = f"1,{h - 1} {poly} {w - 1},{h - 1}"
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {w} {h}">'
            f'<rect x="0" y="0" width="{w}" height="{h}" rx="4" '
            f'fill="{self._TRACK}" fill-opacity="0.22"/>'
            f'<polygon points="{area}" fill="{self._ACCENT}" fill-opacity="0.30"/>'
            f'<polyline points="{poly}" fill="none" stroke="{self._ACCENT}" '
            f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>'
            f'</svg>'
        )
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        return pm

    def _build_memory_card(self) -> QGroupBox:
        card = self._card("内存")
        inner = QHBoxLayout()
        inner.setContentsMargins(4, 4, 4, 4)
        inner.setSpacing(10)
        self._mem_donut = QLabel()
        self._mem_donut.setPixmap(self._render_donut(0.0, self._ACCENT))
        self._mem_donut.setFixedSize(78, 78)
        inner.addWidget(self._mem_donut)
        col = QVBoxLayout()
        col.setSpacing(2)
        self._mem_gb = QLabel("— / — GB")
        self._mem_gb.setObjectName("InfoLabel")
        self._mem_hint = QLabel("加载中…")
        self._mem_hint.setObjectName("MutedLabel")
        col.addWidget(self._mem_gb)
        col.addWidget(self._mem_hint)
        inner.addLayout(col)
        card.layout().addLayout(inner)
        return card

    def _build_cpu_card(self) -> QGroupBox:
        card = self._card("CPU")
        inner = QVBoxLayout()
        inner.setContentsMargins(4, 4, 4, 4)
        inner.setSpacing(4)
        # 顶行：实时 CPU 占用率 + 核数/线程数
        top = QHBoxLayout()
        top.setSpacing(6)
        self._cpu_pct = QLabel("—%")
        self._cpu_pct.setStyleSheet("font-size: 18px; font-weight: 600;")
        self._cpu_text = QLabel("—核/—线程")
        self._cpu_text.setObjectName("MutedLabel")
        top.addWidget(self._cpu_pct)
        top.addWidget(self._cpu_text)
        top.addStretch(1)
        inner.addLayout(top)
        # 实时面积折线图（任务管理器风格，随采样滚动更新）
        self._cpu_graph = QLabel()
        self._cpu_graph.setPixmap(self._render_cpu_graph([]))
        self._cpu_graph.setFixedSize(130, 46)
        inner.addWidget(self._cpu_graph)
        card.layout().addLayout(inner)
        return card

    def _build_gpu_card(self) -> QGroupBox:
        card = self._card("GPU")
        inner = QVBoxLayout()
        inner.setContentsMargins(4, 4, 4, 4)
        inner.setSpacing(6)
        self._gpu_text = QLabel("加载中…")
        self._gpu_text.setObjectName("InfoLabel")
        self._gpu_hint = QLabel("显存 —")
        self._gpu_hint.setObjectName("MutedLabel")
        inner.addWidget(self._gpu_text)
        inner.addWidget(self._gpu_hint)
        # 无独显时整卡置灰（视觉弱化）
        card.setObjectName("CardDim")
        card.layout().addLayout(inner)
        return card

    def _render_conc_slots(self, n: int) -> QPixmap:
        """渲染并发槽位示意图：n 个高亮方块 = 当前并发上限（简单图示替代长文案）。"""
        total = 16
        n = max(1, min(n, total))
        bw, gap, h = 6, 1, 14
        w = total * bw + (total - 1) * gap
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
               f'viewBox="0 0 {w} {h}">')
        for i in range(total):
            x = i * (bw + gap)
            col = self._ACCENT if i < n else self._TRACK
            svg += (f'<rect x="{x}" y="0" width="{bw}" height="{h}" '
                    f'rx="2.5" fill="{col}"/>')
        svg += '</svg>'
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        renderer.render(p)
        p.end()
        return pm

    def _build_concurrency_card(self) -> QGroupBox:
        card = self._card("模型加载上限")
        inner = QVBoxLayout()
        inner.setContentsMargins(6, 6, 6, 6)
        inner.setSpacing(8)
        # 顶行：当前上限大数字 + 系统建议（红字要求：文字精简，冗长说明移入 tooltip）
        top = QHBoxLayout()
        top.setSpacing(8)
        self._conc_big = QLabel("—")
        self._conc_big.setStyleSheet("font-size: 24px; font-weight: 600;")
        self._conc_sug = QLabel("建议 —")
        self._conc_sug.setObjectName("MutedLabel")
        top.addWidget(self._conc_big)
        top.addWidget(self._conc_sug)
        top.addStretch(1)
        self._conc_apply = QPushButton("应用")
        self._conc_apply.clicked.connect(self._on_set_concurrency)
        top.addWidget(self._conc_apply)
        inner.addLayout(top)

        # 槽位示意图（随滑块实时变化）
        self._conc_slots = QLabel()
        self._conc_slots.setPixmap(self._render_conc_slots(1))
        self._conc_slots.setFixedHeight(14)
        inner.addWidget(self._conc_slots)

        # 滑块 + 数值
        srow = QHBoxLayout()
        srow.setSpacing(8)
        self._conc_slider = QSlider(Qt.Orientation.Horizontal)
        self._conc_slider.setRange(1, 16)
        self._conc_slider.setValue(1)
        self._conc_slider.valueChanged.connect(self._on_slider_moved)
        srow.addWidget(self._conc_slider, 1)
        self._conc_val = QLabel("1")
        self._conc_val.setMinimumWidth(22)
        self._conc_val.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        srow.addWidget(self._conc_val)
        inner.addLayout(srow)
        card.layout().addLayout(inner)
        return card

    def _on_slider_moved(self, v: int) -> None:
        """滑块变化 -> 实时更新槽位图示与数值。"""
        self._conc_slots.setPixmap(self._render_conc_slots(v))
        self._conc_val.setText(str(v))

    def _build_load_card(self) -> QGroupBox:
        card = self._card("模型加载 / 测试")
        # 高度按内容固定，不允许被布局压缩（否则固定高度的测试结果框会溢出卡片边界）
        card.setSizePolicy(QSizePolicy.Policy.Preferred,
                           QSizePolicy.Policy.Fixed)
        # 红字要求：加载/测试按钮放到模型同一行，模型名称框缩小（不占满整行）
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("模型:"))
        self._model_combo = ModelComboBox()
        self._model_combo.setMaximumWidth(340)
        row.addWidget(self._model_combo, 1)
        self._load_btn = QPushButton("加载")
        self._load_btn.setObjectName("PrimaryBtn")
        self._test_btn = QPushButton("测试")
        self._load_btn.clicked.connect(self._on_load)
        self._test_btn.clicked.connect(self._on_test)
        row.addWidget(self._load_btn)
        row.addWidget(self._test_btn)
        row.addStretch(1)
        card.layout().addLayout(row)

        # 测试结果：内容可能很长（延迟 + 样例全文）。红字要求：固定高度，
        # 内容多了出滚动条，绝不撑出卡片区域
        self._test_label = QTextEdit()
        self._test_label.setObjectName("TestResult")
        self._test_label.setReadOnly(True)
        self._test_label.setFixedHeight(84)
        self._test_label.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._test_label.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._test_label.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        self._loaded_scroll.setMinimumHeight(84)
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
        n = self._conc_slider.value()

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
        cur = d.get("max_concurrent", 1)
        sug = d.get("suggested", "—")
        reason = d.get("reason", "")
        self._conc_big.setText(str(cur))
        self._conc_sug.setText(f"系统建议 {sug}")
        # 冗长的估算说明放 tooltip（红字要求：卡片内文字精简）
        self._conc_sug.setToolTip(f"按内存自动估算：{reason}" if reason
                                  else "按内存自动估算")
        try:
            self._conc_slider.setValue(int(cur))
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
    # 实时监控（2s 采样：内存环形图 / CPU 曲线，红字要求「要实时运行」）
    # ------------------------------------------------------------------ #
    def _mon_tick(self) -> None:
        """定时采样系统资源（仅页面可见时请求，避免无谓开销）。"""
        if self._mon_busy or not self.isVisible():
            return
        self._mon_busy = True
        self._bg(lambda: self.api.system_info(), self._on_mon_data,
                 self._on_mon_err)

    def _on_mon_data(self, d) -> None:
        self._mon_busy = False
        if d:
            self._apply_system(d)

    def _on_mon_err(self, e: str) -> None:
        self._mon_busy = False
        # 采样失败不刷日志（掉线时避免每 2s 刷屏），自动拉起逻辑保留
        self._auto.handle(e, lambda _m: None)

    def _apply_system(self, d: dict) -> None:
        """用 M1 数据更新内存环形图 / CPU 实时曲线 / GPU 卡。"""
        if not d:
            return
        total = float(d.get("total_ram_gb") or 0)
        free = float(d.get("free_ram_gb") or 0)
        if d.get("mem_percent") is not None:
            used_pct = float(d.get("mem_percent") or 0)
        else:
            used_pct = (1 - free / total) * 100 if total > 0 else 0
        self._mem_donut.setPixmap(self._render_donut(used_pct, self._ACCENT))
        self._mem_gb.setText(f"{free:.1f} / {total:.1f} GB")
        self._mem_hint.setText(f"空闲 {free:.1f} GB")
        self._cpu_text.setText(
            f"{d.get('cpu_count')}核/{d.get('cpu_threads')}线程")
        cpu = float(d.get("cpu_percent") or 0)
        self._cpu_hist.append(cpu)
        self._cpu_pct.setText(f"{cpu:.0f}%")
        self._cpu_graph.setPixmap(self._render_cpu_graph(self._cpu_hist))

        gpu_ok = bool(d.get("gpu_available"))
        if gpu_ok:
            self._gpu_text.setText(str(d.get("gpu_name") or "可用"))
            self._gpu_hint.setText(
                f"显存 {d.get('free_vram_gb')}/{d.get('total_vram_gb')} GB")
        else:
            self._gpu_text.setText("不可用")
            self._gpu_hint.setText("显存 0.0 / 0.0 GB")

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """刷新本页所有数据（后端就绪后调用）。"""
        self._bg(lambda: self.api.system_info(), self._apply_system)
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
        # pool 请求挪后台线程（与 15 轮对话页/API 页同因修复：主线程 HTTP 卡 UI）
        self._bg(lambda: self.api.pool(), self._apply_pool)

    def _apply_pool(self, pool) -> None:
        loaded = (pool or {}).get("loaded", []) or []
        self._render_loaded(loaded)
        # 通知主窗口刷新状态栏「运行中模型」（所有加载/停止/取消路径都汇聚到 refresh）
        self.pool_changed.emit(loaded)

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
