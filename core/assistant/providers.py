# -*- coding: utf-8 -*-
"""
模型接入层（core/assistant/providers.py）。

三种提供商统一到 OpenAI 兼容协议：
    local       llmBuddy 内置网关（127.0.0.1:{LLM_PORT}/v1），复用已加载的本地模型
    openai      任意 OpenAI 兼容端点（硅基流动 / DeepSeek / 通义 / vLLM / Ollama 等）

要点：
    - trust_env=False 访问本机；访问外网时 trust_env=True 以便走系统代理
      （本机确有 HTTP 代理，硅基流动直连必失败）
    - 流式解析同时收 content / reasoning_content / tool_calls 增量
    - 引擎带 --jinja，原生 function calling 可用；万一模板不支持，Agent 层
      还有「文本内 JSON 工具调用」兜底（见 agent.py）
"""
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import httpx


@dataclass
class ChatReply:
    """一次模型回复。"""

    content: str = ""
    reasoning: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # 正文为空、用思考内容兜底时为 True：Agent 会在对话里提示「只有思考没有正文」，
    # 避免用户把推理过程误当最终答案。
    fallback_reasoning: bool = False
    #: 自愈记录（如「max_tokens 2048 被用尽 → 自动放大到 8192 重试」），
    #: Agent 以 note 事件显示，便于用户理解为什么这轮变慢了
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class ProviderError(RuntimeError):
    pass


#: max_tokens 自愈上限：空正文时按 4 倍放大，最多到这个值
#: （32k 已足够让 R1/Qwen3-thinking 既思考完又写出正文）
MAX_TOKENS_CAP = 32768

_STATUS_HINT = {
    401: "鉴权失败（401，请检查 API Key）",
    402: "余额不足（402）",
    403: "无权限（403）",
    404: "模型或端点不存在（404）",
    429: "触发限流（429，TPM/RPM 超限）",
    500: "上游内部错误（500）",
    502: "上游网关错误（502）",
    503: "上游繁忙（503）",
    504: "上游超时（504）",
}


def _status_hint(code: int) -> str:
    """把 HTTP 状态码翻成人话，避免用户只看到一串 `HTTP 503：{...}`。"""
    return _STATUS_HINT.get(code, f"请求失败（{code}）")


def _local_api_key() -> str:
    """本地网关 Key：环境变量优先，其次 keys.json 里任意一条。"""
    key = os.environ.get("LLM_API_KEY", "")
    if key:
        return key
    try:
        from core.config import list_keys
        for it in list_keys():
            if it.get("key"):
                return it["key"]
    except Exception:
        pass
    return ""


class Provider:
    """OpenAI 兼容提供商（含本地引擎特例）。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.id = cfg.get("id") or "local"
        self.label = cfg.get("label") or self.id
        self.kind = cfg.get("kind") or "openai"
        self.base_url = (cfg.get("base_url") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or ""
        self.model = cfg.get("model") or ""
        self._resolved_model = ""
        self._resolved_at = 0.0

    # ------------------------------------------------------------------ #
    # 地址与鉴权
    # ------------------------------------------------------------------ #
    @property
    def is_local(self) -> bool:
        return self.kind == "local"

    def _gateway_port(self) -> int:
        return int(os.environ.get("LLM_PORT", "8000"))

    def endpoint(self) -> str:
        """chat/completions 完整地址。"""
        if self.is_local:
            base = self.base_url or f"http://127.0.0.1:{self._gateway_port()}/v1"
        else:
            base = self.base_url
        if not base:
            raise ProviderError("未配置 base_url")
        base = base.rstrip("/")
        if base.endswith("/v1"):
            return base + "/chat/completions"
        if "/chat/completions" in base:
            return base
        return base + "/v1/chat/completions"

    def _models_endpoint(self) -> str:
        url = self.endpoint()
        return url[: -len("/chat/completions")] + "/models"

    def headers(self) -> Dict[str, str]:
        key = self.api_key
        if self.is_local and not key:
            key = _local_api_key()
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if self.is_local:
            # 让本机网关把「思考」留在 reasoning_content、正文留在 content，
            # 助理据此把思考折叠展示，避免把推理过程当答案。
            h["X-LLM-Raw-Reasoning"] = "1"
        return h

    def _client(self, timeout: float = 300.0) -> httpx.Client:
        # 本机：绕过代理；外网：走系统代理（本机 7897 代理是访问硅基流动的前提）
        return httpx.Client(trust_env=not self.is_local, timeout=timeout,
                            headers=self.headers())

    # ------------------------------------------------------------------ #
    # 模型名
    # ------------------------------------------------------------------ #
    def resolve_model(self, preferred: str = "") -> str:
        """确定要请求的模型名。本地未指定时取已加载的第一个模型。"""
        if preferred:
            return preferred
        if self.model:
            return self.model
        if not self.is_local:
            raise ProviderError(f"{self.label}：未指定模型")
        now = time.time()
        if self._resolved_model and now - self._resolved_at < 8:
            return self._resolved_model
        try:
            with self._client(10.0) as c:
                r = c.get(self._models_endpoint())
            if r.status_code < 400:
                items = (r.json() or {}).get("data") or []
                loaded = [it for it in items if isinstance(it, dict) and it.get("loaded")]
                pick = (loaded or items)
                if pick:
                    self._resolved_model = pick[0].get("id") or ""
                    self._resolved_at = now
                    if self._resolved_model:
                        return self._resolved_model
        except Exception as e:
            raise ProviderError(f"本地引擎不可用：{e}")
        raise ProviderError("本地没有已加载的模型，请先在「模型库」加载一个模型，"
                            "或在智能助理右上角改用云端模型")

    def list_models(self) -> List[str]:
        try:
            with self._client(12.0) as c:
                r = c.get(self._models_endpoint())
            if r.status_code >= 400:
                return []
            items = (r.json() or {}).get("data") or []
            return [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
        except Exception:
            return []

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             model: str = "",
             temperature: float = 0.3,
             max_tokens: int = 2048,
             on_delta: Optional[Callable[[str, str], None]] = None,
             cancel_check: Optional[Callable[[], bool]] = None,
             timeout: float = 300.0) -> ChatReply:
        """流式对话；on_delta(channel, text)，channel ∈ content / reasoning / reset。"""
        target_model = self.resolve_model(model)
        budget = max(1, int(max_tokens or 2048))
        payload: Dict[str, Any] = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": budget,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        reply = self._stream_once(payload, on_delta, cancel_check, timeout)

        # ---- 空正文自愈（用户反馈：思考型模型几乎每次都报「没有返回正文」）----
        # 根因：R1 / Qwen3-thinking 这类模型把 max_tokens 预算全花在思考上，
        # 流式结束后 content 为空、finish_reason="length"。旧实现直接报错让用户
        # 自己去设置里改，体验极差。这里自动把预算放大 4 倍重试一次（封顶
        # MAX_TOKENS_CAP），并把这次自愈作为 note 告知用户。
        if reply.ok and not (reply.content or "").strip() and not reply.tool_calls:
            truncated = str(reply.finish_reason or "").lower() in (
                "length", "max_tokens", "length_exceeded", "max_output_tokens")
            blank = not (reply.reasoning or "").strip()
            if budget < MAX_TOKENS_CAP and (truncated or blank):
                new_budget = int(min(max(budget * 4, 8192), MAX_TOKENS_CAP))
                payload["max_tokens"] = new_budget
                note = (f"上一轮 max_tokens={budget} 被用尽（finish_reason="
                        f"{reply.finish_reason or '未知'}），已自动放大到 "
                        f"{new_budget} 重试")
                if on_delta and (reply.content or reply.reasoning):
                    # 清掉前端已渲染的半截思考/正文，避免重试内容叠在旧内容后面
                    on_delta("reset", "")
                retry = self._stream_once(payload, on_delta, cancel_check, timeout)
                if retry.ok:
                    retry.note = note
                    reply = retry

        # 本地 qwen3 思考模型：正文可能全在 reasoning 里（网关已兜底合并，
        # 但云端部分模型同样存在此现象），这里再兜一层，保证 Agent 有输入。
        if reply.ok and not reply.content.strip() and not reply.tool_calls \
                and reply.reasoning.strip():
            reply.content = reply.reasoning.strip()
            reply.fallback_reasoning = True
        return reply

    def _stream_once(self, payload: Dict[str, Any],
                     on_delta: Optional[Callable[[str, str], None]],
                     cancel_check: Optional[Callable[[], bool]],
                     timeout: float) -> ChatReply:
        """发一次流式请求（429/5xx/网络错误退避重试，最多 3 次）。"""
        reply = ChatReply()
        attempts = 3
        for attempt in range(1, attempts + 1):
            reply = ChatReply()
            try:
                with self._client(timeout) as c:
                    with c.stream("POST", self.endpoint(), json=payload) as resp:
                        if resp.status_code >= 400:
                            resp.read()
                            body = (resp.text or "")[:400]
                            # 免费额度常见 429（TPM/RPM）与偶发 5xx：退避重试，
                            # 直接失败会让技能编排白跑好几步
                            if resp.status_code in (429, 500, 502, 503, 504) \
                                    and attempt < attempts:
                                time.sleep(2.0 * attempt)
                                continue
                            raise ProviderError(
                                f"{_status_hint(resp.status_code)}"
                                f"（HTTP {resp.status_code}）：{body}")
                        self._consume_sse(resp, reply, on_delta, cancel_check)
                break
            except ProviderError as e:
                reply.error = str(e)
                if "429" in str(e) and attempt < attempts:
                    continue
                break
            except httpx.HTTPError as e:
                reply.error = f"网络错误：{e}"
                # 已经推过增量就别重试，否则前端会看到重复文字
                if attempt < attempts and not (reply.content or reply.reasoning):
                    time.sleep(2.0 * attempt)
                    continue
                break
            except Exception as e:
                reply.error = f"{type(e).__name__}: {e}"
                break
        return reply

    @staticmethod
    def _consume_sse(resp: httpx.Response, reply: ChatReply,
                     on_delta: Optional[Callable[[str, str], None]],
                     cancel_check: Optional[Callable[[], bool]]) -> None:
        """解析 OpenAI SSE：content / reasoning_content / tool_calls 增量。

        兼容性（1.0.9）：
          - 有的网关忽略 `stream=true` 直接回**整包 JSON**；也有网关在 200 里塞
            错误体（`{"code":50508,"message":"System is too busy"}`）。旧实现只认
            `data:` 行，遇到这两种情况会静默得到「空回复」，前端只看到
            「模型没有返回正文」——完全误导。这里缓存原始文本，解析不到增量时
            兜底按 JSON 解析（正常回复直接取用、错误体转成明确报错）。
          - `data:` 前缀允许前后空白。
        """
        calls: Dict[int, Dict[str, Any]] = {}
        raw_parts: List[str] = []
        raw_len = 0
        saw_data = False
        for line in resp.iter_lines():
            if cancel_check and cancel_check():
                reply.finish_reason = "cancelled"
                break
            text = (line or "").strip()
            if text and raw_len < 8000:
                raw_parts.append(text)
                raw_len += len(text)
            if not text:
                continue
            if not text.startswith("data:"):
                continue
            saw_data = True
            data = text[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("error"):
                reply.error = str(obj["error"])[:300]
                break
            choices = obj.get("choices") or []
            if not choices:
                continue
            ch = choices[0] or {}
            delta = ch.get("delta") or {}
            if ch.get("finish_reason"):
                reply.finish_reason = ch["finish_reason"]
            piece = delta.get("content")
            if piece:
                reply.content += piece
                if on_delta:
                    on_delta("content", piece)
            think = delta.get("reasoning_content") or delta.get("reasoning")
            if think:
                reply.reasoning += think
                if on_delta:
                    on_delta("reasoning", think)
            for tc in (delta.get("tool_calls") or []):
                idx = int(tc.get("index") or 0)
                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        reply.tool_calls = [calls[k] for k in sorted(calls) if calls[k].get("name")]
        for i, tc in enumerate(reply.tool_calls):
            if not tc.get("id"):
                tc["id"] = f"call_{i}_{int(time.time() * 1000) % 100000}"

        if reply.content.strip() or reply.reasoning.strip() or reply.tool_calls \
                or reply.error:
            return
        # 一个增量都没解析出来：把原始文本当 JSON 再试一次（非流式 / 错误体）
        raw = "\n".join(raw_parts).strip()
        if not raw:
            reply.error = "上游返回了空响应（没有任何内容）"
            return
        obj: Any = None
        try:
            obj = json.loads(raw)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            if obj.get("error"):
                reply.error = str(obj["error"])[:300]
                return
            if obj.get("code") or obj.get("message"):
                reply.error = f"上游错误：{str(obj.get('message') or obj)[:300]}"
                return
            choices = obj.get("choices") or []
            if choices:
                ch = choices[0] or {}
                msg = ch.get("message") or {}
                reply.content = str(msg.get("content") or "")
                reply.reasoning = str(msg.get("reasoning_content")
                                      or msg.get("reasoning") or "")
                reply.finish_reason = str(ch.get("finish_reason") or "")
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        reply.tool_calls.append({
                            "id": tc.get("id") or f"call_{i}", "name": fn["name"],
                            "arguments": fn.get("arguments") or ""})
                if reply.content or reply.reasoning or reply.tool_calls:
                    if on_delta and reply.reasoning:
                        on_delta("reasoning", reply.reasoning)
                    if on_delta and reply.content:
                        on_delta("content", reply.content)
                    return
        if not saw_data:
            reply.error = ("上游没有按流式协议返回（缺少 data: 行）："
                           f"{raw[:300]}")


_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def get_provider(cfg: Dict[str, Any]) -> Provider:
    """按配置取 Provider（缓存，避免每轮重复构造）。"""
    key = json.dumps({k: cfg.get(k) for k in
                      ("id", "base_url", "api_key", "model", "kind")},
                     ensure_ascii=False, sort_keys=True)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    p = Provider(cfg)
    with _CACHE_LOCK:
        _CACHE[key] = (time.time(), p)
    return p
