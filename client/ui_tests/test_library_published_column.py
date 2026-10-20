# -*- coding: utf-8 -*-
"""
模型目录「发布时间」列回归测试（2026-10-20 需求）。

模型库 -> 模型目录 表格需新增一列「发布时间」，展示目录中模型的
published 字段（官方发布日 YYYY-MM-DD）；本地导入/缺失该字段时显示「—」。
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _Api:
    def system_info(self, *a, **k):
        return {}

    def catalog(self, *a, **k):
        return {"items": []}


class _Launcher:
    def is_ready(self, *a, **k):
        return False


class _Cfg:
    models_dir = ""


def _page():
    from ui.pages.library_page import LibraryPage
    return LibraryPage(_Api(), _Launcher(), _Cfg())


def test_table_has_published_column():
    """表格从第 7 列扩为 8 列，且表头含「发布时间」。"""
    page = _page()
    assert page._table.columnCount() == 8, page._table.columnCount()
    labels = list(page._table.horizontalHeaderItem(c).text()
                   for c in range(page._table.columnCount()))
    assert "发布时间" in labels, labels
    # 发布时间应在「参数」(index 5) 之后、「本地/操作」(index 7) 之前
    assert labels.index("发布时间") == 6, labels


def test_render_shows_published_and_dash_for_missing():
    """有 published 字段 -> 显示日期；缺失 -> 显示「—」。"""
    page = _page()
    page._items = [
        {"name": "m1", "repo": "R/A", "quant": "Q4_K_M", "size_gb": 1.0,
         "min_ram_gb": 0, "params_b": 1.0, "published": "2024-09-19",
         "local": False},
        {"name": "m2", "repo": "本地导入", "local": True, "published": ""},
    ]
    page._free_ram = 0.0
    page._render()

    assert page._table.rowCount() == 2
    assert page._table.item(0, 6).text() == "2024-09-19", page._table.item(0, 6).text()
    # 本地导入条目无发布时间 -> 占位「—」
    assert page._table.item(1, 6).text() == "—", page._table.item(1, 6).text()
