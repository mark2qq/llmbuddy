# -*- coding: utf-8 -*-
"""
第十一轮 GUI 改版无头测试。

覆盖：
    - 图1：已加载模型行「启动/停止」合并为单个切换按钮（不并排出现）：
            运行中显示「停止」、暂停后显示「启动」、加载中置灰
    - 图2：API 页可用模型列表默认显示已加载模型（置顶 + 默认选中 + 空提示）
    - 核心修复：用户生成过 API Key 后网关对 /v1/* 启用鉴权（401），
            客户端自动改用已生成 Key 重试（list_models / chat_stream），
            修复「API 页模型列表变空、对话发不出去」
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

import json  # noqa: E402

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    """模拟后端：1 运行中 + 1 已下载。"""

    def pool(self):
        return {"loaded": [{"name": "qwen3-4b", "status": "ready"}]}

    def list_models(self):
        return [{"id": "qwen3-4b"}, {"id": "deepseek-7b"}]

    def list_keys(self):
        return []

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
    """驱动 Qt 事件循环处理子线程跨线程信号（条件等待，避免竞态）。"""
    import time
    deadline = time.time() + timeout
    while True:
        app.processEvents()
        if until is not None and until():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.005)


def _wait_models_applied(p, timeout=5.0):
    """等待 ApiPage.refresh() 的后台模型聚合完成（刷新已异步化：16 轮）。"""
    ok = _spin(until=lambda: p._models.count() >= 2, timeout=timeout)
    assert ok, "refresh() 后台模型聚合未在限时内完成"


# ---------------------------------------------------------------------------
# 图1：启动/停止合并为单个切换按钮
# ---------------------------------------------------------------------------
def test_row_toggle_button_not_side_by_side():
    """红字：启动和停止不要同时出现——每行只有一个切换按钮。"""
    from ui.pages.dashboard_page import DashboardPage

    p = DashboardPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    p._render_loaded([
        {"name": "qwen3-4b", "status": "ready", "progress": 1.0},
        {"name": "deepseek-7b", "status": "paused", "progress": 1.0},
    ])
    # 每张卡片只有 2 个操作按钮：切换 + 取消加载（无并排的启动+停止）
    for name, expect_text, expect_obj in (
            ("qwen3-4b", "停止", "RowDangerBtn"),
            ("deepseek-7b", "启动", "RowBtn")):
        card, toggle, cancel, loading = p._row_widgets[name]
        assert toggle.text() == expect_text, f"{name} 应显示「{expect_text}」"
        assert toggle.objectName() == expect_obj
        assert toggle.isEnabled()
        assert cancel.text() == "取消加载"
        assert len(card.findChildren(QPushButton)) == 2, \
            "启动/停止按钮不应同时出现"
    # 同一个切换按钮：运行中=停止，暂停后=启动（文案互斥）
    assert p._row_widgets["qwen3-4b"][1].text() == "停止"
    assert p._row_widgets["deepseek-7b"][1].text() == "启动"


def test_row_toggle_loading_disabled():
    """加载中：切换按钮置灰显示「加载中…」，仅可取消加载。"""
    from ui.pages.dashboard_page import DashboardPage

    p = DashboardPage(_FakeApi(), _FakeLauncher(), _FakeConfig())
    p._render_loaded([{"name": "m1", "status": "loading", "progress": 0.5}])
    _card, toggle, cancel, loading = p._row_widgets["m1"]
    assert "加载中" in toggle.text()
    assert not toggle.isEnabled()
    assert cancel.isEnabled()
    # 置灰->恢复时，加载中的行切换按钮应保持不可用
    p._set_row_enabled("m1", True)
    assert not toggle.isEnabled()
    assert cancel.isEnabled()


def test_row_toggle_click_routes_to_pause_and_start():
    """切换按钮点击路由：运行中->暂停；暂停->恢复运行。"""
    from ui.pages.dashboard_page import DashboardPage

    class _RecApi(_FakeApi):
        def __init__(self):
            self.calls = []

        def model_status(self, name):
            return {"status": "paused" if name == "deepseek-7b" else "ready"}

        def pause_model(self, name):
            self.calls.append(("pause", name))
            return {"ok": True}

        def resume_model(self, name):
            self.calls.append(("resume", name))
            return {"status": "ready"}

        def load_model(self, name, **kw):
            self.calls.append(("load", name))
            return {"status": "ready"}

    api = _RecApi()
    p = DashboardPage(api, _FakeLauncher(), _FakeConfig())
    p._render_loaded([
        {"name": "qwen3-4b", "status": "ready", "progress": 1.0},
        {"name": "deepseek-7b", "status": "paused", "progress": 1.0},
    ])
    # 运行中的行：切换按钮=停止 -> pause_model
    p._row_widgets["qwen3-4b"][1].click()
    _spin(until=lambda: any(c[0] == "pause" for c in api.calls))
    assert ("pause", "qwen3-4b") in api.calls
    # 暂停的行：切换按钮=启动 -> resume_model（已加载未运行则恢复）
    p._row_widgets["deepseek-7b"][1].click()
    _spin(until=lambda: any(c[0] == "resume" for c in api.calls))
    assert ("resume", "deepseek-7b") in api.calls
    p._stop_poll()  # 停掉启动路径的轮询定时器


# ---------------------------------------------------------------------------
# 图2：API 页默认显示已加载模型
# ---------------------------------------------------------------------------
def test_api_page_default_selects_loaded():
    """红字：要默认显示已经加载的所有模型——置顶 + 默认选中。"""
    from ui.pages.api_page import ApiPage

    class _Api(_FakeApi):
        def list_models(self):
            return [
                {"id": "qwen2.5-7b-instruct", "loaded": False},
                {"id": "qwen3-4b", "loaded": True},
                {"id": "deepseek-7b", "loaded": False},
            ]

        def catalog(self, force=False, keyword=""):
            # 三个都视为「本程序已下载」，避免被本地过滤剔除
            return {"items": [
                {"name": "qwen3-4b", "local": True},
                {"name": "qwen2.5-7b-instruct", "local": True},
                {"name": "deepseek-7b", "local": True},
            ], "cached": True}

    p = ApiPage(_Api(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    # 第 0 项是「全局 Key」入口，模型项共 3 个（合计 4 项）
    assert p._models.count() == 4
    # 已加载模型排在模型项的第一行（全局项之后）
    assert "qwen3-4b" in p._models.item(1).text()
    assert "● 已加载" in p._models.item(1).text()
    # 默认选中第一个已加载模型（而非全局项/未加载模型）
    assert p._selected_model == "qwen3-4b"
    cur = p._models.currentItem()
    assert cur is not None and cur.data(Qt.ItemDataRole.UserRole) == "qwen3-4b"
    # 其余模型仍在列表中（全部可见，只是置顶；第 0 项为全局入口）
    ids = [p._models.item(i).data(Qt.ItemDataRole.UserRole)
           for i in range(1, p._models.count())]
    assert set(ids) == {"qwen2.5-7b-instruct", "qwen3-4b", "deepseek-7b"}


def test_api_page_empty_shows_hint():
    """没有已下载模型时给出可操作提示（全局 Key 入口仍在）。"""
    from ui.pages.api_page import ApiPage

    class _Api(_FakeApi):
        def list_models(self):
            return []

        def pool(self):
            return {"loaded": []}

    p = ApiPage(_Api(), None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    # 第 0 项全局入口 + 1 条提示项
    assert p._models.count() == 2
    assert "全局 Key" in p._models.item(0).text()
    assert "暂无已下载模型" in p._models.item(1).text()
    # 自动选中全局项 -> 不绑定任何模型（空串）
    assert not p._selected_model


# ---------------------------------------------------------------------------
# 核心修复：401 自动改用已生成 Key 重试
# ---------------------------------------------------------------------------
class _Resp:
    """最小 httpx.Response 替身（status_code/text/json）。"""

    def __init__(self, code, body):
        self.status_code = code
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        return self._body


class _StreamResp:
    """最小流式响应替身（上下文管理器 + iter_lines）。

    忠实模拟 httpx 语义：流式响应未调用 read() 前访问 .text/.json() 抛
    ResponseNotRead（历史 bug：错误路径忘读 -> 掩盖真实状态码）。
    """

    def __init__(self, code, lines, body_text=None):
        self.status_code = code
        self._lines = lines
        self._read = False
        self._text = body_text if body_text is not None else (
            "unauthorized" if code >= 400 else "")

    def read(self):
        self._read = True
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        return iter(self._lines)

    @property
    def text(self):
        if not self._read:
            import httpx
            raise httpx.ResponseNotRead(
                "Attempted to access streaming response content, "
                "without having called `read()`.")
        return self._text


class _KeyedHttpxClient:
    """按 Authorization 头模拟网关鉴权的 httpx.Client 替身。

    - /api/v1/keys（管理接口，无鉴权）返回一条全局生成 Key
    - /v1/models、/v1/chat/completions：无 Key -> 401；带生成 Key -> 200
    """

    def __init__(self, **kw):
        self._headers = kw.get("headers") or {}
        _KeyedHttpxClient.stats["clients"] += 1

    stats = {"clients": 0, "models_noauth": 0, "models_auth": 0,
             "chat_noauth": 0, "chat_auth": 0, "keys": 0}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, path, params=None):
        auth = self._headers.get("Authorization", "")
        if path == "/api/v1/keys":
            _KeyedHttpxClient.stats["keys"] += 1
            return _Resp(200, {"code": 0, "data": {"keys": [
                {"key": "gen-key-1", "model": "", "created_at": "2026-10-19T00:00:00Z"},
            ]}})
        if path == "/v1/models":
            if auth == "Bearer gen-key-1":
                _KeyedHttpxClient.stats["models_auth"] += 1
                return _Resp(200, {"object": "list",
                                   "data": [{"id": "qwen3-4b", "loaded": True}]})
            _KeyedHttpxClient.stats["models_noauth"] += 1
            return _Resp(401, {"detail": "Invalid or missing API key"})
        return _Resp(404, {"detail": "not found"})

    def stream(self, method, url, **kw):
        auth = self._headers.get("Authorization", "")
        if auth == "Bearer gen-key-1":
            _KeyedHttpxClient.stats["chat_auth"] += 1
            return _StreamResp(200, [
                'data: {"choices":[{"delta":{"content":"你"}}]}',
                'data: {"choices":[{"delta":{"content":"好"}}]}',
                "data: [DONE]",
            ])
        _KeyedHttpxClient.stats["chat_noauth"] += 1
        return _StreamResp(401, [])


def test_list_models_401_retries_with_generated_key(monkeypatch):
    """生成过 Key 后 /v1/models 401 -> 自动带已生成 Key 重试（API 页列表不空）。"""
    import httpx
    from core.api_client import ApiClient

    _KeyedHttpxClient.stats.update({"clients": 0, "models_noauth": 0,
                                    "models_auth": 0, "chat_noauth": 0,
                                    "chat_auth": 0, "keys": 0})
    monkeypatch.setattr(httpx, "Client", _KeyedHttpxClient)

    api = ApiClient(api_key="")          # config 未配 Key（历史默认）
    models = api.list_models()
    assert [m["id"] for m in models] == ["qwen3-4b"], \
        "401 后应改用已生成 Key 重试并拿到模型列表"
    st = _KeyedHttpxClient.stats
    assert st["models_noauth"] == 1 and st["models_auth"] == 1
    assert st["keys"] == 1, "仅在 401 后才查询 keys.json（开放态不额外请求）"


def test_chat_stream_401_retries_with_generated_key(monkeypatch):
    """生成过 Key 后对话 401 -> 自动带已生成 Key 重试（对话不再发不出）。"""
    import httpx
    from core.api_client import ApiClient

    _KeyedHttpxClient.stats.update({"clients": 0, "models_noauth": 0,
                                    "models_auth": 0, "chat_noauth": 0,
                                    "chat_auth": 0, "keys": 0})
    monkeypatch.setattr(httpx, "Client", _KeyedHttpxClient)

    api = ApiClient(api_key="")
    chunks = []
    res = api.chat_stream([{"role": "user", "content": "hi"}], "qwen3-4b",
                          on_chunk=chunks.append)
    assert res["text"] == "你好", "重试后应正常拿到流式输出"
    assert chunks == ["你", "好"]
    st = _KeyedHttpxClient.stats
    assert st["chat_noauth"] == 1 and st["chat_auth"] == 1
    assert st["keys"] == 1


def test_chat_stream_error_status_surfaces_real_error(monkeypatch):
    """错误状态码必须透出真实 HTTP 错误（红字：ResponseNotRead 掩盖了真相）。

    流式响应未 read() 就访问 .text 会抛 ResponseNotRead，导致：
      ① 用户看到晦涩的 httpx 内部错误而非真实原因；
      ② 401 被掩盖 -> 自动重试逻辑被绕过。
    修复：错误路径先 resp.read() 再取 text。
    """
    import httpx
    from core.api_client import ApiClient, ApiError

    class _ErrClient:
        """任何请求都返回 503（带真实错误体）。"""

        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            return _Resp(200, {"code": 0, "data": {"keys": []}})

        def stream(self, method, url, **kw):
            return _StreamResp(503, [], body_text='{"detail":"engine not ready"}')

    monkeypatch.setattr(httpx, "Client", _ErrClient)
    api = ApiClient(api_key="")
    try:
        api.chat_stream([{"role": "user", "content": "hi"}], "qwen3-4b",
                        on_chunk=lambda _p: None)
    except ApiError as e:
        assert e.code == 503, f"应透出真实状态码 503，实际 {e.code}"
        assert "engine not ready" in e.message, "应透出真实错误体"
    else:
        raise AssertionError("503 应抛出 ApiError")


def test_gateway_calls_single_attempt_when_open(monkeypatch):
    """开放态（无任何 Key）：不触发重试与 keys 查询（快路径不受影响）。"""
    import httpx
    from core.api_client import ApiClient

    class _OpenClient:
        def __init__(self, **kw):
            self._headers = kw.get("headers") or {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, path, params=None):
            assert path != "/api/v1/keys", "开放态不应查询 keys"
            return _Resp(200, {"object": "list", "data": [{"id": "m1"}]})

        def stream(self, method, url, **kw):
            return _StreamResp(200, [
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                "data: [DONE]",
            ])

    monkeypatch.setattr(httpx, "Client", _OpenClient)
    api = ApiClient(api_key="")
    assert [m["id"] for m in api.list_models()] == ["m1"]
    res = api.chat_stream([{"role": "user", "content": "hi"}], "m1",
                          on_chunk=lambda _p: None)
    assert res["text"] == "ok"
