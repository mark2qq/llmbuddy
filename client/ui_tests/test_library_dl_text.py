# -*- coding: utf-8 -*-
"""
下载进度文案回归（2026-10-20 下载加速配套）。

多线程 Range 分片加速后，模型库页下载弹窗需展示：
已下载/总量、实时速度、并发线程数。
"""
import os
import sys

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


def test_dl_text_shows_bytes_speed_and_threads():
    from ui.pages.library_page import LibraryPage

    txt = LibraryPage._dl_text("qwen3-4b", "downloading", {
        "progress": 0.42,
        "downloaded": 440401920,
        "total_bytes": 1048576000,
        "speed_mbps": 18.5,
        "threads": 8,
    })
    assert "42%" in txt, txt
    assert "420.0/1000 MB" in txt, txt
    assert "18.5 MB/s" in txt, txt
    assert "8 线程并行" in txt, txt


def test_dl_text_minimal_fields():
    """旧后端只返回 status/progress 时不报错，退化为百分比文案。"""
    from ui.pages.library_page import LibraryPage

    txt = LibraryPage._dl_text("m", "downloading", {"progress": 0.1})
    assert "10%" in txt, txt
    assert "MB" not in txt, txt
