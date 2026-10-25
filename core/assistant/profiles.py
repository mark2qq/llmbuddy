# -*- coding: utf-8 -*-
"""
按需求自动加载的 profile 匹配（core/assistant/profiles.py）。

profile 是一组「需求关键词 -> 技能 + 工具白名单」的静态声明，用来在用户
只提需求（「帮我把下载目录整理一下」）时，自动把合适的技能挂进 system 提示、
把工具收窄到够用的一组，避免把 24+ 个工具全量塞给本地小模型。

设计约束（架构 §3.1 / PRD §5.4）：
    - **纯本地计算**：无网络、无 IO，可在任何时候同步调用（含单测）。
    - 大小写不敏感的子串匹配（中文没有分词，"整理" 命中 "帮我整理一下"）。
    - 多命中取「命中关键词数最多」；同数取 priority **数值高**者
      （priority 越大越优先，与内置 10/20/30 的语义一致）。
    - `available_skills` 里过滤掉未安装的技能 slug；过滤后为空 → 返回 None
      （让 agent 走 L3 降级：把已安装技能清单写进 system，模型自行 skill_run）。
    - `triggers` 为空数组 = 该 profile 永不自动加载（用户手动禁用即置空）。
"""
from typing import Any, Dict, Iterable, List, Optional

from core.assistant.store import _default_profiles


def _builtin() -> List[Dict[str, Any]]:
    """内置 profile 的拷贝（避免任何调用方意外修改模块级常量）。"""
    return _default_profiles()


#: 3 个内置 profile（随包落地，`ensure_builtin_profiles` 首次写入 config.json）
BUILTIN_PROFILES: List[Dict[str, Any]] = _builtin()


def ensure_builtin_profiles() -> None:
    """首次运行把内置 profile 落到 `config.json.profiles`，不覆盖用户改动。

    "不覆盖" 的口径：
      - `profiles` 不存在 / 不是 list / 空 list -> 写入 3 个内置；
      - 已存在：只**补**缺失的 id（老配置里少一个内置 profile 时补上），
        已存在的同 id 条目整个保留（用户改过的 triggers/tools 不回退）。
    任何异常都吞掉：配置补字段失败不能让整个助理模块起不来。
    """
    try:
        from core.assistant.store import load_config, save_config
        cfg = load_config()
        cur = cfg.get("profiles")
        if not isinstance(cur, list) or not cur:
            save_config({"profiles": _builtin()})
            return
        have = {str(p.get("id") or "") for p in cur if isinstance(p, dict)}
        missing = [p for p in _builtin() if p["id"] not in have]
        if missing:
            save_config({"profiles": list(cur) + missing})
    except Exception:
        pass


def _triggers_of(profile: Dict[str, Any]) -> List[str]:
    """取 profile 的触发词（兼容 triggers / keywords 两种字段名）。"""
    raw = profile.get("triggers")
    if raw is None:
        raw = profile.get("keywords")
    if isinstance(raw, str):
        raw = [t.strip() for t in raw.replace("，", ",").split(",")]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(t).strip().lower() for t in raw if str(t).strip()]


def _priority_of(profile: Dict[str, Any]) -> int:
    try:
        return int(profile.get("priority") or 0)
    except (TypeError, ValueError):
        return 0


def _skills_of(profile: Dict[str, Any]) -> List[str]:
    raw = profile.get("skills") or []
    if isinstance(raw, str):
        raw = [raw]
    return [str(s).strip() for s in raw if str(s).strip()]


def match_profile(text: str, profiles: Optional[Iterable[Dict[str, Any]]] = None,
                  available_skills: Optional[Iterable[str]] = None,
                  ) -> Optional[Dict[str, Any]]:
    """按用户原文匹配一个 profile；未命中返回 None。

    Args:
        text: 用户消息原文（只做子串匹配，不做分词）。
        profiles: profile 列表；None/空 -> 用 BUILTIN_PROFILES。
        available_skills: 已安装技能 slug 集合；为 None 时**不做**技能存在性
            过滤（调用方明确表示「不关心」），为空集合时全部过滤掉。

    Returns:
        命中的 profile dict 拷贝，额外带 `matched`（命中的触发词列表）与
        `skills`（已按 available_skills 过滤后的技能）。未命中返回 None。
    """
    needle = str(text or "").lower()
    if not needle.strip():
        return None
    pool: List[Dict[str, Any]] = [p for p in (profiles if profiles is not None
                                              else BUILTIN_PROFILES)
                                  if isinstance(p, dict)]
    if not pool:
        pool = BUILTIN_PROFILES

    avail: Optional[set] = None
    if available_skills is not None:
        avail = {str(s) for s in available_skills}

    best: Optional[Dict[str, Any]] = None
    best_hits = 0
    best_priority = 0
    for p in pool:
        if not p.get("enabled", True):
            continue
        hits = [t for t in _triggers_of(p) if t in needle]
        if not hits:
            continue
        skills = _skills_of(p)
        if avail is not None:
            skills = [s for s in skills if s in avail]
            if not skills:
                # 技能一个都没装：该 profile 不可用，交给 L3 降级
                continue
        if len(hits) > best_hits or (len(hits) == best_hits
                                    and _priority_of(p) > best_priority):
            best = p
            best_hits = len(hits)
            best_priority = _priority_of(p)

    if best is None:
        return None
    out = dict(best)
    out["skills"] = list(_skills_of(best))
    if avail is not None:
        out["skills"] = [s for s in out["skills"] if s in avail]
    out["tools"] = [str(t) for t in (best.get("tools") or []) if str(t).strip()]
    out["matched"] = [t for t in _triggers_of(best) if t in needle]
    out["priority"] = _priority_of(best)
    return out


def available_skill_slugs() -> List[str]:
    """已安装技能的 slug 列表（供 match_profile 的 available_skills 用）。"""
    try:
        from core.assistant import skills as skills_mod
        return [str(s.get("slug") or "") for s in skills_mod.list_skills()
                if s.get("slug")]
    except Exception:
        return []
