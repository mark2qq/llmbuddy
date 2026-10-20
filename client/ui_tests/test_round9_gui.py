# -*- coding: utf-8 -*-
"""
第九轮 GUI 改版无头测试（红字 6 项中 GUI 侧 5 项）。

覆盖：
    - 图3：Enter 发送 / Shift+Enter 换行（_ChatInput 键事件）
    - 图2：模型面板 WorkBuddy 模式（分组标题 / 配置自定义模型 / 云端条目）
    - 图5：API 页选中模型显示对应 Key（未生成显示空）
    - 图1/图6：按钮缩小样式存在、日志区限高（QSS/布局参数断言）
"""
import os
import sys

# 客户端以 client/ 为包根；置于 sys.path 最前，保证 import core/ui 解析到 client 内。
# 同时移除仓库根 E:/llm-service，否则 pytest 预置的 rootdir 会让 `core` 解析到后端
# core/（无 task_runner 模块），导致 GUI 测试导入失败。
_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtCore import Qt, QEvent  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    """模拟后端：1 运行中 + 1 已下载；keys 含一个绑定模型的 key。"""

    def pool(self):
        return {"loaded": [
            {"name": "qwen3-4b", "status": "ready"},
            {"name": "llama-3.1-8b", "status": "paused"},
        ]}

    def list_models(self):
        return [{"id": "qwen3-4b"}, {"id": "llama-3.1-8b"}, {"id": "deepseek-7b"}]

    def list_keys(self):
        return [
            {"key": "aa11", "model": "", "created_at": "2026-10-19T10:00:00Z"},
            {"key": "bb22", "model": "qwen3-4b", "created_at": "2026-10-19T11:00:00Z"},
        ]

    def catalog(self, force=False, keyword=""):
        # 三个模型都视为「本程序已下载」，API 页本地过滤不会剔除
        return {"items": [
            {"name": "qwen3-4b", "local": True},
            {"name": "llama-3.1-8b", "local": True},
            {"name": "deepseek-7b", "local": True},
        ], "cached": True}


def _press_enter(input_widget, shift=False):
    key = Qt.Key.Key_Return
    mods = Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
    ev = QKeyEvent(QEvent.Type.KeyPress, key, mods, "\r" if not shift else "")
    input_widget.keyPressEvent(ev)


def _wait_until(cond, timeout=5.0):
    """驱动事件循环直到条件满足或超时。

    子线程启动 + 执行 + 跨线程信号入队需数毫秒，固定次数 processEvents
    会在信号到达前跑完（竞态），必须带 sleep 轮询。
    """
    import time
    deadline = time.time() + timeout
    while True:
        app.processEvents()
        if cond():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.005)


# ---------------------------------------------------------------------------
# 图3：Enter 发送 / Shift+Enter 换行
# ---------------------------------------------------------------------------
def test_enter_sends_and_shift_enter_newline():
    import threading
    from ui.pages.chat_page import ChatPage, _ChatInput

    sent = []
    box = _ChatInput()
    box.send_requested.connect(lambda: sent.append(True))

    _press_enter(box, shift=False)
    assert sent == [True], "Enter 应触发发送信号"

    _press_enter(box, shift=True)
    assert sent == [True], "Shift+Enter 不应触发发送信号（走换行）"

    # 集成：Enter 触发 _on_send 进入发送态；生成中可随时终止，完成后恢复
    gate = threading.Event()

    class _BlockApi(_FakeApi):
        def chat_stream(self, *a, **k):
            gate.wait(5)
            result = {"text": "", "finish": {}, "cancelled": False}
            # 契约：结束前必须回调 on_done（与真实 api_client 一致）
            on_done = k.get("on_done")
            if on_done:
                on_done(result)
            return result

    p = ChatPage(_BlockApi(), None, None)
    p.refresh()
    # 刷新已异步化（15 轮）：等模型聚合应用，否则 _current_model 为空触发模态弹窗
    assert _wait_until(lambda: bool(p._local_models)), "后台模型聚合未完成"
    p._input.setPlainText("你好")
    _press_enter(p._input, shift=False)
    for _ in range(30):
        app.processEvents()
        if p._busy:
            break
    assert p._busy is True, "Enter 应触发 _on_send 进入发送状态"
    assert p._gen_id == 1
    # 生成中：按钮变为停止态
    assert p._send_btn.toolTip() == "停止生成"

    gate.set()
    assert _wait_until(lambda: not p._busy), "生成完成后应恢复发送态"
    assert p._busy is False, "生成完成后应恢复发送态"
    assert p._send_btn.toolTip() == "Enter 发送，Shift+Enter 换行"


# ---------------------------------------------------------------------------
# 图2：WorkBuddy 模式面板
# ---------------------------------------------------------------------------
def test_model_menu_groups_and_custom_entry():
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    p.refresh()
    # 刷新已异步化（15 轮）：菜单改用缓存数据，须先等聚合结果
    assert _wait_until(lambda: bool(p._local_models)), "后台模型聚合未完成"
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions() if a.text()]

    assert any("本地模型 · 运行中" in t for t in texts)
    assert any("本地模型 · 已下载" in t for t in texts)
    assert any("线上免费模型" in t for t in texts)
    assert any("配置自定义模型" in t for t in texts)
    # 本地条目带状态图标前缀
    assert any("●" in t and "qwen3-4b" in t for t in texts)
    # 云端条目带 ☁ 前缀
    assert any("☁" in t and "Qwen/Qwen2.5-7B-Instruct" in t for t in texts)


def test_custom_cloud_model_persist_and_show():
    from ui.pages.chat_page import ChatPage, _CUSTOM_MODELS_KEY

    p = ChatPage(_FakeApi(), None, None)
    p._settings.setValue(_CUSTOM_MODELS_KEY, ["my-custom-model"])
    try:
        p._build_model_menu()
        texts = [a.text() for a in p._model_menu.actions() if a.text()]
        assert any("my-custom-model" in t and "自定义" in t for t in texts)
    finally:
        p._settings.setValue(_CUSTOM_MODELS_KEY, [])


# ---------------------------------------------------------------------------
# 图5：API 页按模型显示 Key
# ---------------------------------------------------------------------------
class _FakeConfig:
    def openai_base_url(self):
        return "http://127.0.0.1:8000/v1"


def test_api_page_per_model_key():
    from ui.pages.api_page import ApiPage

    p = ApiPage(_FakeApi(), None, _FakeConfig())
    p.refresh()
    # 刷新已异步化（16 轮）：等后台模型聚合完成再断言
    assert _wait_until(lambda: p._models.count() >= 2), "后台模型聚合未完成"
    assert p._models.count() >= 2

    # 选中 qwen3-4b（有绑定 key bb22）
    target = None
    for i in range(p._models.count()):
        if p._models.item(i).data(Qt.ItemDataRole.UserRole) == "qwen3-4b":
            target = i
            break
    p._models.setCurrentRow(target)
    # 手动触发 key 查询（避免依赖 currentItemChanged 异步时序）
    p._on_model_selected(p._models.item(target), None)
    assert p._selected_model == "qwen3-4b"
    # 等待后台 Worker 完成 keys 查询
    assert _wait_until(lambda: p._key_label.text()), "应查询到绑定 Key"
    assert p._key_label.text() == "bb22", "选中模型应显示其绑定 Key"

    # 选中未生成 Key 的模型 -> 显示空
    other = None
    for i in range(p._models.count()):
        if p._models.item(i).data(Qt.ItemDataRole.UserRole) == "deepseek-7b":
            other = i
            break
    p._key_label.setText("stale")   # 先放一个脏值，验证会被清空
    p._on_model_selected(p._models.item(other), None)
    assert _wait_until(lambda: p._key_label.text() != "stale"), \
        "切换模型后应刷新 Key 展示"
    assert p._key_label.text() == "", "未生成 Key 的模型应显示空"


# ---------------------------------------------------------------------------
# 图1/图6：样式与布局参数
# ---------------------------------------------------------------------------
def test_styles_and_layout_params():
    qss_path = os.path.join(_CLIENT_DIR, "resources", "styles.qss")
    with open(qss_path, "r", encoding="utf-8") as f:
        qss = f.read()
    # 图1：行按钮缩小（min-height 20 / max-height 24）
    assert "#RowBtn" in qss and "min-height: 20px" in qss
    assert "#RowCancelBtn { min-width: 64px; }" in qss
    # 图2：面板样式
    assert "QMenu#ModelMenu" in qss
    # 第十轮：底部模型下拉 + 圆形发送/停止按钮
    assert "#ModelDropBtn" in qss
    assert "#SendCircleBtn" in qss and "border-radius: 20px" in qss

    from ui.pages.dashboard_page import DashboardPage
    p = DashboardPage(_FakeApi(), None, None)
    # 图6：日志区限高 180、已加载区随上半区拉伸
    assert p._log.parentWidget().maximumHeight() == 180
