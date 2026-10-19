# -*- coding: utf-8 -*-
"""
API Key 按模型绑定测试（test_keys.py）。

覆盖：
  - M7 POST /api/v1/keys/generate 生成全局/绑定模型 Key
  - GET /api/v1/keys 列出全部 Key（含 model 字段、倒序）
  - 网关接受 keys.json 中的生成 Key（/v1/models 200）
  - 绑定模型的 Key 用于不同模型 → 403（先于模型可用性检查）
  - 垃圾 Key → 401
  - 单元：config.get_key_for_model 取最新绑定、缺失返回 None

所有测试结束后由 _restore_keys 把 cache/keys.json 还原为测试前状态
（默认不存在则删除），不影响后续测试模块。
"""
import os
import time

import pytest

import core.config as core_config_mod
from core.config import KEYS_PATH, generate_key, get_key_for_model


# ---------------------------------------------------------------------------
# 还原 keys.json：模块级自动夹具，测试前后都把 keys.json 重置为空列表（[]）。
# 这样既可保证测试期间生成的 key 不影响其他测试模块（网关回到开放态），
# 也避免依赖 os.remove（在 Windows 下被 uvicorn 子进程占用时可能失败，导致残留污染）。
# ---------------------------------------------------------------------------
def _reset_keys(data) -> None:
    """带重试地把 keys.json 重置为 data，规避 Windows 下 uvicorn 子进程短暂
    持有文件句柄导致的写入/删除失败（历史回归根因）。"""
    for _ in range(30):
        try:
            os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
            with open(KEYS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return
        except Exception:
            try:
                os.remove(KEYS_PATH)
            except Exception:
                pass
            time.sleep(0.1)


@pytest.fixture(scope="module", autouse=True)
def _clean_keys():
    _reset_keys([])   # 测试前清空，打破任何历史残留污染
    yield
    _reset_keys([])   # 测试后清空，保证网关恢复开放、不影响其他测试


def _gen(client, model=""):
    """通过 M7 生成一个 Key，返回 (api_key, model)。"""
    body = {"model": model} if model else {}
    r = client.post("/api/v1/keys/generate", json=body)
    assert r.status_code == 200, f"generate failed: {r.status_code} {r.text}"
    d = r.json()["data"]
    return d["api_key"], d.get("model", "")


def test_generate_and_list_keys(client):
    """生成全局 + 绑定模型 Key，GET /api/v1/keys 能查到且 model 字段正确、倒序。"""
    global_key, gm = _gen(client, "")           # 全局 Key（model=""）
    bound_key, bm = _gen(client, "qa-model-x")   # 绑定模型 Key
    assert global_key and gm == ""
    assert bound_key and bm == "qa-model-x"

    r = client.get("/api/v1/keys")
    assert r.status_code == 200, f"list keys failed: {r.status_code} {r.text}"
    keys = r.json()["data"]["keys"]
    by_key = {k["key"]: k for k in keys}
    assert global_key in by_key and by_key[global_key]["model"] == ""
    assert bound_key in by_key and by_key[bound_key]["model"] == "qa-model-x"
    # 倒序：最新生成的排在最前
    created = [k["created_at"] for k in keys]
    assert created == sorted(created, reverse=True), "keys 未按生成时间倒序"


def test_gateway_accepts_generated_key(client):
    """带正确（全局）Key 请求 /v1/models → 200（证明网关接受生成 Key）。"""
    global_key, _ = _gen(client, "")
    r = client.get("/v1/models",
                   headers={"Authorization": f"Bearer {global_key}"})
    assert r.status_code == 200, f"expected 200, got {r.status_code} {r.text}"
    assert "data" in r.json()


def test_bound_key_wrong_model_403(client):
    """绑定 qa-model-x 的 Key 请求 chat/completions 但 model=other-model → 403。"""
    bound_key, _ = _gen(client, "qa-model-x")
    body = {
        "model": "other-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    r = client.post("/v1/chat/completions", json=body,
                    headers={"Authorization": f"Bearer {bound_key}"})
    assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text}"
    assert "qa-model-x" in r.json()["detail"]


def test_garbage_key_401(client):
    """垃圾 Key（且 keys.json 非空、auth 已生效）→ 401。"""
    _gen(client, "")  # 确保 keys.json 非空，使网关启用鉴权
    r = client.get("/v1/models", headers={"Authorization": "Bearer garbage-key"})
    assert r.status_code == 401, f"expected 401, got {r.status_code} {r.text}"


def test_get_key_for_model_unit(tmp_path, monkeypatch):
    """单元层面验证 get_key_for_model：取最新绑定、缺失返回 None、空模型返回 None。"""
    fake = tmp_path / "keys.json"
    monkeypatch.setattr(core_config_mod, "KEYS_PATH", str(fake))
    # 清掉可能存在的真实文件影响（已用 monkeypatch 指向 tmp）
    k1 = generate_key("qa-model-x")
    k2 = generate_key("qa-model-x")   # 后生成的应为「最新」
    assert get_key_for_model("qa-model-x") == k2
    assert get_key_for_model("nonexistent") is None
    assert get_key_for_model("") is None
