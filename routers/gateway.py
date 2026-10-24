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
# 默认上下文：Agent 客户端（WorkBuddy 等）单次请求常达 3 万 tokens 以上，
# 8192 必然超限；32768 是 qwen2.5 系列原生支持的最大上下文，作为默认值。
N_CTX = int(os.environ.get("LLM_N_CTX", "32768"))
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
    """粗略估算 token 数（中文 0.75/字，其它 0.28/字符 + 每条 8）。"""
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
        # 0.75/0.28 系数（2026-10-19 实测校准）：Qwen 分词器下中文约 1 token/1.3 字，
        # 原 0.6 系数会低估约 30%，导致「以为塞得进、实际超限」。
        total += int(cjk * 0.75 + other * 0.28) + 8
    return total


def trim_messages(messages: List[dict], budget: int) -> List[dict]:
    """超出预算时丢弃最早的（非 system）消息。"""
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    other = [m for m in messages if m.get("role") != "system"]
    while estimate_tokens(sys_msgs + other) > budget and len(other) > 1:
        other.pop(0)
    return sys_msgs + other


# 上下文预算保留余量（tokens）：覆盖 chat template、特殊符号等开销
CTX_MARGIN = 256
# 估算安全系数：我们的 token 估算与引擎实际计数存在 5%~10% 偏差，预留缓冲
CTX_SAFETY = 1.12


def _text_tokens(text: str) -> int:
    """单个字符串的粗略 token 估算（与 estimate_tokens 口径一致）。"""
    if not text:
        return 0
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    return int(cjk * 0.75 + (len(text) - cjk) * 0.28)


def estimate_tools(tools) -> int:
    """估算 tools（函数定义）占用的 token 数。

    ⚠️ 这是 2026-10-19 上下文超限问题的关键：WorkBuddy 等 Agent 客户端会把
    上百个工具的 JSON Schema 塞进 tools 字段（常达 2~3 万 tokens），旧实现只裁剪
    messages、完全没算 tools，导致请求仍远超引擎上下文而 400。
    """
    if not tools:
        return 0
    try:
        return _text_tokens(json.dumps(tools, ensure_ascii=False))
    except Exception:
        return 0


def _slim(obj, limit: int):
    """递归截断 schema 中的 description 文本（工具数量与参数结构保持不变）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "description" and isinstance(v, str):
                out[k] = v if len(v) <= limit else v[:limit] + "…"
            else:
                out[k] = _slim(v, limit)
        return out
    if isinstance(obj, list):
        return [_slim(v, limit) for v in obj]
    return obj


def fit_payload(messages: List[dict], tools, n_ctx: int, max_tokens: int):
    """把 messages + tools 压进引擎上下文窗口。

    逐级降级（尽量保住工具能力，实在放不下才禁用工具）：
        1. 裁剪最早的对话历史（保留 system）；
        2. 工具 description 截断到 1000 字（Agent 客户端靠描述选工具，能留就多留）；
        3. 仍不够则 300 字、80 字；
        4. 丢弃 tools，并在最前面补一条系统提示告知模型「本轮工具不可用」。
    返回 (messages, tools, dropped_tools, used_tokens, budget, overflow)。
    overflow=True 表示即使降级到第 4 级仍放不下（由调用方回 400 明确报错）。
    """
    n_ctx = int(n_ctx or N_CTX)
    # max_tokens 不能吃掉整窗：至少给 prompt 留一半
    max_tokens = max(1, min(int(max_tokens or 512), max(64, n_ctx // 2)))
    # 估算值偏乐观（实测引擎计数约为估算的 1.05~1.10 倍，chat template 还有额外开销），
    # 预算再除以安全系数，确保「估算塞得进 = 引擎真塞得进」。
    budget = max(128, int((n_ctx - max_tokens - CTX_MARGIN) / CTX_SAFETY))

    msgs = trim_messages(list(messages or []), budget)
    dropped = False

    def used() -> int:
        return estimate_tokens(msgs) + estimate_tools(tools)

    if tools and used() > budget:
        # 分级截断：先保 1000 字（Agent 客户端靠 description 选择工具，能留就多留），
        # 不够再 300 / 80 字，最后才禁用工具。
        tools = _slim(tools, 1000)
    if tools and used() > budget:
        tools = _slim(tools, 300)
    if tools and used() > budget:
        tools = _slim(tools, 80)
    if tools and used() > budget:
        dropped = True
        tools = None
        note = {"role": "system",
                "content": "（本机模型上下文有限，本轮工具调用已禁用，请直接用文字回答。）"}
        msgs = [note] + msgs
        msgs = trim_messages(msgs, budget)

    return msgs, tools, dropped, used(), budget, used() > budget


def _friendly_engine_error(payload, n_ctx: int, model: str) -> Dict[str, Any]:
    """把引擎的英文错误（尤其上下文超限）翻译成可操作的中文错误体。"""
    obj = payload
    if isinstance(payload, (bytes, bytearray)):
        try:
            obj = json.loads(payload.decode("utf-8", "replace"))
        except Exception:
            obj = None
    elif isinstance(payload, str):
        try:
            obj = json.loads(payload)
        except Exception:
            obj = None

    raw_msg = ""
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict):
            raw_msg = str(err.get("message") or "")
        elif isinstance(err, str):
            raw_msg = err
    low = raw_msg.lower()
    if "exceed" in low and "context" in low:
        need = 0
        if isinstance(obj, dict):
            # 真实 llama.cpp 把 n_prompt_tokens 放在 error 对象内部
            # （{"error":{"code":400,...,"n_prompt_tokens":38771,"n_ctx":32768}}），
            # 老实现只读顶层，导致线上文案永远拿不到具体 token 数。
            err_obj = obj.get("error")
            if isinstance(err_obj, dict):
                need = int(err_obj.get("n_prompt_tokens") or 0)
            if not need:
                need = int(obj.get("n_prompt_tokens") or 0)
        detail = f"请求约 {need} tokens" if need else "请求内容过长"
        return {"error": {
            "message": (f"上下文超限：{detail}，超过模型「{model}」的上下文上限 {n_ctx} tokens。"
                        f"已自动裁剪历史消息与工具定义仍放不下。"
                        f"请在客户端「设置 → 上下文长度 n_ctx」调大（如 32768）后重新加载模型，"
                        f"或新建会话/换用上下文更大的模型。"),
            "type": "context_overflow_error",
            "code": 1010,
        }}
    return {"error": {"message": raw_msg or "本地引擎返回错误", "type": "engine_error", "code": 1009}}


def _normalize(data: dict, raw_reasoning: bool = False) -> None:
    """工具调用时把空 content 归一为 null（符合 OpenAI 规范，避免下游 400）。

    raw_reasoning=True 表示调用方（如智能助理）自己会分别处理思考与正文，
    此时不再把 reasoning_content 回填进 content，避免把思考当答案。
    """
    try:
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            if msg.get("tool_calls") and (msg.get("content") in (None, "")):
                msg["content"] = None
            # 兜底：若引擎仍走思考通道（content 空、reasoning_content 有值），
            # 将 reasoning_content 回填到 content，避免只读 content 的客户端（如 WorkBuddy）显示空回复。
            elif (not raw_reasoning and msg.get("content") in (None, "")
                  and msg.get("reasoning_content")):
                msg["content"] = msg["reasoning_content"]
    except Exception:
        pass


def _rewrite_sse_line(line: bytes, model: str, raw_reasoning: bool = False):
    """
    改写 SSE 行：
      1. 把 data 行的 model 字段替换为请求模型名；
      2. 将思考通道 delta.reasoning_content 合并进 delta.content，
         保证只读 content 的客户端（如 WorkBuddy）也能看到完整流式输出
         （思考模型关闭不了思考时，正文会先进 reasoning_content，必须兜底）。
      3. raw_reasoning=True（请求头 X-LLM-Raw-Reasoning: 1）时不合并，
         让调用方自己区分「思考」和「正文」——智能助理用它折叠思考过程。
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
            if reasoning and not raw_reasoning:
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
    # 高级客户端（智能助理）用该头要求「思考/正文分开」，不做 reasoning→content 合并
    raw_reasoning = str(request.headers.get("x-llm-raw-reasoning", "")).strip() in ("1", "true", "yes")
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

    # 上下文裁剪（2026-10-19 修复：必须把 tools 的 token 一起算进来，
    # 否则 Agent 客户端上百个工具定义会独自撑爆上下文，触发引擎 400）
    entry = pool.get(model)
    n_ctx = int(getattr(entry.instance, "n_ctx", 0) or N_CTX) if entry else N_CTX
    msgs, tools, dropped, used_tokens, budget, overflow = fit_payload(
        messages, body.get("tools"), n_ctx, max_tokens)
    if overflow:
        logger.warning("request for '%s' still exceeds ctx after trimming: used~%d budget=%d",
                       model, used_tokens, budget)
        return JSONResponse(status_code=400, content={"error": {
            "message": (f"上下文超限：裁剪后仍有约 {used_tokens} tokens，超过模型「{model}」"
                        f"可用预算 {budget} tokens（n_ctx={n_ctx}）。"
                        f"请调大「设置 → 上下文长度 n_ctx」（如 32768）后重新加载模型，"
                        f"或新建会话再试。"),
            "type": "context_overflow_error",
            "code": 1010,
        }})
    body["messages"] = msgs
    if tools is None:
        body.pop("tools", None)
        body.pop("tool_choice", None)
        if dropped:
            logger.warning("tools dropped for '%s': prompt ~%d tokens, budget=%d, n_ctx=%d",
                           model, used_tokens, budget, n_ctx)
    else:
        body["tools"] = tools

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
            if resp.status_code >= 400:
                # 引擎错误统一翻译成可读中文（上下文超限给出调参指引）
                return JSONResponse(status_code=resp.status_code,
                                    content=_friendly_engine_error(data, n_ctx, model))
            _normalize(data, raw_reasoning)
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
                    if resp.status_code >= 400:
                        # 流式请求在开始前就被引擎拒绝：把错误以 SSE 事件回给客户端，
                        # 避免客户端只看到「空回复 / 未知错误」。
                        raw_err = await resp.aread()
                        err = _friendly_engine_error(raw_err, n_ctx, model)
                        logger.warning("engine rejected stream request: %s", err)
                        yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
                        yield b"data: [DONE]\n\n"
                        return
                    async for raw in resp.aiter_raw():
                        buf += raw
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            out = _rewrite_sse_line(line, model, raw_reasoning)
                            if out is not None:
                                yield out
            if buf:
                out = _rewrite_sse_line(buf, model, raw_reasoning)
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

    # 上下文适配：prompt 超长时保留尾部（最近内容），避免引擎直接 400
    entry = pool.get(model)
    n_ctx = int(getattr(entry.instance, "n_ctx", 0) or N_CTX) if entry else N_CTX
    max_tokens = int(body.get("max_tokens", 256))
    max_tokens = max(1, min(max_tokens, max(64, n_ctx // 2)))
    budget = max(128, n_ctx - max_tokens - CTX_MARGIN)
    try:
        if isinstance(prompt, list):
            prompt = "".join(p if isinstance(p, str) else str(p) for p in prompt)
            body["prompt"] = prompt
        est = _text_tokens(prompt)
        if est > budget:
            keep = max(1, int(len(prompt) * budget / max(1, est)))
            body["prompt"] = prompt[-keep:]
            logger.warning("completion prompt truncated for '%s': ~%d -> ~%d tokens",
                           model, est, budget)
    except Exception:
        pass

    if not stream:
        try:
            async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)), timeout=600) as client:
                resp = await client.post(url, json=body, headers=headers)
            data = resp.json()
            if resp.status_code >= 400:
                return JSONResponse(status_code=resp.status_code,
                                    content=_friendly_engine_error(data, n_ctx, model))
            data["model"] = model
            return JSONResponse(content=data, status_code=resp.status_code)
        except Exception as e:
            logger.exception("completion failed")
            return JSONResponse(status_code=500, content={"error": {"message": str(e)}})

    async def event_stream():
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)), timeout=600) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    raw_err = await resp.aread()
                    err = _friendly_engine_error(raw_err, n_ctx, model)
                    logger.warning("engine rejected completion stream: %s", err)
                    yield ("data: " + json.dumps(err, ensure_ascii=False) + "\n\n").encode("utf-8")
                    yield b"data: [DONE]\n\n"
                    return
                async for chunk in resp.aiter_raw():
                    yield chunk

    return StreamingResponse(event_stream(), media_type="text/event-stream")
