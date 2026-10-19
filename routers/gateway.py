# -*- coding: utf-8 -*-
"""
OpenAI 兼容网关（routers/gateway.py）。

保留并改造原 serve.py 的 /health、/v1/models、/v1/chat/completions、/v1/completions：
    - /v1/chat|completions 不再依赖全局 switch_model，而是先 pool.load(model) 确保加载，
      再按 model 名经 pool.route(model) 路由到对应引擎端口；SSE 透传并改写 model 字段。
    - 沿用 trim_messages / _normalize / _rewrite_sse_line 上下文裁剪与归一化逻辑。
"""

import os
import json
import time
import asyncio
import logging
from typing import Optional, Dict, Any, List

import httpx
from fastapi import APIRouter, Request, Header, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import HTTPException

from core.pool import get_model_pool, ModelPoolError
from core.config import load_registry, list_keys
from core.system import detect_system

logger = logging.getLogger("llm-service")

# 配置（沿用环境变量）
API_KEY = os.environ.get("LLM_API_KEY", "")
DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "qwen2.5-7b-instruct")
N_CTX = int(os.environ.get("LLM_N_CTX", "8192"))
ENGINE_API_KEY = os.environ.get("LLM_ENGINE_KEY", "local")

router = APIRouter()


# ----------------------------------------------------------------------------
# 网关鉴权：环境变量 LLM_API_KEY + keys.json 中任意生成 Key
# ----------------------------------------------------------------------------
def require_key(authorization: str = Header(None)) -> Optional[str]:
    """网关鉴权，返回命中的 Key（用于后续模型绑定校验），开放场景返回 None。

    - 环境变量 LLM_API_KEY 非空时，必须携带该 Key（优先级最高）；
    - 否则/同时，接受 keys.json 中任意一条生成 Key（Bearer 或裸值）；
    - 若同时无任何 Key 配置（环境变量为空且 keys.json 为空/缺失）则保持开放。
    keys.json 缺失或解析失败时按「无生成 Key」处理，不会因文件问题崩溃。
    """
    valid = set()
    if API_KEY:
        valid.add(API_KEY)
    try:
        for it in list_keys():
            k = it.get("key")
            if k:
                valid.add(k)
    except Exception:
        pass
    if not valid:
        return None
    # 环境变量 Key 优先级最高
    if API_KEY and authorization in (f"Bearer {API_KEY}", API_KEY):
        return API_KEY
    # 再匹配 keys.json 中生成的 Key（Bearer 或裸值）
    if authorization in valid:
        return authorization
    if isinstance(authorization, str) and authorization.startswith("Bearer "):
        bare = authorization[len("Bearer "):]
        if bare in valid:
            return bare
    # 给出长度提示：客户端常见错误是 Key 只复制/粘贴了前 60 位
    sent = ""
    if isinstance(authorization, str):
        sent = (authorization[len("Bearer "):]
                if authorization.startswith("Bearer ") else authorization)
    raise HTTPException(
        status_code=401,
        detail=f"Invalid or missing API key（收到 {len(sent.strip())} 位，完整 Key 应为 64 位）")


def _model_for_key(key: Optional[str]) -> Optional[str]:
    """返回该 Key 绑定的模型名；全局 Key（model=''）返回 ''；未找到/出错返回 None。"""
    if not key:
        return None
    try:
        for it in list_keys():
            if it.get("key") == key:
                return it.get("model") or ""
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------------
# 上下文裁剪（防引擎 400 超限）
# ----------------------------------------------------------------------------
def estimate_tokens(messages: List[dict]) -> int:
    """粗略估算 token 数（中文 0.6/字，其它 0.25/字符 + 每条 4）。"""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        else:
            text = json.dumps(content, ensure_ascii=False) if content else ""
        cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        other = len(text) - cjk
        total += int(cjk * 0.6 + other * 0.25) + 4
    return total


def trim_messages(messages: List[dict], budget: int) -> List[dict]:
    """超出预算时丢弃最早的（非 system）消息。"""
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    other = [m for m in messages if m.get("role") != "system"]
    while estimate_tokens(sys_msgs + other) > budget and len(other) > 1:
        other.pop(0)
    return sys_msgs + other


def _normalize(data: dict) -> None:
    """工具调用时把空 content 归一为 null（符合 OpenAI 规范，避免下游 400）。"""
    try:
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            if msg.get("tool_calls") and (msg.get("content") in (None, "")):
                msg["content"] = None
            # 兜底：若引擎仍走思考通道（content 空、reasoning_content 有值），
            # 将 reasoning_content 回填到 content，避免只读 content 的客户端（如 WorkBuddy）显示空回复。
            elif (msg.get("content") in (None, "")) and msg.get("reasoning_content"):
                msg["content"] = msg["reasoning_content"]
    except Exception:
        pass


def _rewrite_sse_line(line: bytes, model: str):
    """
    改写 SSE 行：
      1. 把 data 行的 model 字段替换为请求模型名；
      2. 将思考通道 delta.reasoning_content 合并进 delta.content，
         保证只读 content 的客户端（如 WorkBuddy）也能看到完整流式输出
         （思考模型关闭不了思考时，正文会先进 reasoning_content，必须兜底）。
    """
    if not line.startswith(b"data:"):
        return line + b"\n" if line else None
    payload = line[len(b"data:"):].strip()
    if not payload or payload == b"[DONE]":
        return line + b"\n"
    try:
        obj = json.loads(payload)
        obj["model"] = model
        for choice in obj.get("choices", []):
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            reasoning = delta.get("reasoning_content")
            if reasoning:
                # 思考内容合并进 content（保留 reasoning_content 字段供高级客户端使用）
                delta["content"] = (delta.get("content") or "") + reasoning
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"
    except Exception:
        return line + b"\n"


# ----------------------------------------------------------------------------
# 网关错误映射
# ----------------------------------------------------------------------------
def _pool_error_response(e: ModelPoolError) -> JSONResponse:
    status = {1001: 404, 1002: 404, 1003: 503, 1004: 503, 1005: 429}.get(e.code, 400)
    return JSONResponse(status_code=status,
                        content={"error": {"message": e.message, "code": e.code}})


async def _ensure_ready(pool, model: str, timeout: int = 180) -> Optional[str]:
    """
    确保模型已加载并就绪，返回引擎 base url；失败抛 ModelPoolError。
    """
    try:
        entry = pool.load(model)
    except ModelPoolError:
        raise
    deadline = time.time() + timeout
    while time.time() < deadline:
        entry = pool.get(model)
        if entry is None:
            raise ModelPoolError(1008, f"model '{model}' was unloaded")
        st = entry.instance.status
        if st == "ready":
            return entry.base_url()
        if st == "paused":
            # 已加载但未运行：尝试启动（运行名额不足会抛 1005，提示先停一个）
            try:
                pool.resume(model)
            except ModelPoolError as e:
                raise e
        elif st == "error":
            raise ModelPoolError(1003, f"engine for '{model}' failed to start")
        await asyncio.sleep(0.4)
    raise ModelPoolError(1003, f"engine for '{model}' timeout waiting ready")


# ----------------------------------------------------------------------------
# 路由
# ----------------------------------------------------------------------------
@router.get("/health")
def health():
    """健康检查：含 pool 信息（保留原 /health 并扩展）。"""
    pool = get_model_pool()
    sys_info = detect_system()
    loaded = pool.list_loaded()
    return {
        "status": "ok" if loaded else "idle",
        "gateway_port": int(os.environ.get("LLM_PORT", "8000")),
        "engine_base_port": pool.engine_base_port,
        "max_concurrent": pool.max_concurrent,
        "loaded_models": [e["name"] for e in loaded],
        "pool": {"max_concurrent": pool.max_concurrent, "loaded": loaded},
        "system": {
            "free_ram_gb": sys_info["free_ram_gb"],
            "suggested_max_models": sys_info["suggested_max_models"],
        },
    }


@router.get("/v1/models", dependencies=[Depends(require_key)])
def list_models():
    """模型列表：来自注册表，loaded 标记来自 ModelPool。"""
    pool = get_model_pool()
    loaded_names = {e["name"] for e in pool.list_loaded()}
    data = [{
        "id": name,
        "object": "model",
        "owned_by": "local",
        "loaded": name in loaded_names,
    } for name in load_registry().keys()]
    return {"object": "list", "data": data}


@router.post("/v1/chat/completions")
async def chat_completions(request: Request, key: Optional[str] = Depends(require_key)):
    body = await request.json()
    model = body.get("model") or DEFAULT_MODEL
    # 模型绑定校验（auth 层面，先于模型可用性检查；/v1/models 不做此校验）
    bound = _model_for_key(key)
    if bound and bound != model:
        raise HTTPException(status_code=403, detail=f"该 API Key 仅可用于模型 {bound}")
    messages = body.get("messages", [])
    if not isinstance(messages, list) or not messages:
        return JSONResponse(status_code=400,
                            content={"error": {"message": "messages required"}})

    max_tokens = int(body.get("max_tokens", 2048))
    stream = bool(body.get("stream", False))

    pool = get_model_pool()
    try:
        base = await _ensure_ready(pool, model)
    except ModelPoolError as e:
        return _pool_error_response(e)

    # 上下文裁剪
    budget = max(512, N_CTX - max_tokens - 200)
    body["messages"] = trim_messages(messages, budget)

    url = f"{base}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ENGINE_API_KEY}",
        # 强制关闭 keep-alive：引擎默认仅 1 slot，空闲长连接会长期占用该 slot，
        # 导致后续请求挂起超时（见缺陷修复说明）。显式 Connection: close 确保响应后释放 slot。
        "Connection": "close",
    }

    if not stream:
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)), timeout=600) as client:
                resp = await client.post(url, json=body, headers=headers)
            data = resp.json()
            _normalize(data)
            data["model"] = model
            return JSONResponse(content=data, status_code=resp.status_code)
        except Exception as e:
            logger.exception("chat failed")
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

    async def event_stream():
        buf = b""
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)), timeout=600) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    async for raw in resp.aiter_raw():
                        buf += raw
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            out = _rewrite_sse_line(line, model)
                            if out is not None:
                                yield out
            if buf:
                out = _rewrite_sse_line(buf, model)
                if out is not None:
                    yield out
        except Exception as e:
            logger.exception("stream failed")
            yield ("data: " + json.dumps({"error": {"message": str(e)}}, ensure_ascii=False) + "\n\n").encode("utf-8")

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/v1/completions")
async def completions(request: Request, key: Optional[str] = Depends(require_key)):
    body = await request.json()
    model = body.get("model") or DEFAULT_MODEL
    # 模型绑定校验（与 chat/completions 一致，先于模型可用性检查）
    bound = _model_for_key(key)
    if bound and bound != model:
        raise HTTPException(status_code=403, detail=f"该 API Key 仅可用于模型 {bound}")
    prompt = body.get("prompt", "")
    if not prompt:
        return JSONResponse(status_code=400, content={"error": {"message": "prompt required"}})
    stream = bool(body.get("stream", False))

    pool = get_model_pool()
    try:
        base = await _ensure_ready(pool, model)
    except ModelPoolError as e:
        return _pool_error_response(e)

    url = f"{base}/v1/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {ENGINE_API_KEY}",
               "Connection": "close"}

    if not stream:
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)), timeout=600) as client:
                resp = await client.post(url, json=body, headers=headers)
            data = resp.json()
            data["model"] = model
            return JSONResponse(content=data, status_code=resp.status_code)
        except Exception as e:
            logger.exception("completion failed")
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

    async def event_stream():
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)), timeout=600) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")
