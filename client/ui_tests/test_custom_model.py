# -*- coding: utf-8 -*-
"""
对话页「配置自定义模型」完整流程测试（1.0.7）。

覆盖：
    1. custom_chat_url 三种输入（带 /v1、不带 /v1、带尾斜杠）
    2. CustomModelDialog 校验（必填缺失不关闭）与保存回填
    3. 旧键 chat/custom_cloud_models -> 新键 chat/custom_models 迁移
    4. chat_page 菜单含「自定义模型」段与「⚙ 配置自定义模型」入口
    5. 选中自定义模型后 _current_custom 正确、发送走 chat_stream_custom
       （monkeypatch/fake api 断言参数，不发真实网络请求）

QSettings 隔离：autouse fixture 在测试前保存并移除 llmBuddy / LLMDesktop 的
chat/custom_models、chat/custom_cloud_models 两个键，测试结束后恢复原值，
不污染也不破坏真实用户配置。
"""
import json
import os
import sys
import time

import pytest

# 客户端以 client/ 为包根；置于 sys.path 最前，保证 import core/ui 解析到 client 内。
_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

CUSTOM_KEY = "chat/custom_models"
LEGACY_KEY = "chat/custom_cloud_models"
_ORG_APP = [("llmBuddy", "llmBuddy"), ("LLMDesktop", "LLMDesktop")]


@pytest.fixture(autouse=True)
def _isolate_settings():
    """保存 -> 清空 -> 测试 -> 恢复，隔离 QSettings，不污染真实配置。"""
    saved = []
    for org, app_name in _ORG_APP:
        s = QSettings(org, app_name)
        state = {key: s.value(key, None) for key in (CUSTOM_KEY, LEGACY_KEY)}
        for key in (CUSTOM_KEY, LEGACY_KEY):
            s.remove(key)
        s.sync()
        saved.append((org, app_name, state))
    yield
    for org, app_name, state in saved:
        s = QSettings(org, app_name)
        for key in (CUSTOM_KEY, LEGACY_KEY):
            if state[key] is None:
                s.remove(key)
            else:
                s.setValue(key, state[key])
        s.sync()


def _set_llmbuddy(key, value):
    s = QSettings("llmBuddy", "llmBuddy")
    s.setValue(key, value)
    s.sync()


def _get_llmbuddy(key):
    s = QSettings("llmBuddy", "llmBuddy")
    v = s.value(key, None)
    s.sync()
    return v


def _spin(until=None, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        app.processEvents()
        if until is not None and until():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.005)


class _FakeApi:
    def pool(self):
        return {"loaded": []}

    def list_models(self):
        return []


# --------------------------------------------------------------------------- #
# 1) custom_chat_url
# --------------------------------------------------------------------------- #
def test_custom_chat_url_variants():
    from core.api_client import custom_chat_url

    assert custom_chat_url("https://api.siliconflow.cn/v1") == \
        "https://api.siliconflow.cn/v1/chat/completions"
    assert custom_chat_url("https://api.siliconflow.cn") == \
        "https://api.siliconflow.cn/v1/chat/completions"
    assert custom_chat_url("https://api.siliconflow.cn/v1/") == \
        "https://api.siliconflow.cn/v1/chat/completions"
    assert custom_chat_url("https://api.siliconflow.cn/") == \
        "https://api.siliconflow.cn/v1/chat/completions"


# --------------------------------------------------------------------------- #
# 2) CustomModelDialog 校验与保存回填
# --------------------------------------------------------------------------- #
def test_dialog_missing_required_does_not_close():
    from ui.widgets.custom_model_dialog import CustomModelDialog

    dlg = CustomModelDialog()
    dlg._on_save()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert dlg._error_label.text() != ""


def test_dialog_save_writes_back():
    from ui.widgets.custom_model_dialog import (CustomModelDialog,
                                                load_custom_models)

    dlg = CustomModelDialog()
    dlg._name_edit.setText("我的模型")
    dlg._base_url_edit.setText("https://api.example.com/v1")
    dlg._api_key_edit.setText("sk-123")
    dlg._model_edit.setText("Qwen/Qwen2.5-7B-Instruct")
    dlg._on_save()

    assert dlg.result() == QDialog.DialogCode.Accepted
    entries = load_custom_models(QSettings("llmBuddy", "llmBuddy"), None)
    assert len(entries) == 1
    assert entries[0] == {
        "name": "我的模型",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-123",
        "model": "Qwen/Qwen2.5-7B-Instruct",
    }


def test_dialog_name_defaults_to_model():
    from ui.widgets.custom_model_dialog import (CustomModelDialog,
                                                load_custom_models)

    dlg = CustomModelDialog()
    dlg._base_url_edit.setText("https://api.example.com/v1")
    dlg._api_key_edit.setText("sk-123")
    dlg._model_edit.setText("model-x")
    dlg._on_save()
    entries = load_custom_models(QSettings("llmBuddy", "llmBuddy"), None)
    assert entries[0]["name"] == "model-x"
    assert entries[0]["model"] == "model-x"


def test_dialog_prefills_from_entry():
    from ui.widgets.custom_model_dialog import CustomModelDialog

    entry = {"name": "N", "base_url": "https://x/v1", "api_key": "k", "model": "m"}
    dlg = CustomModelDialog(entry=entry)
    assert dlg._name_edit.text() == "N"
    assert dlg._base_url_edit.text() == "https://x/v1"
    assert dlg._api_key_edit.text() == "k"
    assert dlg._model_edit.text() == "m"
    assert dlg._editing == entry


def test_dialog_edit_updates_existing_entry():
    from ui.widgets.custom_model_dialog import (CustomModelDialog,
                                                load_custom_models,
                                                save_custom_models)

    s = QSettings("llmBuddy", "llmBuddy")
    save_custom_models(s, [{"name": "模型A", "base_url": "https://a/v1",
                            "api_key": "old", "model": "m-a"}])
    dlg = CustomModelDialog()
    dlg._edit_combo.setCurrentIndex(1)  # 选中「模型A」
    assert dlg._editing["model"] == "m-a"
    dlg._api_key_edit.setText("new-key")
    dlg._on_save()

    entries = load_custom_models(QSettings("llmBuddy", "llmBuddy"), None)
    assert len(entries) == 1
    assert entries[0]["name"] == "模型A"
    assert entries[0]["api_key"] == "new-key"


def test_dialog_delete_removes_entry(monkeypatch):
    from ui.widgets.custom_model_dialog import (CustomModelDialog,
                                                load_custom_models,
                                                save_custom_models)

    s = QSettings("llmBuddy", "llmBuddy")
    save_custom_models(s, [{"name": "模型A", "base_url": "https://a/v1",
                            "api_key": "k", "model": "m-a"}])
    monkeypatch.setattr(
        QMessageBox, "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))

    dlg = CustomModelDialog()
    dlg._edit_combo.setCurrentIndex(1)
    dlg._on_delete()
    assert load_custom_models(QSettings("llmBuddy", "llmBuddy"), None) == []


# --------------------------------------------------------------------------- #
# 3) 旧键 -> 新键迁移
# --------------------------------------------------------------------------- #
def test_migration_from_legacy_key():
    from ui.pages.chat_page import ChatPage

    _set_llmbuddy(LEGACY_KEY, ["old-a", "old-b"])
    p = ChatPage(_FakeApi(), None, None)
    entries = p._custom_models()
    assert entries == [
        {"name": "old-a", "base_url": "", "api_key": "", "model": "old-a"},
        {"name": "old-b", "base_url": "", "api_key": "", "model": "old-b"},
    ]
    # 新键已写入（一次性迁移）
    stored = json.loads(_get_llmbuddy(CUSTOM_KEY))
    assert [e["model"] for e in stored] == ["old-a", "old-b"]


# --------------------------------------------------------------------------- #
# 4) 菜单含自定义模型段与配置入口
# --------------------------------------------------------------------------- #
def test_menu_contains_custom_section():
    from ui.pages.chat_page import ChatPage
    from ui.widgets.custom_model_dialog import save_custom_models

    save_custom_models(QSettings("llmBuddy", "llmBuddy"),
                       [{"name": "我的模型", "base_url": "https://x/v1",
                         "api_key": "k", "model": "m"}])
    p = ChatPage(_FakeApi(), None, None)
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions()]
    assert any("自定义模型" in t for t in texts)
    assert any("我的模型" in t for t in texts)
    assert any("配置自定义模型" in t for t in texts)
    assert any("线上免费模型" in t for t in texts)


# --------------------------------------------------------------------------- #
# 5) 选中自定义模型 + 发送走 chat_stream_custom
# --------------------------------------------------------------------------- #
def test_pick_custom_and_send_routes():
    from ui.pages.chat_page import ChatPage
    from ui.widgets.custom_model_dialog import save_custom_models

    save_custom_models(QSettings("llmBuddy", "llmBuddy"),
                       [{"name": "我的模型", "base_url": "https://api.example.com/v1",
                         "api_key": "sk-abc", "model": "mymodel"}])

    calls = {}

    class _RoutingApi:
        def pool(self):
            return {"loaded": []}

        def list_models(self):
            return []

        def chat_stream_custom(self, entry, messages, **kw):
            calls["entry"] = entry
            calls["messages"] = list(messages)
            calls["kw"] = kw
            kw["on_done"]({"text": "hi", "finish": {}, "cancelled": False})
            return {"text": "hi", "finish": {}, "cancelled": False}

    p = ChatPage(_RoutingApi(), None, None)
    p._on_pick_model("custom:我的模型")
    assert p._current_custom is not None
    assert p._current_custom["model"] == "mymodel"
    assert p._current_model == "我的模型"
    # 按钮前缀用 ☁（自定义模型，即便与本地重名也保持）
    assert p._model_btn.text().startswith("☁")

    p._input.setPlainText("你好")
    p._on_send()
    assert _spin(until=lambda: p._busy is False), "自定义模型发送未在限时内完成"

    assert calls["entry"]["base_url"] == "https://api.example.com/v1"
    assert calls["entry"]["api_key"] == "sk-abc"
    assert calls["entry"]["model"] == "mymodel"
    assert calls["messages"][-1] == {"role": "user", "content": "你好"}
    assert calls["kw"].get("max_tokens") == 2048
    assert "on_chunk" in calls["kw"]
    assert "on_done" in calls["kw"]
    assert "cancel_check" in calls["kw"]


def test_custom_missing_config_does_not_send(monkeypatch):
    """旧迁移数据缺 base_url/api_key：弹配置不完整提示，不发请求。"""
    from ui.pages.chat_page import ChatPage

    _set_llmbuddy(LEGACY_KEY, ["旧模型"])
    calls = {}

    class _NoSendApi:
        def pool(self):
            return {"loaded": []}

        def list_models(self):
            return []

        def chat_stream_custom(self, *a, **k):
            calls["sent"] = True
            return {"text": "", "cancelled": False}

    captured = {}

    def fake_question(parent, title, text, *a, **k):
        captured["title"] = title
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))

    p = ChatPage(_NoSendApi(), None, None)
    p._on_pick_model("custom:旧模型")
    p._input.setPlainText("你好")
    p._on_send()

    assert captured["title"] == "自定义模型配置不完整"
    assert p._busy is False
    assert "sent" not in calls


# --------------------------------------------------------------------------- #
# 6) custom_chat_url 边界（多尾斜杠 / 非 /v1 版本前缀 / 裸主机）
# --------------------------------------------------------------------------- #
def test_custom_chat_url_more_edge_cases():
    from core.api_client import custom_chat_url

    # 多个尾斜杠统一去除
    assert custom_chat_url("https://api.x/v1///") == \
        "https://api.x/v1/chat/completions"
    assert custom_chat_url("https://api.x////") == \
        "https://api.x/v1/chat/completions"
    # 仅识别「以 /v1 结尾」；其它版本前缀（/v1beta、/v2）一律补 /v1（已知限制）
    assert custom_chat_url("https://api.x/v1beta") == \
        "https://api.x/v1beta/v1/chat/completions"
    assert custom_chat_url("https://api.x/v2") == \
        "https://api.x/v2/v1/chat/completions"
    # 无 scheme 的裸主机同样补全
    assert custom_chat_url("api.x") == "api.x/v1/chat/completions"


# --------------------------------------------------------------------------- #
# 7) 存储迁移幂等性：重复读取不重复追加；新键写入后旧键不再干扰
# --------------------------------------------------------------------------- #
def test_migration_is_idempotent():
    from ui.widgets.custom_model_dialog import load_custom_models

    _set_llmbuddy(LEGACY_KEY, ["old-a"])
    s = QSettings("llmBuddy", "llmBuddy")
    legacy = QSettings("LLMDesktop", "LLMDesktop")

    e1 = load_custom_models(s, legacy)
    assert [e["model"] for e in e1] == ["old-a"]
    # 第二次读取：直接从新键返回，绝不重复追加
    e2 = load_custom_models(s, legacy)
    assert [e["model"] for e in e2] == ["old-a"]
    assert len(e2) == 1


def test_new_key_written_then_legacy_ignored():
    from ui.widgets.custom_model_dialog import (load_custom_models,
                                                save_custom_models)

    s = QSettings("llmBuddy", "llmBuddy")
    save_custom_models(s, [{"name": "n", "base_url": "https://x/v1",
                            "api_key": "k", "model": "m"}])
    # 旧键里塞入脏数据：新键已存在时不应再被旧键影响
    _set_llmbuddy(LEGACY_KEY, ["should-be-ignored"])
    entries = load_custom_models(s, QSettings("LLMDesktop", "LLMDesktop"))
    assert [e["model"] for e in entries] == ["m"]


# --------------------------------------------------------------------------- #
# 8) chat_stream_custom 直连自定义端点（SSE 解析 / 头部 / >=400 先 read 再抛 /
#    无 401 重试）—— 全部用 fake httpx，不发真实网络请求
# --------------------------------------------------------------------------- #
class _FakeStreamResp:
    def __init__(self, status_code, lines=(), text=""):
        self.status_code = status_code
        self._lines = list(lines)
        self.text = text
        self._read_called = False

    def iter_lines(self):
        return iter(self._lines)

    def read(self):
        self._read_called = True
        return self.text


def _patch_httpx(monkeypatch, responses):
    """用记录型 fake httpx.Client 替换真实客户端，按顺序消费 responses。"""
    from core import api_client as ac

    calls = {"clients": [], "requests": []}
    resp_iter = iter(responses)

    class _RespCtx:
        def __init__(self, resp):
            self._resp = resp

        def __enter__(self):
            return self._resp

        def __exit__(self, *a):
            return False

    class _FakeClient:
        def __init__(self, **kwargs):
            calls["clients"].append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, json=None, **kw):
            calls["requests"].append(
                {"method": method, "url": url, "json": json, "kw": kw})
            return _RespCtx(next(resp_iter))

    monkeypatch.setattr(ac.httpx, "Client", _FakeClient)
    return calls


def test_chat_stream_custom_sse_and_headers(monkeypatch):
    from core.api_client import ApiClient

    sse = [
        'data: {"choices":[{"delta":{"content":"你"}}]}',
        'data: {"choices":[{"delta":{"content":"好"}}]}',
        'data: [DONE]',
    ]
    calls = _patch_httpx(monkeypatch, [_FakeStreamResp(200, sse)])

    client = ApiClient()
    chunks = []
    result = client.chat_stream_custom(
        {"name": "m", "base_url": "https://api.x/v1/", "api_key": "sk-1",
         "model": "gpt"},
        [{"role": "user", "content": "hi"}],
        on_chunk=chunks.append)

    kw = calls["clients"][0]
    assert kw.get("trust_env") is False
    assert kw["headers"]["Authorization"] == "Bearer sk-1"
    assert kw["headers"]["Accept"] == "application/json"

    req = calls["requests"][0]
    assert req["method"] == "POST"
    # 尾斜杠被 custom_chat_url 规整
    assert req["url"] == "https://api.x/v1/chat/completions"
    assert req["json"]["model"] == "gpt"
    assert req["json"]["stream"] is True

    assert "".join(chunks) == "你好"
    assert result["text"] == "你好"


def test_chat_stream_custom_http_error_reads_then_raises_no_retry(monkeypatch):
    import core.api_client as ac
    from core.api_client import ApiClient

    resp = _FakeStreamResp(401, text="unauthorized")
    calls = _patch_httpx(monkeypatch, [resp])

    client = ApiClient()
    with pytest.raises(ac.ApiError) as ei:
        client.chat_stream_custom(
            {"base_url": "https://api.x/v1", "api_key": "k", "model": "m"},
            [{"role": "user", "content": "hi"}],
            on_chunk=lambda s: None)

    assert ei.value.code == 401
    assert resp._read_called is True   # 先 read() 再抛，避免掩盖真实状态码
    assert len(calls["requests"]) == 1  # 自定义端点不做网关 401 重试


# --------------------------------------------------------------------------- #
# 9) 对话框「测试连接」_test_custom_entry（fake httpx，不发真实网络）
# --------------------------------------------------------------------------- #
def test_test_custom_entry_success_and_error(monkeypatch):
    from ui.widgets import custom_model_dialog as cmd

    calls = {}

    class _Resp:
        def __init__(self, status_code, text=""):
            self.status_code = status_code
            self.text = text

    class _FakeClient:
        def __init__(self, **kwargs):
            calls["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None):
            calls["url"] = url
            calls["json"] = json
            return calls["resp"]

    monkeypatch.setattr(cmd.httpx, "Client", _FakeClient)

    calls["resp"] = _Resp(200, '{"ok":true}')
    ms = cmd._test_custom_entry(
        {"base_url": "https://api.x/v1", "api_key": "sk-1", "model": "m"})
    assert calls["kwargs"].get("trust_env") is False
    assert calls["kwargs"]["headers"]["Authorization"] == "Bearer sk-1"
    assert calls["url"] == "https://api.x/v1/chat/completions"
    assert calls["json"]["model"] == "m"
    assert isinstance(ms, float) and ms >= 0

    calls["resp"] = _Resp(401, "unauthorized")
    with pytest.raises(RuntimeError):
        cmd._test_custom_entry(
            {"base_url": "https://api.x/v1", "api_key": "sk-1", "model": "m"})
