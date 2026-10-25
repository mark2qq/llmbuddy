# -*- coding: utf-8 -*-
"""
第十五轮 GUI 改版无头测试。

覆盖（红字要求）：
    - API 页「当前模型」行新增「复制」按钮：可一键复制模型名称，
      参考下方 Key 的复制模式（点击后短暂显示「已复制」再还原）
    - 未选中具体模型（全局 Key 入口 / 无模型）时按钮置灰不可点
    - 侧栏标题改版：产品名放大、版本号灰色置于其下
    - 对话页刷新改为后台线程：不再阻塞主线程、不再重复请求
"""
import os
import sys
import time

# 客户端以 client/ 为包根；置于 sys.path 最前，保证 import core/ui 解析到 client 内。
_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    def pool(self):
        return {"loaded": [{"name": "qwen3-4b", "status": "ready"}]}

    def list_models(self):
        return [{"id": "qwen3-4b", "loaded": True, "downloaded": True},
                {"id": "deepseek-7b", "loaded": False, "downloaded": True}]

    def list_keys(self):
        return []

    def system_info(self):
        return {"free_ram_gb": 8.0}

    def catalog(self, force=False, keyword=""):
        return {"items": [
            {"name": "qwen3-4b", "local": True},
            {"name": "deepseek-7b", "local": True},
        ], "cached": True}


class _FakeConfig:
    models_dir = ""
    n_ctx = 8192
    n_threads = 4
    n_gpu_layers = 0

    def openai_base_url(self):
        return "http://127.0.0.1:8000/v1"


def _wait_models_applied(p, timeout=5.0):
    """等待 ApiPage.refresh() 的后台模型聚合完成（刷新已异步化：16 轮）。

    列表重建后 count >= 2（第 0 项全局 Key + 至少一条模型/提示项）；
    初始未填充时 count == 0。
    """
    import time as _time
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        app.processEvents()
        _time.sleep(0.02)
        if p._models.count() >= 2:
            return
    raise AssertionError("refresh() 后台模型聚合未在限时内完成")


def test_api_page_copy_model_button_copies_name():
    """红字：当前模型行加「复制」按钮，点击复制模型名称。"""
    from ui.pages.api_page import ApiPage

    p = ApiPage(_FakeApi(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    assert p._selected_model == "qwen3-4b"   # 默认选中已加载模型
    assert p._copy_model_btn.text() == "复制"
    assert p._copy_model_btn.isEnabled()

    p._copy_model_btn.click()
    cb = app.clipboard()
    assert cb.text() == "qwen3-4b"           # 已复制完整模型名
    assert p._copy_model_btn.text() == "已复制"   # 反馈文案（1.8s 后由定时器还原）
    p._copy_model_btn.setText("复制")          # 手动还原，避免定时器残留


def test_api_page_copy_model_disabled_when_global():
    """红字：选中「全局 Key」入口（未绑定模型）时复制按钮置灰。"""
    from ui.pages.api_page import ApiPage

    p = ApiPage(_FakeApi(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    p._models.setCurrentRow(0)               # 第 0 项 = 全局 Key
    assert p._selected_model == ""
    assert not p._copy_model_btn.isEnabled()
    # 点击不应产生任何剪贴板写入
    before = app.clipboard().text()
    p._copy_model_btn.click()
    assert app.clipboard().text() == before


def test_api_page_copy_model_no_models_disabled():
    """无任何已下载模型时按钮同样置灰。"""
    from ui.pages.api_page import ApiPage

    class _EmptyApi(_FakeApi):
        def list_models(self):
            return []

        def catalog(self, force=False, keyword=""):
            return {"items": [], "cached": False}

    p = ApiPage(_EmptyApi(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    assert not p._selected_model            # 无模型可选中（None 或空串）
    assert not p._copy_model_btn.isEnabled()


# ---------------------------------------------------------------------------
# 侧栏标题改版（红字：字体放大；版本号灰色放到名称下并缩小一号）
# ---------------------------------------------------------------------------
def test_nav_title_name_enlarged_and_version_below():
    from PySide6.QtWidgets import QWidget, QLabel
    from ui.main_window import MainWindow
    from test_round13_gui import _FakeLauncher as Round13Launcher, _FakeConfig as Round13Config

    win = MainWindow(_FakeApi(), Round13Launcher(), Round13Config())
    win._refresh_all = lambda: None
    nav = win.findChild(QWidget, "NavTitle")
    assert nav is not None
    name_lbl = nav.findChild(QLabel, "NavTitleLabel")
    ver_lbl = nav.findChild(QLabel, "NavVersionLabel")
    assert name_lbl is not None and name_lbl.text() == "llmBuddy"
    assert ver_lbl is not None and ver_lbl.text().startswith("v")
    # 布局顺序：版本号在产品名之后（置于其下方）
    lay = nav.layout()
    items = [lay.itemAt(i).widget() for i in range(lay.count())]
    assert items.index(ver_lbl) > items.index(name_lbl), "版本号应排在产品名下方"
    # QSS 字号：产品名放大（18px）、版本号小一号且灰色（11px）
    qss_path = os.path.join(_CLIENT_DIR, "resources", "styles.qss")
    with open(qss_path, encoding="utf-8") as f:
        qss = f.read()
    assert "#NavTitleLabel" in qss and "font-size: 18px" in qss
    assert "#NavVersionLabel" in qss and "font-size: 11px" in qss
    ver_block = qss.split("#NavVersionLabel")[1].split("}")[0]
    assert "#7d8596" in ver_block, "版本号应为灰色"


# ---------------------------------------------------------------------------
# 对话页刷新异步化（红字：切换到对话模块时反应比较慢，排查原因）
# 根因：refresh() 在主线程同步发 2 个 HTTP 请求，且 _build_model_menu
# 又重复请求 2 次。修复：后台线程聚合 + 菜单只用缓存。
# ---------------------------------------------------------------------------
def test_chat_refresh_async_and_no_duplicate_requests():
    from ui.pages.chat_page import ChatPage

    calls = {"pool": 0, "list": 0}

    class _CountApi(_FakeApi):
        def pool(self):
            calls["pool"] += 1
            time.sleep(0.05)          # 模拟网络耗时
            return super().pool()

        def list_models(self):
            calls["list"] += 1
            time.sleep(0.05)
            return super().list_models()

    p = ChatPage(_CountApi(), None, _FakeConfig())

    calls["pool"] = calls["list"] = 0
    t0 = time.perf_counter()
    p.refresh()
    elapsed = time.perf_counter() - t0
    # 关键断言 1：refresh() 必须立即返回（< 30ms），不得在主线程等网络
    assert elapsed < 0.03, f"refresh() 阻塞主线程 {elapsed*1000:.0f}ms"
    # 关键断言 2：触发时刻主线程零请求（聚合在后台线程进行）
    assert calls["pool"] == 0 and calls["list"] == 0

    # 等后台线程完成并应用结果
    deadline = time.time() + 3
    while time.time() < deadline and not p._local_models:
        app.processEvents()
        time.sleep(0.02)
    assert p._local_models == ["qwen3-4b", "deepseek-7b"]
    # 关键断言 3：一次刷新每接口只调 1 次（修复前菜单重建会再调一轮）
    assert calls["pool"] == 1 and calls["list"] == 1

    # 关键断言 4：_build_model_menu 不发任何网络请求（打开菜单瞬时完成）
    calls["pool"] = calls["list"] = 0
    p._build_model_menu()
    p._on_pick_model("qwen3-4b")
    assert calls["pool"] == 0 and calls["list"] == 0
