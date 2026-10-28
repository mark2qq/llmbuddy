# -*- coding: utf-8 -*-
"""记忆管理对话框（memory_dialog.py）。

对齐 WorkBuddy 的「记忆」：本机 Markdown 文件，用户可以直接看、直接改。
三层结构（见 `core/assistant/memory.py`）：

    全局记忆          跨空间生效的偏好 / 长期事实
    空间记忆          只在本空间生效的项目约定 / 已确认结论
    工作日志（按天）   助理每天追加的流水，按日期查看 / 编辑

布局：左侧条目列表 + 右侧编辑器（路径、字符数、保存 / 清空）。
保存走后台线程（`core.task_runner.Worker`），不冻结界面；写完回一行状态。

QSS 口径：标题用 `#MemTitle`，路径提示用 `#MemHint`，字数用 `#MemMeta`，
都在 styles.qss 里有对应规则（主题切换时 theme.py 会一起覆盖）。
"""
import time
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QVBoxLayout, QWidget)

from core.task_runner import Worker


def _fmt_chars(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f} 万字"
    return f"{n} 字"


class MemoryDialog(QDialog):
    """记忆浏览 / 编辑。

    参数：
        data      `GET /memory` 的快照（含 global / space / logs 三部分正文）
        on_save   `(scope, date, text) -> dict`，返回后端结果（失败抛异常）
        on_toggle `(enabled) -> None`，切换「自动注入」开关（可选）
    """

    saved = Signal(dict)        # 保存成功：{scope, date, chars}

    def __init__(self, parent: Optional[QWidget] = None,
                 data: Optional[Dict[str, Any]] = None,
                 on_save: Optional[Callable[[str, str, str], Dict]] = None,
                 on_toggle: Optional[Callable[[bool], None]] = None):
        super().__init__(parent)
        self._data = data if isinstance(data, dict) else {}
        self._on_save = on_save
        self._on_toggle = on_toggle
        self._items: List[Dict[str, Any]] = []
        self._orig: Dict[str, str] = {}
        self._cur_key = ""
        self._saving = False

        self.setWindowTitle("记忆")
        self.setMinimumSize(720, 500)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        title = QLabel("记忆")
        title.setObjectName("MemTitle")
        lay.addWidget(title)

        self._hint = QLabel(self._hint_text())
        self._hint.setObjectName("MemHint")
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        body = QHBoxLayout()
        body.setSpacing(10)
        self._list = QListWidget()
        self._list.setObjectName("MemList")
        self._list.setFixedWidth(190)
        self._list.currentRowChanged.connect(self._on_row_changed)
        body.addWidget(self._list)

        right = QVBoxLayout()
        right.setSpacing(6)
        self._path = QLabel("")
        self._path.setObjectName("MemHint")
        self._path.setWordWrap(True)
        self._path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right.addWidget(self._path)

        self._edit = QPlainTextEdit()
        self._edit.setObjectName("MemEdit")
        self._edit.setPlaceholderText(
            "这里的内容会在每一轮对话开始时注入模型上下文。\n"
            "建议写成简短的事实 / 约定，一条一行；不要写密码、密钥等敏感信息。")
        self._edit.textChanged.connect(self._refresh_meta)
        right.addWidget(self._edit, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._meta = QLabel("")
        self._meta.setObjectName("MemMeta")
        row.addWidget(self._meta)
        row.addStretch(1)
        self._clear_btn = QPushButton("清空")
        self._clear_btn.setObjectName("MiniBtn")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.setToolTip("清空当前条目（点「保存」后才真正写入）")
        self._clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self._clear_btn)
        self._save_btn = QPushButton("保存")
        self._save_btn.setObjectName("PrimaryBtn")
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.clicked.connect(self._on_save_clicked)
        row.addWidget(self._save_btn)
        self._close_btn = QPushButton("关闭")
        self._close_btn.setObjectName("MiniBtn")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.clicked.connect(self.accept)
        row.addWidget(self._close_btn)
        right.addLayout(row)
        body.addLayout(right, 1)
        lay.addLayout(body, 1)

        self._status = QLabel("")
        self._status.setObjectName("MemMeta")
        lay.addWidget(self._status)

        self._build_items()
        if self._list.count():
            self._list.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    def _hint_text(self) -> str:
        enabled = self._data.get("enabled", True)
        ws = str(self._data.get("workspace") or "")
        state = "自动注入：开（每轮进 system）" if enabled else "自动注入：关（只存不注入）"
        return (f"本机 Markdown 记忆 · {state}"
                + (f" · 当前空间 {ws.rsplit('/', 1)[-1]}" if ws else ""))

    def _build_items(self) -> None:
        g = self._data.get("global") or {}
        s = self._data.get("space") or {}
        self._items = [
            {"key": "global", "label": "全局记忆", "scope": "global", "date": "",
             "text": str(g.get("text") or ""), "path": str(g.get("path") or "")},
            {"key": "space", "label": "空间记忆", "scope": "space", "date": "",
             "text": str(s.get("text") or ""), "path": str(s.get("path") or "")},
        ]
        logs = [x for x in (self._data.get("logs") or []) if isinstance(x, dict)]
        today = time.strftime("%Y-%m-%d")
        if not any(str(x.get("date")) == today for x in logs):
            logs.insert(0, {"date": today, "text": "", "path": "",
                            "preview": "今天还没有记录"})
        for x in logs[:14]:
            day = str(x.get("date") or "")
            if not day:
                continue
            self._items.append({
                "key": f"log:{day}", "label": f"{day} 日志",
                "scope": "log", "date": day,
                "text": str(x.get("text") or ""),
                "path": str(x.get("path") or "")})
        self._list.clear()
        for it in self._items:
            row = QListWidgetItem(it["label"])
            row.setToolTip(it.get("path") or "")
            self._list.addItem(row)
            self._orig[it["key"]] = it["text"]

    def _item_by_key(self, key: str) -> Optional[Dict[str, Any]]:
        return next((x for x in self._items if x["key"] == key), None)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _stash_current(self) -> None:
        """把编辑器里的内容记回当前条目（切条目 / 关闭时不能丢）。"""
        it = self._item_by_key(self._cur_key)
        if it is not None:
            it["text"] = self._edit.toPlainText()

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._items):
            return
        self._stash_current()
        it = self._items[row]
        self._cur_key = it["key"]
        self._edit.blockSignals(True)
        self._edit.setPlainText(it["text"])
        self._edit.blockSignals(False)
        self._path.setText(it.get("path") or "（保存后生成文件）")
        self._status.setText("")
        self._refresh_meta()

    def _refresh_meta(self) -> None:
        text = self._edit.toPlainText()
        it = self._item_by_key(self._cur_key)
        dirty = it is not None and text != self._orig.get(self._cur_key, "")
        self._meta.setText(("● 未保存 · " if dirty else "") + _fmt_chars(len(text)))
        self._save_btn.setEnabled(not self._saving)

    def _on_clear(self) -> None:
        if self._edit.toPlainText().strip():
            ans = QMessageBox.question(
                self, "清空记忆", "确定清空当前条目的内容？点「保存」后才真正写入。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        self._edit.setPlainText("")

    def _on_save_clicked(self) -> None:
        it = self._item_by_key(self._cur_key)
        if it is None or self._on_save is None or self._saving:
            return
        text = self._edit.toPlainText()
        if text == self._orig.get(self._cur_key, ""):
            self._status.setText("没有改动，无需保存")
            return
        self._saving = True
        self._save_btn.setEnabled(False)
        self._status.setText("正在保存…")
        w = Worker(self._on_save, it["scope"], it["date"], text)
        w.result.connect(lambda res, i=dict(it): self._on_saved(res, i))
        w.error.connect(self._on_save_error)
        w.start()

    def _on_saved(self, res: Any, it: Dict[str, Any]) -> None:
        self._saving = False
        self._save_btn.setEnabled(True)
        text = self._edit.toPlainText()
        self._orig[it["key"]] = text
        if isinstance(res, dict) and res.get("path"):
            it["path"] = str(res["path"])
            if it["key"] == self._cur_key:
                self._path.setText(it["path"])
        chars = len(text)
        self._status.setText(f"已保存（{_fmt_chars(chars)}）")
        self._refresh_meta()
        self.saved.emit({"scope": it["scope"], "date": it.get("date") or "",
                         "chars": chars})

    def _on_save_error(self, msg: str) -> None:
        self._saving = False
        self._save_btn.setEnabled(True)
        self._status.setText(f"保存失败：{msg}")

    def closeEvent(self, ev) -> None:      # noqa: N802 (Qt 命名)
        self._stash_current()
        super().closeEvent(ev)

    # ------------------------------------------------------------------ #
    # 给页面 / 测试用
    # ------------------------------------------------------------------ #
    def keys(self) -> List[str]:
        return [x["key"] for x in self._items]

    def dirty_keys(self) -> List[str]:
        self._stash_current()
        return [x["key"] for x in self._items
                if x["text"] != self._orig.get(x["key"], "")]

    def dirty_payloads(self) -> List[Dict[str, Any]]:
        self._stash_current()
        out: List[Dict[str, Any]] = []
        for x in self._items:
            if x["text"] != self._orig.get(x["key"], ""):
                out.append({"scope": x["scope"], "date": x.get("date") or "",
                            "text": x["text"]})
        return out

    def select_key(self, key: str) -> None:
        for i, x in enumerate(self._items):
            if x["key"] == key:
                self._list.setCurrentRow(i)
                return

    def editor(self) -> QPlainTextEdit:
        return self._edit

    def status_text(self) -> str:
        return self._status.text()
