# -*- coding: utf-8 -*-
"""
控制台布局回归测试（2026-10-20 红字：内容框下移了，请固定）。

缺陷：窗口高度不足时 QVBoxLayout 压缩「模型加载/测试」卡片，固定高度的
测试结果框无法压缩，溢出卡片底部、盖住下一张卡片的标题。

修复：
    1. 页面外层包 QScrollArea（空间不足时整页纵向滚动，而不是压缩卡片）；
    2. 加载卡垂直方向 Fixed（按内容高度固定，不被压缩）；
    3. 测试结果框固定 56px + 纵向滚动条；
    4. 监控卡尺寸收紧，内容最小宽度适配 800px 内容区（无横向滚动）。
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
    QApplication, QScrollArea, QSizePolicy, QTextEdit)

app = QApplication.instance() or QApplication(sys.argv)

# 应用真实基线样式（含卡片 padding / 边框），否则测不出真实的最小宽度
from core.theme import apply_stylesheet  # noqa: E402

apply_stylesheet(app, "")

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


def _make_page(width=800, height=640):
    page = DashboardPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    page.resize(width, height)
    page.show()
    app.processEvents()
    app.processEvents()
    return page


def test_page_is_scrollable_container():
    page = _make_page()
    assert isinstance(page._page_scroll, QScrollArea)
    assert page._page_scroll.widgetResizable() is True
    assert page._page_scroll.widget() is not None


def test_test_result_box_fixed_height_and_scrollable():
    page = _make_page()
    box = page._test_label
    assert isinstance(box, QTextEdit)
    assert box.height() == 56, "测试结果框应为固定高度"
    assert box.minimumHeight() == box.maximumHeight() == 56
    assert box.verticalScrollBarPolicy() != \
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff


def test_load_card_not_compressible():
    page = _make_page()
    card = page._test_label.parentWidget()
    assert card.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Fixed, \
        "加载卡应按内容固定高度，避免被压缩后内容溢出"


def test_test_box_stays_inside_card_at_small_sizes():
    """窗口偏小时（整页滚动）测试框仍完整落在卡片内，不越界。"""
    page = _make_page(760, 560)
    box = page._test_label
    card = box.parentWidget()
    assert box.geometry().bottom() <= card.contentsRect().bottom(), \
        f"测试框越界：bottom={box.geometry().bottom()} " \
        f"card_bottom={card.contentsRect().bottom()}"
    # 内容高于视口 -> 出现纵向滚动（而不是压扁卡片）
    assert page._page_scroll.verticalScrollBar().maximum() > 0


def test_no_horizontal_scroll_at_normal_width():
    """主窗口最小尺寸（1000x680 -> 内容区约 800x640）下无横向溢出。"""
    page = _make_page(800, 640)
    assert page._page_scroll.horizontalScrollBar().maximum() == 0, \
        "800px 内容区不应出现横向溢出"


def test_log_card_pinned_bottom():
    """运行日志固定在页面底部：已加载模型再多也不被挤走（红字需求）。"""
    page = _make_page(1080, 640)
    log_card = page._log.parentWidget()
    base = log_card.geometry()
    assert base.bottom() <= page.height(), "运行日志应在可视区内"
    for n in (1, 3, 8):
        page._apply_pool({"loaded": [
            {"name": f"m{i}", "status": "paused", "progress": 0.0}
            for i in range(n)]})
        app.processEvents()
        app.processEvents()
        g = log_card.geometry()
        assert (g.y(), g.height()) == (base.y(), base.height()), \
            f"{n} 个模型时运行日志位置/高度被改变"
        assert g.bottom() <= page.height()


# --------------------------------------------------------------------------- #
# 加载区紧凑化（2026-10-21 红字：出现滚动条是已加载模型高度不够，
# 只要把模型加载的区域高度调小一点就行）
# --------------------------------------------------------------------------- #
def _two_models():
    return {"loaded": [
        {"name": "qwen2.5-0.5b-instruct-q4_k_m", "status": "paused",
         "progress": 0.0, "ctx": 32768, "threads": 16, "gpu_layers": 0},
        {"name": "llama-3.2-3b-instruct-q4_k_m", "status": "paused",
         "progress": 0.0, "ctx": 32768, "threads": 16, "gpu_layers": 0}]}


def test_load_card_compact_gives_room_to_loaded_models():
    """加载区高度收紧（56px 测试框 + 紧凑间距），给已加载模型让出空间。"""
    page = _make_page(1080, 640)
    card = page._test_label.parentWidget()
    assert page._test_label.height() == 56
    assert card.height() <= 152, f"加载卡过高：{card.height()}"
    assert card.layout().spacing() <= 6


def test_two_models_no_scroll_at_min_window():
    """主窗口最小高度（1080x640）下，两个已加载模型不需要滚动。"""
    page = _make_page(1080, 640)
    page._apply_pool(_two_models())
    app.processEvents()
    app.processEvents()
    assert page._loaded_scroll.can_scroll() is False, \
        "两个模型时已加载列表不应出现滚动条"
    assert page._loaded_scroll.overlay_visible() is False


def test_three_models_no_scroll_at_normal_window():
    """常规高度（1080x700）下，三个已加载模型不需要滚动。"""
    page = _make_page(1080, 700)
    data = _two_models()
    data["loaded"].append({"name": "qwen3-4b", "status": "ready",
                           "progress": 1.0, "ctx": 4096,
                           "threads": 8, "gpu_layers": 0})
    page._apply_pool(data)
    app.processEvents()
    app.processEvents()
    assert page._loaded_scroll.can_scroll() is False, \
        "三个模型时已加载列表不应出现滚动条"


def test_page_fits_min_window_height():
    """压缩后页面在最小窗口高度下不再需要整页滚动。"""
    page = _make_page(1080, 640)
    assert page._page_scroll.can_scroll() is False
    assert page._page_scroll.overlay_visible() is False
