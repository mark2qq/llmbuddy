# -*- coding: utf-8 -*-
"""
客户端崩溃诊断补强（crash_diag.py）。

为什么还要再加一层（2026-10-26 用户反馈「执行任务时闪退」）：
    现有设施只覆盖了一半路径——
      - `sys.excepthook`：主线程**未捕获**异常 ✔
      - `faulthandler`：原生异常（0xC0000409 等）有栈 ✔
    但下面三类完全没有痕迹，出问题时只能靠猜：
      1. **后台线程**（Worker / AssistantStream）里抛的异常：Python 只在 stderr
         打印，冻结包没有控制台 → 直接消失；
      2. **Qt 自己**的 fatal（`qFatal` / `QWidget: ...` 会 `abort()` 掉进程）：
         走 Qt 消息处理器，不经过 Python；
      3. **进程被强杀**（taskkill / 安装器 / OOM）：不会跑 atexit，日志停在
         上一行，分不清「用户退出」还是「被带走」。

本模块把这三类都落到 `%LOCALAPPDATA%/llmBuddy/crash.log`，并在正常退出时写
`session end` 标记 —— 之后只要看日志最后几行，就能判断是不是真的崩了。
所有函数都对异常免疫（诊断设施不允许反过来搞崩程序）。
"""
import os
import sys
import threading
import time
import traceback

_LOG_NAME = "crash.log"


def _log_path() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(local, "llmBuddy")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return os.path.join(d, _LOG_NAME)


def log_line(msg: str) -> None:
    """往 crash.log 追加一行（失败静默）。"""
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(msg.rstrip("\n") + "\n")
    except Exception:  # noqa: BLE001
        pass


def log_block(title: str, text: str) -> None:
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write("\n=== %s %s ===\n" % (title, time.strftime("%Y-%m-%d %H:%M:%S")))
            f.write(text.rstrip("\n") + "\n")
    except Exception:  # noqa: BLE001
        pass


def install_thread_excepthook() -> None:
    """后台线程未捕获异常 → crash.log（含线程名）。

    冻结包没有控制台，Worker/SSE 线程里的异常原先完全不可见：用户只看到
    「任务没反应/界面卡住」，我们拿到日志也什么都没有。
    """
    def _hook(args) -> None:
        try:
            name = getattr(args.thread, "name", "?")
            log_block("thread crash",
                      "thread=%s\n%s" % (name, "".join(
                          traceback.format_exception(args.exc_type, args.exc_value,
                                                     args.exc_traceback))))
        except Exception:  # noqa: BLE001
            pass

    try:
        threading.excepthook = _hook
    except Exception:  # noqa: BLE001
        pass


def install_qt_message_handler() -> None:
    """Qt 消息 → crash.log。qFatal 会 abort 进程，必须留下现场。"""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:  # noqa: BLE001
        return

    level_name = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "critical",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def _handler(mode, context, message) -> None:
        try:
            name = level_name.get(mode, str(mode))
            if name == "debug":
                return                      # debug 太吵，只留 warning 以上
            where = ""
            if context is not None and context.file:
                where = f" ({context.file}:{context.line})"
            log_line("%s [qt:%s]%s %s"
                     % (time.strftime("%H:%M:%S"), name, where, message))
        except Exception:  # noqa: BLE001
            pass

    try:
        qInstallMessageHandler(_handler)
    except Exception:  # noqa: BLE001
        pass


def install_quit_marker(app) -> None:
    """正常退出写 `session end`：没有这一行就说明上次是被强杀/崩溃。"""
    try:
        from PySide6.QtCore import QCoreApplication

        def _on_quit() -> None:
            log_line("\n=== session end %s pid=%d ==="
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
        QCoreApplication.instance().aboutToQuit.connect(_on_quit)
    except Exception:  # noqa: BLE001
        pass


def install_all(app=None) -> None:
    """一次性装好全部诊断（main.py 在 GUI 分支调用）。"""
    install_thread_excepthook()
    install_qt_message_handler()
    if app is not None:
        install_quit_marker(app)
    log_line("\n--- diag installed %s pid=%d argv=%s"
             % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                " ".join(sys.argv[1:])[:120]))
