# -*- coding: utf-8 -*-
"""
轻量 UI 动作面包屑（ui_trace.py）。

为什么需要：冻结包里的原生崩溃（`0xC0000409` fail-fast、`0x8001010E`
COM 套间错误）走不到 sys.excepthook，faulthandler 也常常只剩一行
「Windows fatal exception」而拿不到业务栈。有了面包屑，下次崩溃时至少能从
`%LOCALAPPDATA%\\llmBuddy\\logs\\actions.log` 的最后几行看出：
用户当时在哪个页面、点了哪个按钮、弹了哪个对话框。

设计取舍：
    - 只在「鼠标按下」和「页面切换」时写一行，不做全事件记录（怕拖慢界面）；
    - 文件超过 64KB 自动砍掉前半，只留最近约 300 行；
    - 任何异常都吞掉——诊断设施绝不能反过来把程序搞崩；
    - 设 `LLM_NO_TRACE=1` 可整体关掉。
"""
import os
import time

from PySide6.QtCore import QEvent, QObject

_MAX_BYTES = 64 * 1024
_KEEP_LINES = 300
_IGNORED_CLASSES = {"QScrollBar", "QSlider", "QLineEdit", "QSpinBox",
                    "QPlainTextEdit", "QTextEdit"}


def _log_path() -> str:
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "llmBuddy", "logs", "actions.log")


def trace(action: str) -> None:
    """追加一条面包屑（失败静默）。"""
    if os.environ.get("LLM_NO_TRACE"):
        return
    try:
        path = _log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > _MAX_BYTES:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-_KEEP_LINES:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%H:%M:%S"), action))
    except Exception:  # noqa: BLE001
        pass


def mark_session_start() -> None:
    trace("===== session start pid=%d =====" % os.getpid())


def mark_normal_exit() -> None:
    """正常退出标记：没有这一行就说明上次是崩溃/被强杀。"""
    trace("===== normal exit pid=%d =====" % os.getpid())


class _ClickTracer(QObject):
    """全局事件过滤器：记录用户按下了哪个控件。"""

    def eventFilter(self, obj, event):  # noqa: N802 (Qt 命名)
        try:
            if event.type() != QEvent.Type.MouseButtonPress:
                return False
            cls = obj.metaObject().className()
            if cls in _IGNORED_CLASSES:
                return False
            text = ""
            try:
                text = obj.text()
            except Exception:  # noqa: BLE001
                pass
            if not text:
                try:
                    text = obj.windowTitle()
                except Exception:  # noqa: BLE001
                    pass
            name = obj.objectName()
            # 没名字也没文字的控件（容器、布局壳）不记，避免噪音
            if not name and not text:
                return False
            trace("[click] %s#%s %r" % (cls, name or "-", str(text)[:48]))
        except Exception:  # noqa: BLE001
            pass
        return False


_tracer = None


def install(app) -> None:
    """在 QApplication 上装全局点击面包屑（重复调用无副作用）。"""
    global _tracer
    if _tracer is not None or app is None:
        return
    if os.environ.get("LLM_NO_TRACE"):
        return
    try:
        _tracer = _ClickTracer(app)
        app.installEventFilter(_tracer)
    except Exception:  # noqa: BLE001
        _tracer = None
