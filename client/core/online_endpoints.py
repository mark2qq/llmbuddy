# -*- coding: utf-8 -*-
"""在线「免 Key」端点检测（client/core/online_endpoints.py）。

背景（1.0.12 用户需求）：菜单原来叫「检测本机免 Key 端点…」，探的是本机
Ollama / LM Studio —— 没装这两个的用户点了一点反应都没有，等于没有功能。
改成「检测在线免 Key 端点…」：**验证真的能不填 Key 就对话的在线服务**。

判定口径（三道探针，缺一不可）：

    ① `GET {root}/models`（或候选自带的 models_url）
       —— 连不通 / 超时 → `unreachable`；401/403 → `auth`（要 Key，不算免 Key）
    ② `POST {root}/chat/completions`（8 tokens 的 "ping"）
       —— 200 且能解析出回复 → `ok`；401/403 → `auth`；其它 → `error`
    ③ 记录是否支持 `tools`（function calling）：助理要用工具，只有纯文本的
       端点能聊天但干不了活，界面上要标出来

**只看第 ① 步是不够的**：实测 `api.airforce` 的 `/models` 公开可读（783 个
模型），但 chat 接口返回 `401 Missing Authorization` —— 只看 /models 会把
「要 Key 的服务」当成免 Key 接进来，用户配好后每轮都报鉴权失败。

候选清单：
    · 内置（`BUILTIN`）：目前只有 Pollinations（截至 2026-10 实测可用）；
    · 已配置为免 Key 的提供商（`keyless: true`）也会被重新验证 —— 用户自己
      加的端点同样能验，避免"标了免 Key 其实早就收费了"。
    想扩充内置清单：往 `BUILTIN` 加一条即可（id/label/root/模型名）。

代理：外网请求走系统代理，但**死代理自动降级直连**（复用
`core.assistant.providers.env_proxy_usable`，与本机代理探测同一套判断）。
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: (id, 显示名, OpenAI 兼容根地址, 探测用模型名, models 地址, 备注)
#: root 结尾不要带 /v1：本模块按 OpenAI 习惯自己拼 /models、/chat/completions。
BUILTIN: Tuple[Tuple[str, str, str, str, str, str], ...] = (
    ("pollinations", "Pollinations", "https://text.pollinations.ai/openai",
     "openai-fast", "https://text.pollinations.ai/models",
     "免费公益端点，无需任何 Key；匿名档可用 openai-fast"),
)

#: 探针超时（秒）：在线服务首包偶尔慢，太短会误判成不可达
MODEL_TIMEOUT = 8.0
CHAT_TIMEOUT = 20.0
#: 并发数：候选通常只有个位数，4 路足够且不会把免费端点打限流
MAX_WORKERS = 4

STATUS_TEXT = {
    "ok": "可用",
    "auth": "需要 Key",
    "unreachable": "连不上",
    "error": "接口报错",
    "empty": "没有可用模型",
}


def _trust_env() -> bool:
    """外网请求是否走系统代理（死代理就直连，别把请求打进空端口）。"""
    try:
        from core.assistant.providers import env_proxy_usable
        return bool(env_proxy_usable())
    except Exception:
        return True


def _client(timeout: float):
    import httpx
    return httpx.Client(trust_env=_trust_env(), timeout=timeout,
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json"})


def _parse_models(raw: Any) -> List[str]:
    """从 /models 的响应里抽模型名，兼容三种常见形状。"""
    data = raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return []
    names: List[str] = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
        if isinstance(items, dict):
            items = list(items.keys())
        for it in items if isinstance(items, list) else []:
            if isinstance(it, str):
                names.append(it)
            elif isinstance(it, dict):
                nm = (it.get("id") or it.get("name") or it.get("model"))
                if nm:
                    names.append(str(nm))
    elif isinstance(data, list):
        for it in data:
            if isinstance(it, str):
                names.append(it)
            elif isinstance(it, dict):
                nm = (it.get("id") or it.get("name") or it.get("model"))
                if nm:
                    names.append(str(nm))
    out: List[str] = []
    for n in names:
        n = n.strip()
        if n and n not in out:
            out.append(n)
    return out


def _models_url(root: str, override: str = "") -> str:
    if override:
        return override
    base = root.rstrip("/")
    return base + "/models"


def _declares_tools(raw: Any, model: str) -> bool:
    """models 响应里该模型是否声明支持 function calling（`"tools": true`）。

    Pollinations 的 `/models` 会带 `{"name": "openai-fast", "tools": true}`；
    OpenAI 官方格式不带这个字段。判不出来就当不支持（宁可不吹）。
    """
    data = raw
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return False
    items: Any = []
    if isinstance(data, dict):
        items = data.get("data") or []
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return False
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("id") or it.get("name") or "")
        if model and name and name != model:
            continue
        if it.get("tools") is True:
            return True
    return False


def _chat_url(root: str) -> str:
    base = root.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _probe_chat(root: str, model: str) -> Dict[str, Any]:
    """最小对话探针：8 tokens 的 ping，只看「能不能在没有 Key 的情况下回话」。"""
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}],
               "max_tokens": 8, "stream": False}
    try:
        with _client(CHAT_TIMEOUT) as c:
            r = c.post(_chat_url(root), json=payload)
    except Exception as e:                                  # noqa: BLE001
        return {"status": "unreachable", "detail": f"{type(e).__name__}: {e}"[:200]}
    if r.status_code in (401, 403):
        return {"status": "auth",
                "detail": f"HTTP {r.status_code}（需要 Key）{(r.text or '')[:120]}"}
    if r.status_code >= 400:
        return {"status": "error",
                "detail": f"HTTP {r.status_code} {(r.text or '')[:160]}"}
    body = r.text or ""
    reply = ""
    try:
        obj = json.loads(body)
        ch = (obj.get("choices") or [{}])[0] if isinstance(obj, dict) else {}
        msg = (ch.get("message") or {}) if isinstance(ch, dict) else {}
        reply = str(msg.get("content") or msg.get("reasoning_content") or "")
    except Exception:
        reply = body[:80]
    return {"status": "ok", "reply": reply[:120],
            "tools": "tools" in body.lower() or "tool_calls" in body}


def probe_one(cand: Dict[str, Any]) -> Dict[str, Any]:
    """验一个候选：models → chat。返回统一结构（含 status / detail）。"""
    root = str(cand.get("base_url") or "").rstrip("/")
    model = str(cand.get("model") or "").strip()
    t0 = time.time()
    out: Dict[str, Any] = {
        "id": cand.get("id") or root, "label": cand.get("label") or root,
        "base_url": root, "keyless": True, "source": cand.get("source") or "builtin",
        "models": [], "status": "error", "detail": "", "tools": False,
        "model": model, "latency_ms": 0,
    }
    if not root:
        out.update(status="error", detail="没有 base_url")
        return out
    models: List[str] = []
    got_models = False
    models_raw = ""
    try:
        with _client(MODEL_TIMEOUT) as c:
            r = c.get(_models_url(root, str(cand.get("models_url") or "")))
        if r.status_code in (401, 403):
            # 有的服务把 /models 也锁了：不急着判定，交给 chat 探针定论
            out["detail"] = f"/models HTTP {r.status_code}"
        elif r.status_code < 400:
            models_raw = r.text or ""
            models = _parse_models(models_raw)
            got_models = True
    except Exception as e:                                  # noqa: BLE001
        out["detail"] = f"/models 连不上：{type(e).__name__}"
    out["models"] = models

    if not model:
        model = models[0] if models else ""
    # 先看 /models 有没有声明工具能力，再用 chat 探针交叉验证
    declared_tools = _declares_tools(models_raw, model) if models_raw else False
    chat = _probe_chat(root, model) if model else {
        "status": "empty", "detail": "没有可用模型名，无法探测对话接口"}
    out["model"] = model
    out["status"] = chat.get("status") or "error"
    if chat.get("detail"):
        out["detail"] = (out["detail"] + "；" if out["detail"] else "") + chat["detail"]
    out["reply"] = chat.get("reply") or ""
    out["tools"] = bool(chat.get("tools") or declared_tools)
    out["latency_ms"] = int((time.time() - t0) * 1000)
    if out["status"] == "ok":
        out["detail"] = (f"{len(models)} 个模型" if got_models and models
                         else "对话可用") + ("·支持工具调用" if out["tools"] else "")
    return out


def candidates(providers: Optional[Iterable[Dict[str, Any]]] = None
               ) -> List[Dict[str, Any]]:
    """待探测清单：内置 + 已配置为免 Key 的提供商（按 id 去重）。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for pid, label, root, model, models_url, note in BUILTIN:
        out.append({"id": pid, "label": label, "base_url": root, "model": model,
                    "models_url": models_url, "note": note, "source": "builtin"})
        seen.add(pid)
    for p in (providers or []):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        base = str(p.get("base_url") or "").strip()
        if not pid or pid in seen or not base or not p.get("keyless"):
            continue
        if str(p.get("kind") or "") == "local" or base.startswith("http://127.0.0.1"):
            continue            # 本机端点由 local_endpoints 负责
        out.append({"id": pid, "label": p.get("label") or pid, "base_url": base,
                    "model": str(p.get("model") or ""), "models_url": "",
                    "note": "已配置为免 Key", "source": "custom"})
        seen.add(pid)
    return out


def probe(providers: Optional[Iterable[Dict[str, Any]]] = None
          ) -> List[Dict[str, Any]]:
    """并发探测全部候选，按「可用 → 需要 Key → 连不上」排序返回。

    任何异常都在单个候选内消化：探测失败只影响这一条，不会抛出。
    """
    items = candidates(providers)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(items))) as ex:
        results = list(ex.map(lambda c: probe_one(c), items))
    order = {"ok": 0, "empty": 1, "error": 2, "auth": 3, "unreachable": 4}
    results.sort(key=lambda x: (order.get(str(x.get("status")), 9),
                                x.get("latency_ms") or 0))
    return results


def summary(results: Iterable[Dict[str, Any]]) -> str:
    """一行状态文案（写状态栏 / 菜单提示）。"""
    rows = [r for r in (results or []) if isinstance(r, dict)]
    if not rows:
        return "没有可探测的端点"
    ok = [r for r in rows if r.get("status") == "ok"]
    auth = [r for r in rows if r.get("status") == "auth"]
    down = [r for r in rows if r.get("status") in ("unreachable", "empty", "error")]
    bits = []
    if ok:
        bits.append("可用 " + "、".join(
            f"{r.get('label')}（{len(r.get('models') or [])} 个模型）"
            for r in ok))
    if auth:
        bits.append(f"需要 Key {len(auth)} 个")
    if down:
        bits.append(f"不可用 {len(down)} 个")
    return "；".join(bits) if bits else "没有可用端点"
