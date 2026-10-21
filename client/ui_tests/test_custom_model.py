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
CLOUD_KEY = "chat/cloud_api_key"
CLOUD_MODELS_KEY = "chat/cloud_models"
CLOUD_MODELS_AT_KEY = "chat/cloud_models_fetched_at"
_ORG_APP = [("llmBuddy", "llmBuddy"), ("LLMDesktop", "LLMDesktop")]
_ISOLATED_KEYS = (CUSTOM_KEY, LEGACY_KEY, CLOUD_KEY,
                  CLOUD_MODELS_KEY, CLOUD_MODELS_AT_KEY)


@pytest.fixture(autouse=True)
def _isolate_settings():
    """保存 -> 清空 -> 测试 -> 恢复，隔离 QSettings，不污染真实配置。"""
    saved = []
    for org, app_name in _ORG_APP:
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
    assert any("在线模型" in t for t in texts)


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


# --------------------------------------------------------------------------- #
# 10) 云端路由：线上免费模型直连 SiliconFlow（复用 chat_stream_custom）
# --------------------------------------------------------------------------- #
def _cloud_routing_api():
    calls = {}

    class _Api:
        def pool(self):
            return {"loaded": []}

        def list_models(self):
            return []

        def chat_stream_custom(self, entry, messages, **kw):
            calls["entry"] = entry
            calls["messages"] = list(messages)
            kw["on_done"]({"text": "hi", "finish": {}, "cancelled": False})
            return {"text": "hi", "finish": {}, "cancelled": False}

    return _Api(), calls


def test_cloud_model_with_existing_key_routes_directly():
    """已配置云端 Key：直接走 chat_stream_custom，不弹框。"""
    from ui.pages.chat_page import ChatPage, SILICONFLOW_BASE_URL, CLOUD_FREE_MODELS

    _set_llmbuddy(CLOUD_KEY, "sk-cloud")
    api, calls = _cloud_routing_api()
    p = ChatPage(api, None, None)

    name = CLOUD_FREE_MODELS[0]["name"]
    p._on_pick_model(name)
    p._input.setPlainText("你好")
    p._on_send()
    assert _spin(until=lambda: p._busy is False), "云端模型发送未在限时内完成"

    assert calls["entry"]["base_url"] == SILICONFLOW_BASE_URL
    assert calls["entry"]["api_key"] == "sk-cloud"
    assert calls["entry"]["model"] == name
    # 合成 entry 为局部变量，不污染 _current_custom（保持选中态语义干净）
    assert p._current_custom is None


def test_cloud_model_missing_key_prompts_then_sends(monkeypatch):
    """未配置 Key：弹 CloudKeyDialog 保存后带 Key 继续本次发送。"""
    from ui.pages.chat_page import ChatPage, CloudKeyDialog, CLOUD_FREE_MODELS

    # 模拟用户在对话框里输入并点「保存并继续」
    def fake_exec(self):
        self._saved_key = "sk-new"
        return True

    monkeypatch.setattr(CloudKeyDialog, "exec", fake_exec)
    api, calls = _cloud_routing_api()
    p = ChatPage(api, None, None)

    name = CLOUD_FREE_MODELS[0]["name"]
    p._on_pick_model(name)
    p._input.setPlainText("你好")
    p._on_send()
    assert _spin(until=lambda: p._busy is False), "云端模型发送未在限时内完成"

    assert calls["entry"]["api_key"] == "sk-new"
    # Key 已写入 QSettings，下次直接复用
    assert p._cloud_api_key() == "sk-new"


def test_cloud_model_cancel_dialog_does_not_send(monkeypatch):
    """取消 Key 对话框：不发送、不卡死、不改动选中状态。"""
    from ui.pages.chat_page import ChatPage, CloudKeyDialog, CLOUD_FREE_MODELS

    monkeypatch.setattr(CloudKeyDialog, "exec", lambda self: False)
    api, calls = _cloud_routing_api()
    p = ChatPage(api, None, None)

    name = CLOUD_FREE_MODELS[0]["name"]
    p._on_pick_model(name)
    p._input.setPlainText("你好")
    p._on_send()

    assert p._busy is False
    assert "entry" not in calls
    assert p._cloud_api_key() == ""


def test_cloud_key_persist_and_read():
    """_set_cloud_api_key / _cloud_api_key 读写往返。"""
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    assert p._cloud_api_key() == ""
    p._set_cloud_api_key("sk-xyz")
    assert p._cloud_api_key() == "sk-xyz"


def test_menu_cloud_section_header():
    """菜单云端段标题为「在线模型 · SiliconFlow」。"""
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions()]
    assert any("在线模型 · SiliconFlow" in t for t in texts)


def test_cloud_key_dialog_validation():
    """CloudKeyDialog：空 Key 保存被拦，填写后可取回。"""
    from ui.pages.chat_page import CloudKeyDialog

    dlg = CloudKeyDialog(current_key="")
    dlg._on_save()
    assert dlg.result() != QDialog.DialogCode.Accepted
    assert dlg._error_label.text() != ""

    dlg._key_edit.setText("sk-abc")
    dlg._on_save()
    assert dlg.saved_key() == "sk-abc"


def test_menu_has_configure_cloud_key_entry():
    """菜单含「配置 SiliconFlow API Key」入口（支持重配已保存的 Key）。"""
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions()]
    assert any("配置 SiliconFlow API Key" in t for t in texts)


def test_reconfigure_existing_cloud_key(monkeypatch):
    """已保存 Key 仍可通过菜单入口重配（问题 A 修复）。"""
    from ui.pages.chat_page import ChatPage, CloudKeyDialog

    _set_llmbuddy(CLOUD_KEY, "sk-old")
    monkeypatch.setattr(CloudKeyDialog, "exec",
                        lambda self: (setattr(self, "_saved_key", "sk-new")
                                      or True))
    p = ChatPage(_FakeApi(), None, None)
    p._on_configure_cloud_key()
    assert p._cloud_api_key() == "sk-new"


# --------------------------------------------------------------------------- #
# 11) 获取最新模型列表：fetch_cloud_models（fake httpx GET，不发真实网络）
# --------------------------------------------------------------------------- #
def _patch_httpx_get(monkeypatch, resp):
    """用记录型 fake httpx.Client 替换 GET 客户端（chat_page 内的 httpx 引用）。"""
    from ui.pages import chat_page as cp

    calls = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            calls["kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, **kw):
            calls["url"] = url
            return resp

    monkeypatch.setattr(cp.httpx, "Client", _FakeClient)
    return calls


class _FakeGetResp:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def test_fetch_cloud_models_parses_tags_and_orders_free_first(monkeypatch):
    from ui.pages.chat_page import (fetch_cloud_models,
                                    SILICONFLOW_BASE_URL)

    payload = json.dumps({
        "object": "list",
        "data": [
            {"id": "paid-model/Pro", "object": "model"},
            {"id": "Qwen/Qwen3-8B", "object": "model"},
            {"id": "Qwen/Qwen2.5-7B-Instruct", "object": "model"},
            {"id": "Qwen/Qwen3-8B", "object": "model"},   # 重复
            {"id": "", "object": "model"},                 # 脏数据
            "not-a-dict",
        ],
    })
    calls = _patch_httpx_get(monkeypatch, _FakeGetResp(200, payload))

    models = fetch_cloud_models("sk-1")
    assert calls["kwargs"].get("trust_env") is False
    assert calls["kwargs"]["headers"]["Authorization"] == "Bearer sk-1"
    assert calls["url"] == f"{SILICONFLOW_BASE_URL}/models?sub_type=chat"

    names = [m["name"] for m in models]
    assert names == ["Qwen/Qwen3-8B", "Qwen/Qwen2.5-7B-Instruct", "paid-model/Pro"]
    assert models[0]["tag"] == "免费"
    assert models[2]["tag"] == "SiliconFlow"


def test_fetch_cloud_models_error_paths(monkeypatch):
    from ui.pages.chat_page import fetch_cloud_models

    # 未配置 Key：直接抛，不发请求
    with pytest.raises(RuntimeError):
        fetch_cloud_models("")

    # HTTP 401：带状态码抛出
    _patch_httpx_get(monkeypatch, _FakeGetResp(401, "invalid token"))
    with pytest.raises(RuntimeError, match="401"):
        fetch_cloud_models("sk-1")

    # 响应非 JSON
    _patch_httpx_get(monkeypatch, _FakeGetResp(200, "<html>oops</html>"))
    with pytest.raises(RuntimeError, match="解析失败"):
        fetch_cloud_models("sk-1")

    # 空列表
    _patch_httpx_get(monkeypatch, _FakeGetResp(200, '{"object":"list","data":[]}'))
    with pytest.raises(RuntimeError, match="未返回"):
        fetch_cloud_models("sk-1")


# --------------------------------------------------------------------------- #
# 12) 云端模型缓存：_cloud_models 回退 / _save_cloud_models 往返
# --------------------------------------------------------------------------- #
def test_cloud_models_fallback_to_builtin():
    from ui.pages.chat_page import ChatPage, CLOUD_FREE_MODELS

    p = ChatPage(_FakeApi(), None, None)
    assert p._cloud_models() == CLOUD_FREE_MODELS


def test_cloud_models_roundtrip():
    from ui.pages.chat_page import ChatPage, CLOUD_FREE_MODELS

    p = ChatPage(_FakeApi(), None, None)
    assert p._cloud_models() == CLOUD_FREE_MODELS
    stored = [
        {"name": "Qwen/Qwen3-8B", "tag": "免费"},
        {"name": "vendor/NewModel", "tag": "SiliconFlow"},
    ]
    p._save_cloud_models(stored)
    assert p._cloud_models() == stored
    # 拉取时间已记录
    assert _get_llmbuddy(CLOUD_MODELS_AT_KEY) is not None


def test_cloud_models_cache_survives_dirty_json():
    """缓存 JSON 损坏时回退内置清单，不崩溃。"""
    from ui.pages.chat_page import ChatPage

    _set_llmbuddy(CLOUD_MODELS_KEY, "{not-json")
    p = ChatPage(_FakeApi(), None, None)
    from ui.pages.chat_page import CLOUD_FREE_MODELS
    assert p._cloud_models() == CLOUD_FREE_MODELS


# --------------------------------------------------------------------------- #
# 13) 菜单：获取入口 + 免费置顶 + 其它模型子菜单
# --------------------------------------------------------------------------- #
def test_menu_has_fetch_entry_and_free_first(monkeypatch):
    from ui.pages.chat_page import ChatPage

    p = ChatPage(_FakeApi(), None, None)
    p._save_cloud_models([
        {"name": "vendor/Paid1", "tag": "SiliconFlow"},
        {"name": "Qwen/Qwen3-8B", "tag": "免费"},
        {"name": "vendor/Paid2", "tag": "SiliconFlow"},
    ])
    p._build_model_menu()
    texts = [a.text() for a in p._model_menu.actions()]
    assert any("获取最新模型列表" in t for t in texts)
    # 免费模型置顶：第一个云端项就是 Qwen/Qwen3-8B
    cloud_idx = [i for i, t in enumerate(texts) if "Qwen/Qwen3-8B" in t]
    assert cloud_idx, "免费模型未出现在顶层菜单"
    # 付费模型收进子菜单
    subs = [a.menu() for a in p._model_menu.actions() if a.menu() is not None]
    sub_texts = [t for m in subs for t in (a.text() for a in m.actions())]
    assert any("其它模型" in t for t in texts)
    assert any("vendor/Paid1" in t for t in sub_texts)
    assert not any("vendor/Paid1" in t for t in texts)


def test_routing_uses_dynamic_cloud_cache():
    """只在缓存里（不在内置清单）的模型也能走云端路由。"""
    from ui.pages.chat_page import ChatPage, SILICONFLOW_BASE_URL, CLOUD_FREE_MODELS

    fresh = "vendor/BrandNew-Free"
    assert fresh not in [c["name"] for c in CLOUD_FREE_MODELS]

    calls = {}

    class _Api(_FakeApi):
        def chat_stream_custom(self, entry, messages, **kw):
            calls["entry"] = entry
            kw["on_done"]({"text": "ok", "finish": {}, "cancelled": False})
            return {"text": "ok", "finish": {}, "cancelled": False}

    p = ChatPage(_Api(), None, None)
    p._save_cloud_models([{"name": fresh, "tag": "免费"}])
    p._set_cloud_api_key("sk-x")
    p._on_pick_model(fresh)
    p._input.setPlainText("你好")
    p._on_send()
    assert _spin(until=lambda: p._busy is False)
    assert calls["entry"]["base_url"] == SILICONFLOW_BASE_URL
    assert calls["entry"]["model"] == fresh


def test_fetch_action_updates_cache(monkeypatch):
    """菜单动作 _on_fetch_cloud_models：弹进度框->后台拉取->写缓存->重建菜单。"""
    from ui.pages import chat_page as cp
    from ui.pages.chat_page import ChatPage, CLOUD_MODELS_KEY

    payload = json.dumps({"object": "list", "data": [
        {"id": "Qwen/Qwen3-8B", "object": "model"},
        {"id": "vendor/Paid1", "object": "model"},
    ]})
    _patch_httpx_get(monkeypatch, _FakeGetResp(200, payload))
    # 进度框改为非阻塞（offscreen 下 exec 会永久阻塞事件循环）
    monkeypatch.setattr(cp.ProgressDialog, "exec", lambda self: True)

    _set_llmbuddy(CLOUD_KEY, "sk-1")
    p = ChatPage(_FakeApi(), None, None)
    p._on_fetch_cloud_models()
    assert _spin(until=lambda: p._settings.value(CLOUD_MODELS_KEY) is not None), \
        "拉取后未写入缓存"

    names = [m["name"] for m in p._cloud_models()]
    assert names == ["Qwen/Qwen3-8B", "vendor/Paid1"]
    # 菜单已按新列表重建
    texts = [a.text() for a in p._model_menu.actions()]
    assert any("Qwen/Qwen3-8B" in t for t in texts)
