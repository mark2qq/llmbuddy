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
from ui.pages.dashboard_page import DashboardPage
from ui.pages.library_page import LibraryPage
from ui.pages.chat_page import ChatPage
from ui.pages.api_page import ApiPage
from ui.pages.settings_page import SettingsPage


class MainWindow(QMainWindow):
    """本地大模型服务主窗口。"""

    # 导航项（名称 -> 页面索引）
    NAV_ITEMS = [("控制台", 0), ("模型库", 1), ("对话", 2), ("API", 3), ("设置", 4)]

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._force_quit = False

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
        for name, idx in self.NAV_ITEMS:
            btn = QPushButton(name)
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda _checked, i=idx: self._switch_page(i))
            self._nav_buttons.append(btn)
            nav_layout.addWidget(btn)
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
        self.dashboard = DashboardPage(self.api, self.launcher, self.config)
        self.library = LibraryPage(self.api, self.launcher, self.config)
        self.chat = ChatPage(self.api, self.launcher, self.config)
        self.api_page = ApiPage(self.api, self.launcher, self.config)
        self.settings = SettingsPage(self.api, self.launcher, self.config)
        for p in (self.dashboard, self.library, self.chat, self.api_page, self.settings):
            self._stack.addWidget(p)

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
        self._status_bar.set_status(
            state, url=self.config.gateway_url() if state == "running" else "",
            loaded=0)
        if state == "running":
            self._refresh_all()

    def _on_ready(self, ok: bool) -> None:
        if ok:
            self._status_bar.set_status("running", url=self.config.gateway_url())
            self._refresh_all()

    def _on_log(self, line: str) -> None:
        self.dashboard.append_log(line)

    def _apply_config(self) -> None:
        # 更新 api 客户端端点（launcher 在下次 start 时读取最新 env）
        self.api.host = self.config.gateway_host
        self.api.port = self.config.gateway_port
        self.api.api_key = self.config.api_key
        self._status_bar.set_status("stopped", url=self.config.gateway_url())

    def _refresh_all(self) -> None:
        for p in (self.dashboard, self.library, self.chat, self.api_page, self.settings):
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
        """后台拉取模型池，回填状态栏「已加载模型」段。"""
        worker = Worker(self.api.pool)
        worker.result.connect(self._apply_status_models)
        worker.error.connect(lambda _msg: None)  # 后端未就绪时静默保持原状
        worker.start()

    def _apply_status_models(self, pool) -> None:
        self._status_bar.set_models(self._pool_names((pool or {}).get("loaded")))

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
