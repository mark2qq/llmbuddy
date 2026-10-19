# -*- coding: utf-8 -*-
"""
错误路径回归测试（test_errors.py）。

验证关键错误路径不会返回 500（崩溃），而是以明确的业务错误码 / 状态码返回：
  - 对未注册模型 load -> 1001 / 404
  - 对未知下载任务查询 -> not_found（code 0，data.status="not_found"）
  - load 缺 model 字段 -> 400（参数错误）
均为轻量请求，不触发引擎加载。
"""

import pytest

UNREGISTERED = "no-such-model-xyz"
UNKNOWN_TASK = "no-such-download-task-xyz"


# ---------------------------------------------------------------------------
# 错误路径 1：未注册模型 load -> 1001 / 404
# ---------------------------------------------------------------------------
def test_load_unregistered_returns_1001(client):
    r = client.post("/api/v1/pool/load", json={"model": UNREGISTERED}, timeout=15)
    assert r.status_code == 404, r.text
    assert r.status_code != 500
    body = r.json()
    assert body["code"] == 1001, body
    assert "not registered" in body["message"].lower()


# ---------------------------------------------------------------------------
# 错误路径 2：未知下载任务查询 -> not_found
# ---------------------------------------------------------------------------
def test_query_unknown_download_task_not_found(client):
    r = client.get(f"/api/v1/models/pull/{UNKNOWN_TASK}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == 0, body
    assert body["data"]["status"] == "not_found", body


# ---------------------------------------------------------------------------
# 错误路径 3：load 缺 model 字段 -> 400
# ---------------------------------------------------------------------------
def test_load_missing_model_field_returns_400(client):
    r = client.post("/api/v1/pool/load", json={}, timeout=15)
    assert r.status_code == 400, r.text
    assert r.status_code != 500
    body = r.json()
    assert body["code"] == 1001, body
    assert "model required" in body["message"].lower()
