# -*- coding: utf-8 -*-
"""
记忆管理对话框（1.0.12）测试。

覆盖：
    · 条目结构：全局记忆 / 空间记忆 / 今天日志（已有日志按天列出）
    · 切换条目时编辑内容不丢，dirty_keys / dirty_payloads 只报改动项
    · 保存走 on_save 回调（后台线程），成功后回写 path 与状态行
    · 保存失败显示错误，不静默
    · 无改动时点保存不发请求

offscreen 跑（不需要真实窗口）；每个用例一个 dialog，用完 close。
"""
import os
import sys
import time

import pytest

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from ui.widgets.memory_dialog import MemoryDialog  # noqa: E402


def _data(**kw):
    today = time.strftime("%Y-%m-%d")
    base = {
        "enabled": True,
        "workspace": r"E:\ws\demo",
        "global": {"path": r"C:\mem\MEMORY.md", "text": "# 全局记忆\n- 偏好：中文\n"},
        "space": {"path": r"C:\mem\spaces\demo-abc123\MEMORY.md",
                  "text": "# 空间记忆：demo\n- 约定：提交用中文\n"},
        "logs": [
            {"date": today, "path": r"C:\mem\spaces\demo-abc123\%s.md" % today,
             "text": "# 工作日志\n- 09:00 今天的事\n", "chars": 20,
             "preview": "今天的事"},
            {"date": "2026-10-27", "path": r"C:\mem\spaces\demo-abc123\2026-10-27.md",
             "text": "- 昨天的事\n", "chars": 8, "preview": "昨天的事"},
        ],
        "stats": {"global_chars": 20, "space_chars": 24, "logs": 2, "log_chars": 28},
    }
    base.update(kw)
    return base


def _pump(pred, timeout=5.0):
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        app.processEvents()
        if pred():
            return True
        _t.sleep(0.02)
    app.processEvents()
    return pred()


def test_items_cover_global_space_and_logs():
    dlg = MemoryDialog(None, _data())
    try:
        keys = dlg.keys()
        assert keys[0] == "global" and keys[1] == "space"
        assert f"log:{time.strftime('%Y-%m-%d')}" in keys
        assert "log:2026-10-27" in keys
        # 默认停在第一条（全局记忆）
        assert dlg.editor().toPlainText().startswith("# 全局记忆")
    finally:
        dlg.close()


def test_switch_keeps_edits_and_reports_dirty():
    dlg = MemoryDialog(None, _data())
    try:
        dlg.editor().setPlainText("# 全局记忆\n- 偏好：英文\n")
        dlg.select_key("space")
        assert "约定：提交用中文" in dlg.editor().toPlainText()
        assert dlg.dirty_keys() == ["global"]
        payload = dlg.dirty_payloads()
        assert payload == [{"scope": "global", "date": "",
                            "text": "# 全局记忆\n- 偏好：英文\n"}]
    finally:
        dlg.close()


def test_save_calls_backend_and_reports():

    calls = []

    def on_save(scope, date, text):
        calls.append((scope, date, text))
        return {"path": r"C:\mem\new.md", "chars": len(text), "scope": scope}

    dlg = MemoryDialog(None, _data(), on_save=on_save)
    try:
        dlg.select_key(f"log:{time.strftime('%Y-%m-%d')}")
        dlg.editor().setPlainText("- 10:00 补一条\n")
        dlg._on_save_clicked()
        assert _pump(lambda: bool(calls))
        assert calls[0][0] == "log"
        assert calls[0][1] == time.strftime("%Y-%m-%d")
        assert _pump(lambda: "已保存" in dlg.status_text())
        # 保存后不再算「未保存」
        assert dlg.dirty_keys() == []
    finally:
        dlg.close()


def test_save_without_change_sends_nothing():

    calls = []
    dlg = MemoryDialog(None, _data(),
                       on_save=lambda s, d, t: calls.append((s, d, t)) or {})
    try:
        dlg._on_save_clicked()
        app.processEvents()
        assert calls == []
        assert "没有改动" in dlg.status_text()
    finally:
        dlg.close()


def test_save_error_is_visible():

    def boom(scope, date, text):
        raise RuntimeError("后端未就绪")

    dlg = MemoryDialog(None, _data(), on_save=boom)
    try:
        dlg.editor().setPlainText("改一下\n")
        dlg._on_save_clicked()
        assert _pump(lambda: "保存失败" in dlg.status_text())
        assert "后端未就绪" in dlg.status_text()
    finally:
        dlg.close()


def test_empty_memory_still_offers_entries():
    """一条记忆都没有时也要能写：三条条目 + 空编辑器，不是一片空白。"""
    dlg = MemoryDialog(None, {"enabled": False, "workspace": r"E:\ws\demo",
                              "global": {"path": "", "text": ""},
                              "space": {"path": "", "text": ""}, "logs": []})
    try:
        assert dlg.keys() == ["global", "space", f"log:{time.strftime('%Y-%m-%d')}"]
        assert dlg.editor().toPlainText() == ""
        assert "自动注入：关" in dlg._hint.text()
    finally:
        dlg.close()


def test_composer_context_menu_has_memory_entry(monkeypatch):
    """输入框右键要有「记忆…」入口（底栏不放按钮，入口不能失联）。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QContextMenuEvent
    from ui.widgets import image_composer as ic

    class FakeSignal:
        def __init__(self):
            self.handlers = []

        def connect(self, fn):
            self.handlers.append(fn)

    class FakeAct:
        def __init__(self, text):
            self._text = text
            self.triggered = FakeSignal()

        def text(self):
            return self._text

        def setToolTip(self, _t):
            pass

        def trigger(self):
            for fn in self.triggered.handlers:
                fn()

    class FakeMenu:
        def __init__(self):
            self.acts = []

        def addSeparator(self):
            pass

        def addAction(self, text, *a, **kw):
            act = FakeAct(text)
            self.acts.append(act)
            return act

        def exec(self, *a, **kw):
            labels.extend(x.text() for x in self.acts)
            self.labels_ready = True

        def deleteLater(self):
            pass

    labels = []
    # 不碰真 QMenu：替换构造来源即可（`createStandardContextMenu` 是 Qt 方法，
    # 在 offscreen 下 exec 会真的进入事件循环，测试会挂住）
    monkeypatch.setattr(ic.PasteImageEdit, "createStandardContextMenu",
                        lambda self: FakeMenu())
    ed = ic.PasteImageEdit()
    fired = []
    ed.memory_requested.connect(lambda: fired.append(True))
    ed.contextMenuEvent(QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(1, 1), QPoint(1, 1)))
    assert "记忆…" in labels
    assert "插入提示词…" in labels            # 旧入口不能被挤掉
    ed.deleteLater()
