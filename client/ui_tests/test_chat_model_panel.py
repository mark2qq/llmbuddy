# -*- coding: utf-8 -*-
"""
图3 对话页模型选择面板（无头烟雾测试）。

验证点：
    - refresh() 默认选中「本地正在运行」模型
    - _build_model_menu 面板含三分组（本地运行中 / 本地已下载 / 云端免费模型）
    - _on_pick_model 选中后更新 _current_model 与标签
    - 发送云端模型时给出未接入提示（不真正发送）
"""
import os
import sys

# 客户端以 client/ 为包根；置于 sys.path 最前，确保 import core/ui 解析到 client 内。
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

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    """模拟后端：running 1 个，已下载 2 个，云端固定。"""

    def pool(self):
        return {
            "max_concurrent": 1,
            "running": 1,
            "engine_base_port": 7999,
            "loaded": [
                {"name": "qwen3-4b", "status": "ready"},
                {"name": "llama-3.1-8b", "status": "paused"},
            ],
        }

    def list_models(self):
        # 与 api_client.list_models() 解包后一致：返回列表
        return [
            {"id": "qwen3-4b"},
            {"id": "llama-3.1-8b"},
            {"id": "deepseek-7b"},
        ]

    def chat_stream(self, *a, **k):  # pragma: no cover
        raise AssertionError("不应发送云端模型")


def _make_page():
    from ui.pages.chat_page import ChatPage
    return ChatPage(_FakeApi(), None, None)


def _wait_models(p, timeout=5.0):
    """等待 refresh() 的后台模型聚合完成（刷新已异步化：15 轮）。

    ChatPage.refresh() 改在后台线程请求 pool/list_models，测试必须在
    断言/发送前等结果应用，否则 _current_model 为空（会触发模态弹窗阻塞）。
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
        if p._local_split["running"] or p._local_split["not_running"]:
            return
    raise AssertionError("refresh() 后台模型聚合未在限时内完成")


def test_default_selects_running():
    p = _make_page()
    p.refresh()
    _wait_models(p)
    assert p._current_model == "qwen3-4b", p._current_model
    assert "qwen3-4b" in p._model_btn.text()


def test_menu_has_three_sections():
    p = _make_page()
    p.refresh()
    _wait_models(p)
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions()]
    assert any("本地模型 · 运行中" in t for t in texts)
    assert any("本地模型 · 已下载" in t for t in texts)
    assert any("线上免费模型" in t for t in texts)
    # 本地已下载应含未运行的 llama-3.1-8b 与 deepseek-7b
    assert any("llama-3.1-8b" in t for t in texts)
    assert any("deepseek-7b" in t for t in texts)
    # 云端项存在
    assert any("Qwen/Qwen2.5-7B-Instruct" in t for t in texts)
    # WorkBuddy 模式：底部「配置自定义模型」入口
    assert any("配置自定义模型" in t for t in texts)


def test_pick_model_updates_state():
    p = _make_page()
    p.refresh()
    _wait_models(p)
    p._on_pick_model("deepseek-7b")
    assert p._current_model == "deepseek-7b"
    assert "deepseek-7b" in p._model_btn.text()


def test_send_cloud_model_blocked(qtbot_or_none=None):
    """选云端模型发送应被拦截提示，不调用 chat_stream。"""
    p = _make_page()
    p.refresh()
    _wait_models(p)
    p._on_pick_model("Qwen/Qwen2.5-7B-Instruct")
    # 未真正发送：直接调用内部拦截判断
    assert p._current_model not in p._local_models
    # 模拟输入框有内容并触发发送（应走提示分支，不抛 AssertionError）
    p._input.setPlainText("你好")
    # 捕获 QMessageBox.information
    shown = {}

    def _fake_info(_self, title, msg):
        shown["title"] = title
        shown["msg"] = msg

    from PySide6.QtWidgets import QMessageBox
    orig = QMessageBox.information
    QMessageBox.information = _fake_info
    try:
        p._on_send()
    finally:
        QMessageBox.information = orig
    assert shown.get("title") == "线上模型未接入"
    assert p._busy is False  # 未进入发送
