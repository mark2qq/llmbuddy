# -*- coding: utf-8 -*-
"""
图3 对话页模型选择面板（无头烟雾测试）。

验证点：
    - refresh() 默认选中「本地正在运行」模型
    - _build_model_menu 面板含两大类（本地模型 / 在线模型，自定义模型为在线段子分组）
    - _on_pick_model 选中后更新 _current_model 与标签
    - 发送云端模型时走 SiliconFlow 直连（复用 chat_stream_custom）
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

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

# QSettings 隔离：本文件早期版本缺少该 fixture，曾读到用户真实 cloud_api_key
# 并可能在未配置 Key 的机器上把测试用假 Key 写进真实配置（覆盖用户真 Key）。
# 现对云端/自定义模型相关全部键做 保存->清空->恢复。
_ISOLATED_KEYS = (
    "chat/custom_models",
    "chat/custom_cloud_models",
    "chat/cloud_api_key",
    "chat/cloud_models",
    "chat/cloud_models_fetched_at",
)


@pytest.fixture(autouse=True)
def _isolate_settings():
    saved = []
    for org, app_name in (("llmBuddy", "llmBuddy"), ("LLMDesktop", "LLMDesktop")):
        s = QSettings(org, app_name)
        state = {key: s.value(key, None) for key in _ISOLATED_KEYS}
        for key in _ISOLATED_KEYS:
            s.remove(key)
        s.sync()
        saved.append((org, app_name, state))
    yield
    for org, app_name, state in saved:
        s = QSettings(org, app_name)
        for key in _ISOLATED_KEYS:
            if state[key] is None:
                s.remove(key)
            else:
                s.setValue(key, state[key])
        s.sync()


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
            {"id": "qwen3-4b", "downloaded": True},
            {"id": "llama-3.1-8b", "downloaded": True},
            {"id": "deepseek-7b", "downloaded": True},
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


def test_menu_has_two_sections():
    p = _make_page()
    p.refresh()
    _wait_models(p)
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions()]
    # 新结构收敛为两大类：本地模型 / 在线模型 · SiliconFlow
    assert any("本地模型" in t for t in texts)
    assert any("在线模型 · SiliconFlow" in t for t in texts)
    # 本地模型合并为一段：运行中(●)的 qwen3-4b 与已下载(○)的 llama-3.1-8b、deepseek-7b 仍在菜单
    assert any("●" in t and "qwen3-4b" in t for t in texts)
    assert any("○" in t and "llama-3.1-8b" in t for t in texts)
    assert any("○" in t and "deepseek-7b" in t for t in texts)
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


def test_send_cloud_model_routes_to_siliconflow(monkeypatch):
    """选云端模型发送：走 SiliconFlow 直连（复用 chat_stream_custom），不再拦截。"""
    from ui.pages.chat_page import ChatPage, CloudKeyDialog, SILICONFLOW_BASE_URL

    calls = {}

    class _CloudApi(_FakeApi):
        def chat_stream_custom(self, entry, messages, **kw):
            calls["entry"] = entry
            calls["messages"] = list(messages)
            kw["on_done"]({"text": "hi", "finish": {}, "cancelled": False})
            return {"text": "hi", "finish": {}, "cancelled": False}

    # 未配置 Key：模拟用户在对话框里输入并点「保存并继续」
    monkeypatch.setattr(CloudKeyDialog, "exec",
                        lambda self: (setattr(self, "_saved_key", "sk-cloud")
                                      or True))

    import time
    p = ChatPage(_CloudApi(), None, None)
    p.refresh()
    _wait_models(p)
    p._on_pick_model("Qwen/Qwen2.5-7B-Instruct")

    p._input.setPlainText("你好")
    p._on_send()
    deadline = time.time() + 5.0
    while time.time() < deadline and p._busy:
        app.processEvents()
        time.sleep(0.02)

    assert p._busy is False
    assert calls["entry"]["base_url"] == SILICONFLOW_BASE_URL
    assert calls["entry"]["api_key"] == "sk-cloud"
    assert calls["entry"]["model"] == "Qwen/Qwen2.5-7B-Instruct"
    assert calls["messages"][-1] == {"role": "user", "content": "你好"}
