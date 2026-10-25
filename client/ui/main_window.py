# -*- coding: utf-8 -*-
"""
主窗口（main_window.py）。

布局：
    - 左侧 200px 导航：控制台 / 模型库 / 对话 / API / 设置
    - 右侧 QStackedWidget 切换五大页面
    - 底部状态栏（StatusBar 控件）
    - 系统托盘：关闭窗口=最小化到托盘；仅托盘「退出」才会杀掉后端并退出
"""
import os

from PySide6.QtWidgets import (QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
                               QPushButton, QStackedWidget, QSystemTrayIcon,
                               QMenu, QMessageBox, QApplication, QLabel)
from PySide6.QtCore import Qt, QSize, QTimer
from PySide6.QtGui import QIcon, QPixmap, QColor, QAction

from app_config import APP_NAME, APP_VERSION, APP_TITLE, resource_path
from core.task_runner import Worker

from ui.widgets.status_bar import StatusBar
from ui.pages.assistant_page import AssistantPage
from ui.pages.dashboard_page import DashboardPage
from ui.pages.library_page import LibraryPage
from ui.pages.chat_page import ChatPage
from ui.pages.api_page import ApiPage
from ui.pages.settings_page import SettingsPage


class MainWindow(QMainWindow):
    """本地大模型服务主窗口。"""

    # 导航项（名称 -> 页面索引）
    NAV_ITEMS = [("智能助理", 0), ("控制台", 1), ("模型库", 2),
                 ("对话", 3), ("API", 4), ("设置", 5)]

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._force_quit = False
        # 状态栏当前服务状态（stopped/starting/running），供后台探测结果比对
        self._status_state = "stopped"
        # 探测代际号：每次状态变更/新探测都会 +1，过期探测结果直接丢弃，
        # 避免「停止服务瞬间在途的 health 成功结果晚到」把状态又改回运行中
        self._probe_gen = 0

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1000, 680)
        self._build_icon()
        self._build_ui()
        self._build_tray()
        self._wire_signals()

    # ------------------------------------------------------------------ #
    # 图标（resources/llmbuddy.png；缺失时回退纯色，保证不崩）
    # ------------------------------------------------------------------ #
    def _build_icon(self) -> None:
        path = resource_path(os.path.join("resources", "llmbuddy.png"))
        icon = QIcon(path) if os.path.exists(path) else QIcon()
        if icon.isNull():
            pix = QPixmap(64, 64)
            pix.fill(QColor("#6c8cff"))
            icon = QIcon(pix)
        self._app_icon = icon
        self.setWindowIcon(self._app_icon)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setSpacing(0)
        root.setContentsMargins(0, 0, 0, 0)

        # 左侧导航
        nav = QWidget()
        nav.setObjectName("NavPanel")
        nav.setFixedWidth(200)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(4)
        nav_layout.addWidget(self._nav_title())
        self._nav_buttons = []
        self._assistant_nav_host = None
        for name, idx in self.NAV_ITEMS:
            btn = QPushButton(name)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda _checked, i=idx: self._switch_page(i))
            self._nav_buttons.append(btn)
            nav_layout.addWidget(btn)
            if idx == 0:
                # 「智能助理」下面直接挂二级栏目（任务/空间 + 技能/工具/MCP/审计）
                host = QWidget()
                host.setObjectName("NavSubHost")
                host_lay = QVBoxLayout(host)
                host_lay.setContentsMargins(0, 2, 0, 6)
                host_lay.setSpacing(0)
                # 不给 stretch：让 host 按 sizeHint 拿高度（滚动区已重写 sizeHint），
                # 末尾的 addStretch(1) 吃掉剩余空间。给 stretch 反而会与它按比例
                # 分空间，host 只拿到 2/3，最后一行被挤出视口。
                nav_layout.addWidget(host)
                self._assistant_nav_host = host
                self._assistant_nav_lay = host_lay
        nav_layout.addStretch(1)
        root.addWidget(nav)

        # 右侧：堆叠页 + 底部状态栏
        right = QVBoxLayout()
        right.setSpacing(0)
        right.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget()
        right.addWidget(self._stack, 1)

        self._status_bar = StatusBar()
        right.addWidget(self._status_bar)

        root.addLayout(right, 1)

        # 构建页面
        self.assistant = AssistantPage(self.api, self.launcher, self.config)
        self.dashboard = DashboardPage(self.api, self.launcher, self.config)
        self.library = LibraryPage(self.api, self.launcher, self.config)
        self.chat = ChatPage(self.api, self.launcher, self.config)
        self.api_page = ApiPage(self.api, self.launcher, self.config)
        self.settings = SettingsPage(self.api, self.launcher, self.config)
        for p in (self.assistant, self.dashboard, self.library, self.chat,
                  self.api_page, self.settings):
            self._stack.addWidget(p)

        # 二级栏目要等页面建好才能取控件（任务/空间两块由 AssistantPage 维护）
        if self._assistant_nav_host is not None:
            # stretch=1 同样不能省：否则 panel 只按 sizeHint 排，里面的滚动区
            # 拿不到剩余高度，「空间」那一段就被裁在视口外了。
            self._assistant_nav_lay.addWidget(self.assistant.build_nav_panel(), 1)

        # 注意：构造函数期间不要触发各页 refresh()（其内部会启动 QThread 后台任务）。
        # 在 Qt 事件循环启动前启动 QThread 会导致死锁/卡死（窗口永远不显示）。
        # 真正的首次刷新由 main() 在 app.exec() 之后通过 QTimer.singleShot 触发。
        self._switch_page(0, refresh=False)

    def _nav_title(self) -> QWidget:
        """侧栏标题：产品名（放大）在上，版本号（灰色、小一号）在其下方。"""
        box = QWidget()
        box.setObjectName("NavTitle")
        box.setToolTip(APP_TITLE)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 10, 8, 8)
        lay.setSpacing(1)
        name = QLabel(APP_NAME)
        name.setObjectName("NavTitleLabel")
        ver = QLabel(f"v{APP_VERSION}")
        ver.setObjectName("NavVersionLabel")
        lay.addWidget(name)
        lay.addWidget(ver)
        return box

    def _switch_page(self, idx: int, refresh: bool = True) -> None:
        self._stack.setCurrentIndex(idx)
        for i, btn in enumerate(self._nav_buttons):
            btn.setChecked(i == idx)
        # 二级栏目属于「智能助理」页：切到别的页就收起，不占用导航高度
        if getattr(self, "_assistant_nav_host", None) is not None:
            self._assistant_nav_host.setVisible(idx == 0)
        # 面包屑：切页是排查崩溃最常见的关键动作
        try:
            from core import ui_trace
            btn = self._nav_buttons[idx] if 0 <= idx < len(self._nav_buttons) else None
            ui_trace.trace("[page] %s -> %s" % (
                idx, btn.text() if btn is not None else "?"))
        except Exception:  # noqa: BLE001
            pass
        if refresh:
            w = self._stack.currentWidget()
            if w is not None:
                w.refresh()

    # ------------------------------------------------------------------ #
    # 系统托盘
    # ------------------------------------------------------------------ #
    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self._app_icon)
        self.tray.setToolTip(APP_TITLE)

        menu = QMenu()
        menu.setObjectName("TrayMenu")
        show_action = QAction("显示窗口", self)
        show_action.triggered.connect(self._show_window)
        # 启停互斥（红字要求）：同一时刻只有一个可点，按后端状态实时切换
        self._tray_start_action = QAction("启动服务", self)
        self._tray_start_action.triggered.connect(self._tray_start_service)
        self._tray_stop_action = QAction("停止服务", self)
        self._tray_stop_action.triggered.connect(self._tray_stop_service)
        quit_action = QAction("退出（杀掉后端）", self)
        quit_action.triggered.connect(self._quit_via_tray)
        menu.addAction(show_action)
        menu.addAction(self._tray_start_action)
        menu.addAction(self._tray_stop_action)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.show()
        # 初始状态：按 launcher 实际运行情况点亮（避免启动时两项都可点）
        self._sync_tray_actions(
            "running" if self.launcher.is_running() else "stopped")

    def _sync_tray_actions(self, state: str) -> None:
        """托盘「启动服务 / 停止服务」互斥（红字要求）。

        状态机（与 backend_launcher 的 state_changed 对齐）：
            running / starting -> 启动置灰，停止可点（starting 时可用于中止启动）
            stopped / error    -> 启动可点，停止置灰
        """
        running = state in ("running", "starting")
        start_act = getattr(self, "_tray_start_action", None)
        stop_act = getattr(self, "_tray_stop_action", None)
        if start_act is not None:
            start_act.setEnabled(not running)
        if stop_act is not None:
            stop_act.setEnabled(running)

    def _show_window(self) -> None:
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _tray_start_service(self) -> None:
        """托盘「启动服务」：拉起后端并把状态写入控制台运行日志。"""
        ok = self.launcher.start()
        self.dashboard.append_log("正在启动服务，请稍候…" if ok else "启动失败，请检查日志")

    def _tray_stop_service(self) -> None:
        """托盘「停止服务」：停止后端并把状态写入控制台运行日志。"""
        self.launcher.stop()
        self.dashboard.append_log("服务已停止")

    def _quit_via_tray(self) -> None:
        reply = QMessageBox.question(
            None, "退出确认", "退出将停止本地大模型服务，确定退出？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._force_quit = True
        self.launcher.kill_hard()
        QApplication.quit()

    # ------------------------------------------------------------------ #
    # 信号连接
    # ------------------------------------------------------------------ #
    def _wire_signals(self) -> None:
        # 后端状态
        self.launcher.state_changed.connect(self._on_state)
        self.launcher.ready.connect(self._on_ready)
        self.launcher.log.connect(self._on_log)

        # 设置页保存后同步 api/launcher 端点
        self.settings.config_changed.connect(self._apply_config)

        # 控制台页每次拿到 pool 快照（加载/停止/取消后都会刷新）→ 状态栏即时回填
        self.dashboard.pool_changed.connect(self._on_pool_changed)

        # 兜底轮询：覆盖启动竞态、看门狗自愈、加载进度完成等未经过页面刷新的路径
        self._status_poll = QTimer(self)
        self._status_poll.setInterval(5000)
        self._status_poll.timeout.connect(self._poll_status_models)
        self._status_poll.start()

    def _on_state(self, state: str) -> None:
        # 托盘启停互斥：随后端状态实时切换可用性（红字要求）
        self._sync_tray_actions(state)
        # 作废所有在途探测：停止服务后，停止前发出的 health 成功结果若晚到，
        # 会把状态又覆盖成「运行中」（红字缺陷）
        self._probe_gen += 1
        self._status_state = state
        self._status_bar.set_status(
            state, url=self.config.gateway_url() if state == "running" else "",
            loaded=0)
        if state == "running":
            self._refresh_all()

    def _on_ready(self, ok: bool) -> None:
        if ok:
            self._probe_gen += 1
            self._status_state = "running"
            self._status_bar.set_status("running", url=self.config.gateway_url())
            self._refresh_all()

    def _on_log(self, line: str) -> None:
        self.dashboard.append_log(line)

    def _apply_config(self) -> None:
        # 更新 api 客户端端点（launcher 在下次 start 时读取最新 env）
        self.api.host = self.config.gateway_host
        self.api.port = self.config.gateway_port
        self.api.api_key = self.config.api_key
        # 界面背景色：保存后立即重新应用样式（无需重启客户端）
        try:
            from core.theme import apply_stylesheet
            apply_stylesheet(QApplication.instance(),
                             getattr(self.config, "background_color", ""))
        except Exception:
            pass
        # 保存设置后按实际可达性刷新服务状态。
        # 修复（红字：服务已启动但状态栏显示「服务未启动」）：此前这里硬编码
        # set_status("stopped")，而之后只有 5s 轮询回填模型名、不恢复服务状态，
        # 于是出现「服务未启动 + 已加载模型 1」的矛盾显示。
        self._refresh_status_models()

    def _refresh_all(self) -> None:
        for p in (self.assistant, self.dashboard, self.library, self.chat,
                  self.api_page, self.settings):
            try:
                p.refresh()
            except Exception:
                pass
        # 红字：状态栏显示正在运行的模型（pool 请求在后台线程，不卡 UI）
        self._refresh_status_models()

    @staticmethod
    def _pool_names(loaded) -> list:
        """从 pool.loaded 提取状态栏展示用的模型名列表。

        - ready   : 直接显示模型名（正在运行）
        - loading : 显示「名称(加载中 45%)」，避免加载期间状态栏空着「0（无）」
        - paused / 其它 : 不计入（未在运行）
        """
        out = []
        for e in (loaded or []):
            if not isinstance(e, dict):
                continue
            name = e.get("name")
            if not name:
                continue
            status = e.get("status")
            if status == "ready":
                out.append(name)
            elif status == "loading":
                try:
                    pct = int(float(e.get("progress", 0) or 0) * 100)
                except (TypeError, ValueError):
                    pct = 0
                out.append(f"{name}(加载中 {pct}%)")
        return out

    def _poll_status_models(self) -> None:
        """5s 兜底轮询，保证状态栏模型段最终一致。

        刻意不使用 launcher.is_running() 作门槛：它只在本进程拉起后端
        子进程时为 True（见 backend_launcher.is_running），后端由看门狗
        重启、手动启动、或先启动后端再开客户端时轮询会永不运行，
        状态栏就会一直停在「0（无）」。后端未就绪时请求失败会被静默
        忽略，保留上一次显示即可。
        """
        self._refresh_status_models()

    def _refresh_status_models(self) -> None:
        """后台探测后端（M16 health，含 pool），同步服务状态 + 已加载模型段。

        修复（红字：服务已启动但状态栏显示「服务未启动」）：
        不能只依赖 launcher 的 ready 信号——先手动启动后端再开客户端、看门狗
        重启、保存设置等路径都不会发 ready，状态栏会一直停在「服务未启动」。
        改为以 health 是否可达作为唯一事实来源，5s 兜底轮询 + 保存设置后自愈。
        """
        worker = Worker(self.api.health)
        gen = self._probe_gen
        worker.result.connect(
            lambda data, g=gen: self._apply_status_health(data, g))
        worker.error.connect(
            lambda msg, g=gen: self._on_status_probe_error(msg, g))
        worker.start()

    def _apply_status_health(self, data, gen=None) -> None:
        """health 可达 -> 服务运行中，并回填已加载模型。

        gen 为 None 表示直接调用（测试/内部），不做代际校验；
        否则丢弃过期探测结果（期间状态已变更）。
        """
        if gen is not None and gen != self._probe_gen:
            return
        d = data or {}
        if self._status_state != "running":
            self._status_state = "running"
            self._sync_tray_actions("running")
            self._status_bar.set_status("running",
                                        url=self.config.gateway_url())
        pool = d.get("pool") or {}
        self._status_bar.set_models(self._pool_names(pool.get("loaded")))

    def _on_status_probe_error(self, _msg: str, gen=None) -> None:
        """health 不可达 -> 服务未启动。

        「启动中」不回退：此时 health 失败属正常（服务尚未起来），
        等 launcher 的 ready/state 信号给出最终结果。
        """
        if gen is not None and gen != self._probe_gen:
            return
        if self._status_state == "starting":
            return
        if self._status_state != "stopped":
            self._status_state = "stopped"
            self._sync_tray_actions("stopped")
            self._status_bar.set_status("stopped",
                                        url=self.config.gateway_url())

    def _on_pool_changed(self, loaded) -> None:
        """控制台页 pool 快照变化 → 状态栏即时回填（无需再发请求）。"""
        self._status_bar.set_models(self._pool_names(loaded))

    # ------------------------------------------------------------------ #
    # 关闭事件：最小化到托盘
    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._force_quit:
            event.accept()
            return
        # 关闭窗口 = 最小化到托盘（不退出、不杀后端）
        event.ignore()
        self.hide()
        self.tray.showMessage(
            "本地大模型服务", "已最小化到系统托盘，后端仍在运行。",
            QSystemTrayIcon.MessageIcon.Information, 2000)
