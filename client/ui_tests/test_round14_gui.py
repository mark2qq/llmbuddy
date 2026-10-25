# -*- coding: utf-8 -*-
"""
第十四轮测试：第三方客户端（WorkBuddy）接入相关的 Key 问题防呆。

背景：WorkBuddy 报「API Key 无效或没有权限访问该模型」，实测原因是 Key
只粘贴了 60 位（完整为 64 位）-> 网关 401；另外默认总选中模型导致只能
生成「绑定模型」的 Key，切换模型就会 403。

覆盖：
    - API 页第 0 项为「全局 Key」入口；选中后生成的是不绑定模型的 Key
    - 复制按钮显示位数反馈（便于确认没复制残缺）
    - 网关 401 提示带收到位数（自诊断）
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QListWidgetItem  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

# 测试用合成 Key（64 位十六进制，非真实凭据）
FULL_KEY = "ab12cd34" * 8


class _FakeApi:
    def __init__(self):
        self.generated = []

    def pool(self):
        return {"loaded": [{"name": "Llama-3.2-3B-Instruct-Q4_K_M",
                            "status": "ready"}]}

    def list_models(self):
        return [{"id": "Llama-3.2-3B-Instruct-Q4_K_M", "loaded": True}]

    def catalog(self, force=False, keyword=""):
        return {"items": [{"name": "Llama-3.2-3B-Instruct-Q4_K_M",
                           "local": True}], "cached": True}

    def list_keys(self):
        return [{"key": FULL_KEY, "model": "Llama-3.2-3B-Instruct-Q4_K_M",
                 "created_at": "2026-10-19T13:36:03Z"}]

    def gen_key(self, model=None):
        self.generated.append(model)
        return "f" * 64


class _FakeConfig:
    def openai_base_url(self):
        return "http://127.0.0.1:8000/v1"


def _spin(until=None, timeout=5.0):
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


def test_global_key_item_exists_and_generates_unbound(monkeypatch):
    """第 0 项为全局 Key 入口；选中后「生成」创建不绑定模型的 Key。"""
    import ui.pages.api_page as api_page_mod

    # 生成成功后弹的是模态 ApiKeyDialog（exec 会阻塞无头测试），替换为空壳。
    # 注意必须等 exec 真正返回再结束测试：Worker 的 result 信号是**异步**派发
    # 的，若 monkeypatch 还原后信号才到，_show_key 就会弹出真模态框，把整个
    # pytest 进程永久挂住（整目录跑时稳定复现，单文件跑看时序）。
    dialog_calls = []

    class _NoDialog:
        def __init__(self, *a, **k):
            dialog_calls.append(1)

        def exec(self):
            return 0

    monkeypatch.setattr(api_page_mod, "ApiKeyDialog", _NoDialog)

    from ui.pages.api_page import ApiPage

    api = _FakeApi()
    p = ApiPage(api, None, _FakeConfig())
    p.refresh()
    _wait_models_applied(p)
    first = p._models.item(0)
    assert "全局 Key" in first.text(), "第 0 项应为全局 Key 入口"
    assert first.data(Qt.ItemDataRole.UserRole) == ""

    # 默认选中已加载模型（不是全局项）
    assert p._selected_model == "Llama-3.2-3B-Instruct-Q4_K_M"

    # 切到全局项 -> 生成应调用 gen_key(None)
    p._models.setCurrentRow(0)
    _spin(until=lambda: p._selected_model == "")
    assert p._selected_model == "", "选中全局项后不应有绑定模型"
    assert "全局 Key" in p._key_model_label.text()

    p._on_generate()
    # 等 _show_key 完整跑完（含 dialog.exec 返回），而不是只等 generated——
    # generated 在 Worker 线程里先满足，_ok 弹框那一步还在主线程队列里排着。
    ok = _spin(until=lambda: dialog_calls and p._key_label.text() == "f" * 64)
    assert ok, "生成回调未在限时内完成"
    assert api.generated == [None], "全局项应生成不绑定模型的 Key"


def test_copy_button_reports_length():
    """复制按钮应提示位数（64 位），便于发现复制残缺。"""
    from ui.pages.api_page import ApiPage

    p = ApiPage(_FakeApi(), None, _FakeConfig())
    p.refresh()
    _spin(until=lambda: p._key_label.text() == FULL_KEY)
    assert p._key_label.text() == FULL_KEY, "应显示该模型绑定的完整 Key"
    p._on_copy_key()
    assert "64" in p._copy_btn.text(), "复制后应提示 64 位"
    assert QApplication.clipboard().text() == FULL_KEY, "剪贴板应为完整 Key"
