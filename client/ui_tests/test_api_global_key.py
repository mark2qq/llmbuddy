# -*- coding: utf-8 -*-
"""
红字回归（2026-10-20）：API 页「全局 Key」项回显。

需求：全局 Key 生成后，切换到其他模型项再切回「全局 Key」项时，
下方 Key 输入框需要把已生成的全局 Key 显示出来（此前直接 return 不查询）。
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

import time  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)


class _FakeApi:
    """一个已下载模型；keys 含一条全局 Key + 一条模型绑定 Key。"""

    def list_models(self):
        return [{"id": "model-a", "loaded": True}]

    def catalog(self, force=False, keyword=""):
        return {"items": [{"name": "model-a", "local": True}], "cached": True}

    def list_keys(self):
        return [
            {"key": "GLOBALKEY123", "model": "",
             "created_at": "2026-10-20T01:00:00Z"},
            {"key": "MODELKEY456", "model": "model-a",
             "created_at": "2026-10-20T02:00:00Z"},
        ]


class _FakeConfig:
    def openai_base_url(self):
        return "http://127.0.0.1:8000/v1"


def _spin(until=None, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        app.processEvents()
        if until is not None and until():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.005)


def test_switch_back_to_global_shows_generated_key():
    from ui.pages.api_page import ApiPage

    p = ApiPage(_FakeApi(), None, _FakeConfig())
    p.refresh()
    assert _spin(until=lambda: p._models.count() >= 2), "后台模型聚合未完成"
    assert "全局 Key" in p._models.item(0).text()

    # 1) 选中模型项 -> 显示该模型绑定的 Key
    p._models.setCurrentRow(1)
    assert _spin(until=lambda: p._selected_model == "model-a")
    assert _spin(until=lambda: p._key_label.text() == "MODELKEY456"), \
        f"模型项应显示绑定 Key，实际: {p._key_label.text()!r}"

    # 2) 切回全局项 -> 回显已生成的全局 Key（红字修复点）
    p._models.setCurrentRow(0)
    assert _spin(until=lambda: p._selected_model == "")
    assert _spin(until=lambda: p._key_label.text() == "GLOBALKEY123"), \
        f"切回全局项应显示全局 Key，实际: {p._key_label.text()!r}"
    # 全局项仍不可复制模型名
    assert p._copy_model_btn.isEnabled() is False
