# -*- coding: utf-8 -*-
"""
上下文窗口管理（2026-10-26，模仿 WorkBuddy 的自动压缩）。

问题背景
--------
老实现（`agent.MAX_HISTORY_MESSAGES = 24`）是「只取最近 24 条，更早的直接丢掉」：

  · 长任务跑到第 25 条以后，前面定下的目标、已改过的文件、已确认的结论**全部
    无声消失**，模型开始重复劳动、甚至推翻自己之前的决定；
  · 丢掉多少、剩多少、什么时候丢的，用户完全看不见（没有「上下文已压缩」这类
    提示），表现就是「模型突然变傻了」。

WorkBuddy 的做法是两个关键点，这里照着实现：

  1. **不是丢，是压**：超阈值时把较早的对话交给模型压成一段要点摘要，摘要随
     后一直带在上下文最前面；原始消息仍留在会话文件里（用户回看历史不受影响）。
  2. **看得见**：压缩发生时推送一个事件，界面显示「✓ 上下文已压缩」；同时
     一直显示当前上下文占用（多少 k / 多少 k）。

阈值口径
--------
`estimate_tokens()` 用「汉字≈1 token、其它≈4 字符 1 token」的保守估算，不引入
分词器依赖（打包体积、离线可用性都更重要）。宁可早一点压缩，也不要冒请求被
上游按超长截断的风险。

压缩完了仍然超（摘要本身太大 / 单条消息巨长）时，会继续收缩保留窗口；到下限
（`MIN_KEEP`）还超就只能硬截断，并在事件里如实标记 `truncated=True`。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

#: 默认上下文窗口（tokens）。8k 是本地 4B~8B 模型的常见档位。
DEFAULT_LIMIT = 8192

#: 压缩后保留的最近消息条数（太少接不上话，太多压了等于没压）
DEFAULT_KEEP = 6

#: 收缩保留窗口的下限
MIN_KEEP = 4

#: 触发压缩的占用比例（留 28% 给 system 提示 + 本轮回复）
TRIGGER_RATIO = 0.72

#: 摘要正文上限（字符）
SUMMARY_MAX_CHARS = 1400

#: 生成摘要时给模型的输出预算
SUMMARY_MAX_TOKENS = 512

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

#: 摘要提示词（明确要求保住「目标 / 结论 / 路径 / 未决项」，避免压完变废话）
_SUMMARY_PROMPT = """你是对话压缩器。把下面这段较早的对话压缩成一份要点摘要，供后续继续任务时参考。

要求：
1. 保留：用户的目标与约束、已经完成的动作及其结论、涉及的文件/目录/命令、尚未解决的问题；
2. 丢弃：寒暄、重复表述、失败尝试的中间过程（但要说清「某方案已失败」）；
3. 用中文，条目式，不超过 300 字，不要编造原文没有的信息；
4. 只输出摘要正文，不要任何前言后语。

{previous}对话正文：
{body}"""


# --------------------------------------------------------------------------- #
# 估算
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    """保守估算 token 数：汉字按 1 token，其它字符按 4 字符 1 token。"""
    if not text:
        return 0
    s = str(text)
    cjk = len(_CJK_RE.findall(s))
    rest = max(0, len(s) - cjk)
    return cjk + (rest + 3) // 4


def message_tokens(msg: Dict[str, Any]) -> int:
    """单条消息的 token（含 role 开销 4）。"""
    if not isinstance(msg, dict):
        return 0
    content = msg.get("content")
    if isinstance(content, list):
        # 多模态 content（[{type:text,...}, ...]）只数文本部分
        text = " ".join(str(p.get("text") or "") for p in content
                        if isinstance(p, dict))
    else:
        text = str(content or "")
    total = estimate_tokens(text) + 4
    calls = msg.get("tool_calls")
    if calls:
        try:
            total += estimate_tokens(json.dumps(calls, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass
    return total


def messages_tokens(messages: List[Dict[str, Any]]) -> int:
    return sum(message_tokens(m) for m in (messages or []))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def context_limit(cfg: Optional[Dict[str, Any]] = None) -> int:
    """上下文窗口（tokens）。支持 8192 / "8k" / "32k" 三种写法。"""
    raw = (cfg or {}).get("context_window")
    if raw is None or raw == "":
        return DEFAULT_LIMIT
    if isinstance(raw, (int, float)):
        return max(1024, int(raw))
    text = str(raw).strip().lower().replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)k$", text)
    if m:
        return max(1024, int(float(m.group(1)) * 1024))
    try:
        return max(1024, int(float(text)))
    except ValueError:
        return DEFAULT_LIMIT


def history_of(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    """会话里参与上下文的原始消息（只留 user / assistant 且有正文）。"""
    return [m for m in (session.get("messages") or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")
            and m.get("content")]


def usage(session: Dict[str, Any],
          cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """当前上下文占用：给前端显示「上下文 32% · 2.6k/8k」。"""
    limit = context_limit(cfg)
    history = history_of(session)
    ctx = session.get("context") or {}
    if ctx.get("summary"):
        keep = int(ctx.get("keep") or DEFAULT_KEEP)
        tail = history[-keep:] if keep > 0 else []
        used = estimate_tokens(str(ctx.get("summary"))) + messages_tokens(tail)
    else:
        used = messages_tokens(history)
    return {"used": used, "limit": limit,
            "ratio": round(min(1.0, used / float(limit)), 4),
            "compressed": bool(ctx.get("summary")),
            "compressed_count": int(ctx.get("compressed") or 0),
            "at": str(ctx.get("at") or "")}


# --------------------------------------------------------------------------- #
# 压缩
# --------------------------------------------------------------------------- #
def _rule_summary(messages: List[Dict[str, Any]]) -> str:
    """兜底摘要：模型不可用时按「角色 + 前 160 字」抽取，绝不返回空。"""
    lines: List[str] = []
    for m in messages:
        role = "用户" if m.get("role") == "user" else "助理"
        text = str(m.get("content") or "").strip().replace("\n", " ")
        if not text:
            continue
        lines.append(f"- {role}：{text[:160]}{'…' if len(text) > 160 else ''}")
    out = "\n".join(lines)
    return out[:SUMMARY_MAX_CHARS]


def _llm_summary(messages: List[Dict[str, Any]],
                 previous: str,
                 provider: Any) -> str:
    """让模型压；失败抛异常，由调用方降级到规则摘要。"""
    body_lines: List[str] = []
    for m in messages:
        role = "用户" if m.get("role") == "user" else "助理"
        text = str(m.get("content") or "").strip()
        if text:
            body_lines.append(f"[{role}] {text}")
    body = "\n".join(body_lines)[:12000]
    prompt = _SUMMARY_PROMPT.format(
        previous=(f"此前已有摘要（需要一并合并进来）：\n{previous}\n\n" if previous else ""),
        body=body)
    reply = provider.chat([{"role": "user", "content": prompt}],
                          max_tokens=SUMMARY_MAX_TOKENS, timeout=90.0)
    if getattr(reply, "error", ""):
        raise RuntimeError(reply.error)
    text = str(getattr(reply, "content", "") or "").strip()
    if not text:
        raise RuntimeError("摘要为空")
    return text[:SUMMARY_MAX_CHARS]


def maybe_compress(session: Dict[str, Any],
                   cfg: Optional[Dict[str, Any]],
                   provider: Any = None,
                   force: bool = False) -> Optional[Dict[str, Any]]:
    """必要时压缩历史，返回事件字典（未压缩返回 None）。

    压缩只在「超出触发线」时发生；已经压过的会话再次超线时做**滚动压缩**：
    把「旧摘要 + 新一批较早消息」再压一次，摘要不会无限增长。
    """
    limit = context_limit(cfg)
    history = history_of(session)
    if not history:
        return None

    ctx = dict(session.get("context") or {})
    keep = int(ctx.get("keep") or DEFAULT_KEEP)
    keep = max(MIN_KEEP, min(keep, max(MIN_KEEP, len(history) // 2 or MIN_KEEP)))

    # 当前实际送给模型的量：已有摘要时 = 摘要 + 保留窗口
    if ctx.get("summary"):
        tail = history[-keep:] if keep > 0 else []
        used = estimate_tokens(str(ctx.get("summary"))) + messages_tokens(tail)
        older = history[:-keep] if len(history) > keep else []
    else:
        used = messages_tokens(history)
        older = history[:-keep] if len(history) > keep else []

    trigger = limit * TRIGGER_RATIO
    if not force and used <= trigger:
        return None
    if not older:
        # 还没到能压的程度（消息条数太少，但单条巨长）→ 只能如实标记超限
        return {"type": "context", "used": used, "limit": limit,
                "ratio": round(used / float(limit), 4),
                "compressed": False, "over": used > limit,
                "note": "消息条数不足，无法压缩"}

    before = used
    previous = str(ctx.get("summary") or "")
    summary = ""
    via = "llm"
    if provider is not None:
        try:
            summary = _llm_summary(older, previous, provider)
        except Exception:  # noqa: BLE001 —— 摘要失败绝不影响本轮对话
            summary = ""
    if not summary:
        via = "rule"
        merged = (previous + "\n" if previous else "") + _rule_summary(older)
        summary = merged.strip()[:SUMMARY_MAX_CHARS]

    tail = history[-keep:] if keep > 0 else []
    after = estimate_tokens(summary) + messages_tokens(tail)

    # 压完还超：继续收窄保留窗口（摘要保留，原始消息留在会话文件里）
    while after > trigger and keep > MIN_KEEP:
        keep = max(MIN_KEEP, keep - 1)
        tail = history[-keep:]
        after = estimate_tokens(summary) + messages_tokens(tail)

    session["context"] = {
        "summary": summary,
        "keep": keep,
        "compressed": int(ctx.get("compressed") or 0) + len(older),
        "at": time.strftime("%H:%M:%S"),
        "tokens_before": before,
        "tokens_after": after,
        "via": via,
    }
    return {"type": "context", "used": after, "limit": limit,
            "ratio": round(after / float(limit), 4),
            "compressed": True, "folded": len(older), "keep": keep,
            "saved": max(0, before - after), "via": via,
            "truncated": after > limit,
            "note": "上下文已压缩"}
