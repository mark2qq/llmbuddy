# -*- coding: utf-8 -*-
"""
第十轮 GUI 改版无头测试。

覆盖：
    - 图4/5/6/7：发送圆形按钮 空闲(↑)/生成中(■) 状态切换 + 停止生成可随时终止
    - 核心 bug：生成中异常/超时必须解除 busy（否则切换模型也无法发送）
    - 图2：模型库名称列改为 Interactive（可拖动）+ 自动获取有加载反馈
"""
import os
import sys

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
    """模拟后端：1 运行中 + 已下载。"""

    def pool(self):
        return {"loaded": [{"name": "qwen3-4b", "status": "ready"}]}

    def list_models(self):
        return [{"id": "qwen3-4b"}, {"id": "deepseek-7b"}]

    def system_info(self):
        return {"free_ram_gb": 8.0}

    def catalog(self, force=False, keyword=""):
        return {"items": [], "cached": False}


class _FakeLauncher:
    def start(self):
        return True

    def stop(self):
        return True


class _FakeConfig:
    models_dir = ""
    n_ctx = 8192
    n_threads = 4
    n_gpu_layers = 0

    def openai_base_url(self):
        return "http://127.0.0.1:8000/v1"


def _spin(n=60, until=None, timeout=5.0):
    """驱动 Qt 事件循环，处理子线程跨线程信号。

    不能只用固定次数自旋：子线程启动 + 执行 + 信号入队约需数毫秒，
    而 40 次 processEvents 往往 ~3ms 就跑完，断言会早于结果（竞态）。
    故改为：带 until 条件时等到满足或超时；不带则按 timeout 持续驱动。
    """
    import time
    deadline = time.time() + timeout
    while True:
        app.processEvents()
        if until is not None and until():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# 发送/停止圆形按钮状态
# ---------------------------------------------------------------------------
def test_send_button_state_switch():
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    p.refresh()
    p._set_send_state(False)
    assert "Enter 发送" in p._send_btn.toolTip()
    assert not p._send_btn.icon().isNull()
    p._set_send_state(True)
    assert p._send_btn.toolTip() == "停止生成"
    assert not p._send_btn.icon().isNull()


def test_stop_generation_resets_state():
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    p.refresh()
    # 模拟进入生成中
    p._busy = True
    p._gen_id = 1
    p._cancel_requested = False
    p._assistant_label = p._add_bubble("…", "assistant")
    p._set_send_state(True)

    p._on_stop_generation()
    _spin(5)
    assert p._busy is False, "停止后应解除 busy（可再次发送）"
    assert p._gen_id == 2, "停止应作废当前 generation"
    assert p._cancel_requested is True
    assert p._assistant_label is None
    assert "Enter 发送" in p._send_btn.toolTip(), "停止后按钮恢复发送态"


def test_error_path_resets_busy():
    """核心 bug 修复：推理异常/超时必须解除 busy，否则一直无法发送。"""
    from ui.pages.chat_page import ChatPage

    class _BoomApi(_FakeApi):
        def chat_stream(self, *a, **k):
            raise RuntimeError("engine stalled / timeout")

    p = ChatPage(_BoomApi(), None, None)
    p.refresh()
    p._input.setPlainText("你好")
    p._on_send()
    assert p._busy is True, "发送后应进入生成态"
    _spin(until=lambda: not p._busy)
    assert p._busy is False, "生成异常后必须解除 busy（红字：卡死无法发送）"
    assert "Enter 发送" in p._send_btn.toolTip()
    # 失败信息应体现在气泡里
    texts = []
    for i in range(p._content_layout.count()):
        w = p._content_layout.itemAt(i).widget()
        if w is not None and w.layout() and w.layout().count():
            lbl = w.layout().itemAt(0).widget()
            if lbl is not None:
                texts.append(lbl.text())
    assert any("[生成失败]" in t for t in texts), "异常应提示在气泡中"


def test_switch_model_after_error_can_send():
    """异常恢复后，切换模型应能再次发送（红字诉求的核心场景）。"""
    from ui.pages.chat_page import ChatPage

    class _BoomApi(_FakeApi):
        def chat_stream(self, *a, **k):
            raise RuntimeError("boom")

    p = ChatPage(_BoomApi(), None, None)
    p.refresh()
    p._input.setPlainText("第一条")
    p._on_send()
    _spin(until=lambda: not p._busy)
    assert p._busy is False

    # 切换到另一个本地模型，再次发送应能进入发送态
    p._on_pick_model("deepseek-7b")
    assert p._current_model == "deepseek-7b"
    p._input.setPlainText("第二条")
    p._on_send()
    _spin(until=lambda: not p._busy)
    # 第二次也异常，但关键是「能进入发送态」而非被 busy 永久卡死
    assert p._busy is False
    assert p._gen_id == 2


def test_no_on_done_callback_still_releases_busy():
    """兜底：chat_stream 正常返回但未触发 on_done，busy 也必须释放。

    对应用户红字：一直运行中->无论如何切换模型都无法发送信息。
    只要 worker 走完（不管回调是否触发），就不能卡在生成态。
    """
    from ui.pages.chat_page import ChatPage

    class _NoDoneApi(_FakeApi):
        def chat_stream(self, *a, **k):
            # 故意不调用 on_done（模拟实现漏回调的路径）
            return {"text": "部分输出", "finish": {}, "cancelled": False}

    p = ChatPage(_NoDoneApi(), None, None)
    p.refresh()
    p._input.setPlainText("你好")
    p._on_send()
    assert p._busy is True
    _spin(until=lambda: not p._busy)
    assert p._busy is False, "worker 返回后必须释放 busy（兜底补发 done）"
    assert "Enter 发送" in p._send_btn.toolTip()


# ---------------------------------------------------------------------------
# 图1/图2：模型库
# ---------------------------------------------------------------------------
def test_library_name_column_interactive():
    from PySide6.QtWidgets import QHeaderView
    from ui.pages.library_page import LibraryPage

    p = LibraryPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    hdr = p._table.horizontalHeader()
    assert hdr.sectionResizeMode(0) == QHeaderView.Interactive, \
        "名称列应可左右拖动（红字：和仓库列一样）"
    assert p._table.columnWidth(0) == 340


def test_library_refresh_has_loading_feedback():
    from ui.pages.library_page import LibraryPage

    p = LibraryPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    p.refresh(True)
    # 同步断言：进入加载态（红字：点击没反应）
    assert not p._refresh_btn.isEnabled(), "获取期间应禁用按钮"
    assert "正在获取模型目录" in p._hint.text(), "应有加载提示"
    _spin(until=lambda: p._refresh_btn.isEnabled())
    assert p._refresh_btn.isEnabled(), "完成后应恢复按钮"
    assert "个模型" in p._hint.text()
