# -*- coding: utf-8 -*-
"""
入口（main.py）。

职责：
    1) 创建 QApplication，加载深色主题 QSS
    2) 读取 config（app_config）
    3) 初始化 ApiClient（对接后端 REST/SSE）与 BackendLauncher（看护 serve.py）
    4) 显示 MainWindow（含系统托盘）
    5) 首次进入若后端未就绪，提示「一键启动」

需求 9：无登录、打开即运行——启动即尝试拉起服务，或用户手动在控制台/托盘启动。
"""
import os
import sys
import time

from app_config import DATA_DIRNAME  # 用户数据目录名（llmBuddy；旧名 LLMDesktop 已迁移）


# ---------------------------------------------------------------------------
# 启动期崩溃兜底：窗口程序（console=False）一旦抛出未捕获异常会“静默退出”，
# 用户看到的就是“双击没反应”。这里把所有未捕获异常写到
# %LOCALAPPDATA%/llmBuddy/crash.log，并弹一个系统级消息框，便于排查。
# ---------------------------------------------------------------------------
def _install_crash_handler() -> None:
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    log_dir = os.path.join(local, DATA_DIRNAME)
    _orig = sys.excepthook

    def _show(msg: str) -> None:
        # 优先用 Win32 MessageBox（不依赖 Qt 是否已初始化）
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "本地大模型服务 - 启动错误", 0x10)
        except Exception:
            pass

    def handler(etype, exc, tb) -> None:
        try:
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "crash.log")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n=== crash %s ===\n"
                        % time.strftime("%Y-%m-%d %H:%M:%S"))
                traceback.print_exception(etype, exc, tb, file=f)
            detail = "程序启动失败，错误已记录到：\n%s\n\n%s: %s" % (
                log_path, getattr(etype, "__name__", "Error"), exc)
        except Exception as e:  # noqa: BLE001
            detail = "启动失败：%s: %s（写日志也失败: %s）" % (
                getattr(etype, "__name__", "Error"), exc, e)
        try:
            _show(detail)
        except Exception:
            pass
        if callable(_orig):
            try:
                _orig(etype, exc, tb)
            except Exception:
                pass

    sys.excepthook = handler


_install_crash_handler()

import traceback  # noqa: E402  (置于崩溃兜底之后)


# ---------------------------------------------------------------------------
# 启动里程碑日志：写 %LOCALAPPDATA%/llmBuddy/startup.log。
# 即便窗口程序“静默退出”，也能据此判断卡在哪一步（便于用户/我们排查）。
# ---------------------------------------------------------------------------
def _startup_log(msg: str) -> None:
    try:
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(local, DATA_DIRNAME)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "startup.log"), "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 打包态后端模式：llmBuddy.exe --backend
# 由 GUI 进程（BackendLauncher）以子方式拉起，只跑 FastAPI 服务，不加载 Qt。
# 后端源码（serve.py + core/ + routers/）作为 data 文件随包分发到
# _internal/backend/，与客户端 client/core 隔离；此处把该目录插到 sys.path 最前，
# 再 import serve，保证后端自己的 core/routers 优先被解析（避免同名冲突）。
# ---------------------------------------------------------------------------
if "--backend" in sys.argv[1:]:
    def _backend_dir():
        meipass = getattr(sys, "_MEIPASS", None)
        exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        candidates = []
        if meipass:
            candidates.append(os.path.join(meipass, "backend"))
        candidates.append(os.path.join(exe_dir, "backend"))
        candidates.append(os.path.join(exe_dir, "_internal", "backend"))
        candidates.append(os.path.join(exe_dir, "..", "backend"))
        for c in candidates:
            if os.path.isdir(c):
                return os.path.abspath(c)
        return None

    _bd = _backend_dir()
    if _bd:
        # 仅把 backend/ 插到 sys.path 最前：import serve -> backend/serve.py，
        # import core/routers -> backend/core、backend/routers（避免与客户端 client/core 冲突）。
        sys.path.insert(0, _bd)
    import serve
    serve.run()
    sys.exit(0)

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import QTimer

from app_config import Config, CLIENT_DIR
from core.api_client import ApiClient
from core.backend_launcher import BackendLauncher
from core.single_instance import SingleInstanceGuard
from ui.main_window import MainWindow


def resource_path(rel: str) -> str:
    """解析资源路径。

    开发态：client 目录。
    打包态 onefile：sys._MEIPASS（临时解压目录）。
    打包态 onedir：数据文件在 exe 同级的 _internal/ 下（sys._MEIPASS 不设置），
    因此优先在「安装目录/_internal」中查找，找不到再回退安装目录。
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(meipass)
    install_dir = getattr(sys, "argv", [""])[0]
    install_dir = os.path.dirname(os.path.abspath(install_dir)) if install_dir else ""
    if install_dir:
        candidates.append(os.path.join(install_dir, "_internal"))
        candidates.append(install_dir)
    candidates.append(CLIENT_DIR)
    for base in candidates:
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    # 都没有则返回默认（调用方自行判空）
    return os.path.join(candidates[-1], rel)


def load_stylesheet(background_color: str = "") -> str:
    """加载主题样式：基线 styles.qss + 按背景色的覆盖（见 core.theme）。"""
    try:
        from core.theme import build_stylesheet
        return build_stylesheet(background_color)
    except Exception:
        # 兜底：直接读基线样式，保证界面不裸奔
        path = resource_path(os.path.join("resources", "styles.qss"))
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                return ""
        return ""


def _count_local_models(cfg) -> int:
    """统计本地模型目录下的 .gguf 文件数量（即已下载模型数）。

    不依赖后端是否就绪：直接扫描 Config.models_dir，用于启动弹窗的智能提示。
    """
    try:
        models_dir = getattr(cfg, "models_dir", "") or ""
        if not models_dir or not os.path.isdir(models_dir):
            return 0
        return sum(1 for f in os.listdir(models_dir)
                   if f.lower().endswith(".gguf"))
    except Exception:
        return 0


def main() -> None:
    """程序入口。"""
    _startup_log("boot: frozen=%s meipass=%s exe=%s"
                 % (getattr(sys, "frozen", False),
                    getattr(sys, "_MEIPASS", None), sys.executable))
    app = QApplication(sys.argv)
    app.setApplicationName("本地大模型服务")
    app.setStyle("Fusion")
    # 托盘应用：关闭/隐藏窗口不退出进程（仅托盘“退出”才真正退出）
    app.setQuitOnLastWindowClosed(False)
    _startup_log("QApplication created")

    # 先载入配置：主题背景色需要它（Config.load 无副作用，可安全提前）
    config = Config.load()
    _startup_log("config loaded")
    style = load_stylesheet(config.background_color)
    if style:
        app.setStyleSheet(style)
    _startup_log("stylesheet loaded=%s bg=%s"
                 % (bool(style), config.background_color or "default"))

    # ------------------------------------------------------------------
    # 单实例锁（2026-10-20 需求）：已有 llmBuddy 在运行时，再次打开不启动
    # 新窗口/新进程——第二个实例向已运行实例发送「激活」请求后立即退出，
    # 已运行实例把主窗口置前。必须在创建 BackendLauncher 之前判定，
    # 避免第二个实例重复拉起后端子进程。
    # ------------------------------------------------------------------
    guard = SingleInstanceGuard()
    if not guard.try_lock():
        _startup_log("single-instance: already running -> activate & exit")
        app.processEvents()
        sys.exit(0)
    _startup_log("single-instance: primary acquired")

    api = ApiClient(config.gateway_host, config.gateway_port, config.api_key)
    launcher = BackendLauncher(config)
    _startup_log("config/api/launcher ready")

    win = MainWindow(api, launcher, config)
    guard.activate_requested.connect(win._show_window)
    win.show()
    _startup_log("MainWindow shown")
    # 首次刷新与「后端未就绪」提示统一推迟到事件循环内执行。
    # 关键：绝不能在 app.exec() 之前用 QMessageBox 模态框阻塞——模态框自带的嵌套
    # 事件循环会提前触发这里的刷新，进而在主事件循环尚未启动时启动各页 QThread
    # （Worker），这正是 MainWindow._build_ui 注释所警告的“事件循环前启动 QThread
    # 会导致原生崩溃/死锁”场景（表现为点击弹窗后程序直接退出、且无 crash.log，
    # 启动日志停在 "backend not ready -> ask user" 再无后续）。推迟到事件循环内即可规避。
    def _post_startup() -> None:
        win._switch_page(0)
        if launcher.is_ready():
            return
        _startup_log("backend not ready -> ask user")
        # 智能判断：依据本机已下载模型数量给出不同引导文案
        n = _count_local_models(config)
        if n > 0:
            detail = (f"检测到本机已下载 {n} 个模型，启动后即可在控制台加载并使用。")
        else:
            detail = ("当前尚未下载任何本地模型。可先到「模型库」页面下载模型，"
                      "或启动服务后在控制台加载示例模型。")
        reply = QMessageBox.question(
            win, "启动本地服务",
            f"本地大模型服务尚未运行，是否现在启动？\n\n{detail}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        _startup_log("user reply=%s" % reply)
        if reply == QMessageBox.StandardButton.Yes:
            launcher.start()
            _startup_log("backend start requested")

    QTimer.singleShot(300, _post_startup)

    _startup_log("entering app.exec()")
    rc = app.exec()
    _startup_log("app.exec() returned rc=%s" % rc)
    sys.exit(rc)


if __name__ == "__main__":
    main()
