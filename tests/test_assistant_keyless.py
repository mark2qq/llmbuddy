# -*- coding: utf-8 -*-
"""
免 Key 模型接入 与 死代理自愈 的回归测试（1.0.10）。

用户需求：「排查线上除了硅基流动，还有没有完全免费、不需要 Key、能直接对接的模型」。
结论落成代码后这里锁住三件事：

    ① 默认提供商里有「Pollinations 免 Key」，keyless=True、无需 api_key；
       本机 Ollama / LM Studio 预置为 disabled（没跑起来时不污染菜单）；
    ② 老配置（没有 keyless 字段 / 没有这些提供商）能被 load_config 自动补齐；
    ③ 环境里残留**死掉**的本机代理时，Provider 自动改直连（否则全是「转圈」）。
"""
import json
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import providers as providers_mod
from core.assistant import store as store_mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    for sub in ("skills", "sessions", "audit", "workspace"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "ASSISTANT_DIR", str(root))
    monkeypatch.setattr(store_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(store_mod, "SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(store_mod, "AUDIT_DIR", str(root / "audit"))
    monkeypatch.setattr(store_mod, "CONFIG_PATH", str(root / "config.json"))
    monkeypatch.setattr(store_mod, "DEFAULT_WORKSPACE", str(root / "workspace"))
    store_mod.ensure_dirs()
    return root


def _providers(cfg):
    return {p["id"]: p for p in cfg.get("providers") or []}


# ---------------------------------------------------------------------------
# ① 默认提供商
# ---------------------------------------------------------------------------
def test_pollinations_provider_is_keyless_and_ready(env):
    p = _providers(store_mod.load_config())["pollinations"]
    assert p["keyless"] is True
    assert p["enabled"] is True
    assert p["api_key"] == ""                       # 免 Key：不需要也不该预填
    assert p["base_url"] == store_mod.POLLINATIONS_BASE_URL
    assert p["model"] == store_mod.POLLINATIONS_MODEL == "openai-fast"
    # base_url 只到域名：Provider.endpoint() 会拼 /v1/chat/completions
    assert not p["base_url"].rstrip("/").endswith("/openai")


def test_local_keyless_providers_are_disabled_by_default(env):
    provs = _providers(store_mod.load_config())
    for pid in ("ollama", "lmstudio"):
        assert provs[pid]["keyless"] is True
        assert provs[pid]["enabled"] is False       # 没跑起来时不进菜单
        assert provs[pid]["base_url"].startswith("http://127.0.0.1:")


def test_old_config_gets_new_providers_merged(env, tmp_path):
    """老用户 config.json 里没有 pollinations → 读配置时自动补上（不丢已有字段）。"""
    old = {
        "policy": "ask_all",
        "active_provider": "siliconflow",
        "providers": [
            {"id": "local", "label": "本地模型", "kind": "local", "enabled": True,
             "base_url": "", "api_key": "", "model": ""},
            {"id": "siliconflow", "label": "硅基流动", "kind": "openai",
             "enabled": True, "base_url": "https://api.siliconflow.cn/v1",
             "api_key": "sk-keep-me", "model": "Qwen/Qwen3-8B"},
        ],
    }
    store_mod.CONFIG_PATH  # noqa: B018 - 确保 fixture 已改路径
    with open(store_mod.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False)
    cfg = store_mod.load_config()
    provs = _providers(cfg)
    assert "pollinations" in provs
    assert provs["pollinations"]["keyless"] is True
    # 老字段不被覆盖
    assert provs["siliconflow"]["api_key"] == "sk-keep-me"
    assert cfg["policy"] == "ask_all"


def test_state_payload_exposes_keyless(env):
    """state 下发 keyless，前端才能显示「免 Key」而不是「尚未配置 API Key」。"""
    from core.assistant import agent as agent_mod
    assistant = agent_mod.Assistant()
    rows = assistant.state()
    provs = {p["id"]: p for p in rows["providers"]}
    assert provs["pollinations"]["keyless"] is True
    assert provs["pollinations"]["has_key"] is False
    assert provs["local"]["keyless"] is False
    # 密钥本身绝不下发（state 只给 has_key 布尔）
    assert all("api_key" not in p for p in rows["providers"])


# ---------------------------------------------------------------------------
# ② 免 Key 提供商的请求头 / 端点
# ---------------------------------------------------------------------------
def test_keyless_provider_sends_no_authorization():
    p = providers_mod.Provider({
        "id": "pollinations", "kind": "openai", "keyless": True,
        "base_url": store_mod.POLLINATIONS_BASE_URL, "api_key": "",
        "model": "openai-fast"})
    assert p.endpoint() == "https://text.pollinations.ai/v1/chat/completions"
    assert "Authorization" not in p.headers()


# ---------------------------------------------------------------------------
# ③ 死代理自愈
# ---------------------------------------------------------------------------
def _free_port() -> int:
    """拿一个当前没人监听的端口（模拟「环境变量指向死代理」）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _only_https_proxy(monkeypatch, value: str) -> None:
    """把 HTTPS_PROXY 设成 value，并清掉大写/小写的 HTTP_PROXY。

    坑（本项目实测踩过）：**Windows 的环境变量名大小写不敏感**，
    `monkeypatch.delenv("https_proxy")` 会把刚 `setenv("HTTPS_PROXY")` 的值一起删掉，
    测试就会莫名其妙地「代理看起来没配」而假通过。所以这里绝不 delenv 同名的大小写变体。
    """
    monkeypatch.setenv("HTTPS_PROXY", value)
    monkeypatch.delenv("HTTP_PROXY", raising=False)


def test_env_proxy_dead_is_detected(monkeypatch):
    dead = f"http://127.0.0.1:{_free_port()}"
    _only_https_proxy(monkeypatch, dead)
    providers_mod._PROXY_ALIVE.clear()
    assert providers_mod.env_proxy_usable() is False


def test_env_proxy_alive_is_trusted(monkeypatch):
    """本机有个真在监听的端口 → 认为代理可用（不该被误判成死的）。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        _only_https_proxy(monkeypatch, f"http://127.0.0.1:{port}")
        providers_mod._PROXY_ALIVE.clear()
        assert providers_mod.env_proxy_usable() is True
    finally:
        srv.close()
        providers_mod._PROXY_ALIVE.clear()


def test_no_proxy_env_means_usable(monkeypatch):
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(k, raising=False)
    providers_mod._PROXY_ALIVE.clear()
    assert providers_mod.env_proxy_usable() is True


def test_remote_proxy_is_not_probed(monkeypatch):
    """远端代理不做端口探测（代价高且多半不可达），一律交给 httpx。"""
    monkeypatch.setenv("HTTPS_PROXY", "http://10.255.255.1:3128")
    providers_mod._PROXY_ALIVE.clear()
    assert providers_mod.env_proxy_usable() is True


def test_dead_proxy_falls_back_to_direct(monkeypatch):
    """死代理时 Provider 改用直连：trust_env=False。"""
    _only_https_proxy(monkeypatch, f"http://127.0.0.1:{_free_port()}")
    providers_mod._PROXY_ALIVE.clear()
    p = providers_mod.Provider({
        "id": "pollinations", "kind": "openai", "keyless": True,
        "base_url": store_mod.POLLINATIONS_BASE_URL, "model": "openai-fast"})
    with p._client(5.0) as c:
        assert c.trust_env is False
