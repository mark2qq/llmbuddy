# -*- coding: utf-8 -*-
"""
滚动条默认不显示回归测试（2026-10-21 红字：这个滚动条默认不显示，
只有高度有变化了才出现）。

缺陷：控制台右侧竖滚动条常驻可见（宽 10px、深色轨道），看起来像「永久占位」，
即使内容并没有超出视口也有存在感。

修复：
    1. 新增 AutoHideScrollArea：原生纵向条策略 AlwaysOff（不占宽度、不显示，
       但滚轮/代码滚动仍有效）；
    2. 叠一条 6px 半透明浮层细条，只在内容溢出且刚发生尺寸变化/滚动时出现，
       停止交互 IDLE_MS 后自动收起；
    3. 全局 QScrollBar 样式改细 + 轨道透明，内层滚动条也不再抢视觉。
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QVBoxLayout, QWidget)

app = QApplication.instance() or QApplication(sys.argv)

from core.theme import apply_stylesheet  # noqa: E402

apply_stylesheet(app, "")

from ui.widgets.auto_scroll_area import AutoHideScrollArea  # noqa: E402
from ui.pages.dashboard_page import DashboardPage  # noqa: E402


class _FakeApi:
    def system_info(self):
        return {}

    def concurrency(self):
        return {}

    def catalog(self, force=False):
        return {"items": []}

    def pool(self):
        return {"loaded": []}


class _FakeLauncher:
    def start(self):
        pass

    def stop(self):
        pass


class _FakeConfig:
    n_ctx = 4096
    n_threads = 8
    n_gpu_layers = 0


def _fill(area, rows):
    inner = QWidget()
    lay = QVBoxLayout(inner)
    for i in range(rows):
        lay.addWidget(QLabel(f"row {i}"))
    area.setWidget(inner)
    return inner


def _make_area(rows, width=400, height=300):
    area = AutoHideScrollArea()
    _fill(area, rows)
    area.resize(width, height)
    area.show()
    app.processEvents()
    app.processEvents()
    return area


def _make_page(width=1080, height=700):
    page = DashboardPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    page.resize(width, height)
    page.show()
    app.processEvents()
    app.processEvents()
    return page


# --------------------------------------------------------------------------- #
# 控件级
# --------------------------------------------------------------------------- #
def test_native_bar_hidden_but_scroll_still_works():
    """原生条永不显示，但内容溢出时内部滚动范围仍然有效（滚轮可滚）。"""
    area = _make_area(rows=20, width=400, height=200)
    assert area.verticalScrollBarPolicy() == \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    assert area.can_scroll() is True
    assert area.verticalScrollBar().maximum() > 0
    area.verticalScrollBar().setValue(50)
    app.processEvents()
    assert area.verticalScrollBar().value() == 50


def test_no_bar_when_content_fits():
    """内容不溢出 -> 浮层细条不出现（默认不显示）。"""
    area = _make_area(rows=2, width=400, height=600)
    assert area.can_scroll() is False
    assert area.overlay_visible() is False


def test_overlay_appears_on_overflow_and_hides_when_idle():
    """高度变化导致溢出 -> 浮出细条；停止交互后自动收起。"""
    area = _make_area(rows=20, width=400, height=600)
    assert area.can_scroll() is False
    assert area.overlay_visible() is False
    # 窗口变矮 -> 内容溢出 -> 细条出现
    area.resize(400, 220)
    app.processEvents()
    app.processEvents()
    assert area.can_scroll() is True
    assert area.overlay_visible() is True
    # 空闲计时到期 -> 自动隐藏（不需要一直占着视觉）
    area._hide_if_idle()
    assert area.overlay_visible() is False
    # 再滚一下 -> 重新出现
    area.verticalScrollBar().setValue(30)
    app.processEvents()
    assert area.overlay_visible() is True


def test_no_layout_shift_when_overlay_toggles():
    """浮层不占布局宽度：出现/隐藏时视口宽度不变（内容不左右跳）。"""
    area = _make_area(rows=20, width=400, height=220)
    assert area.overlay_visible() is True
    w_shown = area.viewport().width()
    area._hide_if_idle()
    app.processEvents()
    assert area.overlay_visible() is False
    assert area.viewport().width() == w_shown, "浮层显隐不应改变视口宽度"
    assert area.viewport().width() == area.width()


def test_overlay_drag_syncs_inner_scroll():
    """拖动浮层细条 -> 内部滚动位置同步（反之亦然）。"""
    area = _make_area(rows=20, width=400, height=220)
    area._bar.setValue(40)
    assert area.verticalScrollBar().value() == 40
    area.verticalScrollBar().setValue(10)
    assert area._bar.value() == 10


def test_overlay_geometry_pinned_right_edge():
    area = _make_area(rows=20, width=400, height=220)
    g = area._bar.geometry()
    assert g.width() == AutoHideScrollArea.BAR_WIDTH
    assert g.right() <= area.width()
    assert g.x() >= area.width() - AutoHideScrollArea.BAR_WIDTH - 4


# --------------------------------------------------------------------------- #
# 页面级
# --------------------------------------------------------------------------- #
def test_page_default_tall_window_has_no_scrollbar():
    """默认/较大窗口：内容放得下 -> 页面不显示任何滚动条。"""
    page = _make_page(1180, 780)
    assert page._page_scroll.can_scroll() is False
    assert page._page_scroll.overlay_visible() is False
    assert page._page_scroll.verticalScrollBar().isVisible() is False


def test_page_short_window_reveals_then_hides():
    """窗口变矮 -> 浮出细条；空闲后收起；原生条始终不显示。"""
    page = _make_page(1080, 780)
    assert page._page_scroll.overlay_visible() is False
    page.resize(1080, 560)
    app.processEvents()
    app.processEvents()
    assert page._page_scroll.can_scroll() is True
    assert page._page_scroll.overlay_visible() is True
    assert page._page_scroll.verticalScrollBar().isVisible() is False
    page._page_scroll._hide_if_idle()
    assert page._page_scroll.overlay_visible() is False


def test_loaded_models_list_also_autohides():
    """已加载模型列表同样使用自动隐藏滚动条。"""
    page = _make_page(1080, 780)
    assert isinstance(page._loaded_scroll, AutoHideScrollArea)
    assert page._loaded_scroll.overlay_visible() is False
