# -*- coding: utf-8 -*-
"""默认技能的按需自动加载（core/assistant/defaults.py）。

回答三个问题，全部**纯本地、无网络**（一轮只多读一遍已装技能的 frontmatter）：

1. **什么样的技能会被自动加载？**
   frontmatter 里 `auto_load: true`（默认技能包自带；用户装的技能加一行即可），
   且未被用户放进 `auto_skill_off`，且属于「已安装」清单。

2. **按什么打分、什么时候算命中？**
   `触发词命中 ×3 + 技能名 token 命中 ×2 + 描述关键词命中 ×1`，
   阈值 `MIN_SCORE = 3`（= 至少一个触发词），最多取 `limit` 个（默认 2）。

3. **和已有的自动加载（profile）什么关系？**
   优先级（高 → 低）：
     ① 用户在底栏手动挂载的技能（`mounted_skills`）—— 用户显式选择，一票否决；
     ② 命中的 profile（`profiles.py`，带工具白名单的成组配置）；
     ③ 本模块的默认技能匹配；
     ④ L3 降级：把已装技能清单写进 system，让模型自己 `skill_run`。
   ①②命中时本模块**不再叠加**，避免两套指令互相打架（详见冲突策略文档）。

稳定性约定：
    · 匹配失败（技能目录不可读等）返回空结果，绝不让本轮对话失败；
    · 结果做确定性排序（分数 → priority → slug），同样的输入永远同样的输出；
    · 只影响 system 提示与 schema 顺序，**不裁剪工具**（见 bundled.py 的说明）。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 命中阈值：一个触发词即算命中（3 分）
MIN_SCORE = 3
#: 单轮最多自动加载几个默认技能
DEFAULT_LIMIT = 2
#: 硬上限（配置 `auto_skill_limit` 超过也按这个截）
MAX_LIMIT = 4
#: 打进 system 的技能正文摘录上限（字符）——只摘第一个命中的技能，
#: 其余只报名字，避免把上下文吃满
BODY_EXCERPT_CHARS = 900


def _norm(text: Any) -> str:
    return str(text or "").strip().lower()


def _as_list(raw: Any) -> List[str]:
    """frontmatter 里的 triggers 可能是 list / "a, b" / "a、b"。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.replace("，", ",").replace("、", ",")
        raw = [t for t in text.split(",")]
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: List[str] = []
    for item in raw:
        val = _norm(item)
        if val and val not in out:
            out.append(val)
    return out


def skill_tokens(slug: str) -> List[str]:
    """技能名切 token（`doc-writer` → doc/writer），用于「名字被提到」加分。"""
    raw = _norm(slug)
    parts = [p for p in raw.replace("_", "-").split("-") if len(p) >= 3]
    return parts


def score_skill(text: str, item: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """给单个技能打分，返回 `(score, 依据)`。

    Args:
        text: 用户本轮原文（已做小写化也可以，函数内部再兜一次）。
        item: `skills.list_skills()` 的一条，需要 `slug/name/description`，
            可选 `triggers`。

    Returns:
        `(score, {"triggers": [...], "name": bool, "desc": [...]})`
    """
    needle = _norm(text)
    why: Dict[str, Any] = {"triggers": [], "name": False, "desc": []}
    if not needle:
        return 0, why
    score = 0
    for trig in _as_list(item.get("triggers")):
        if trig and trig in needle:
            why["triggers"].append(trig)
            score += 3
    # 触发词过多时不再叠加：避免「文档」这种高频词把分数拉爆
    score = min(score, 9)

    slug = str(item.get("slug") or "")
    if any(tok in needle for tok in skill_tokens(slug)):
        why["name"] = True
        score += 2

    desc = _norm(item.get("description"))
    if desc:
        # 描述命中的口径：取长度 ≥2 的片段做子串，最多算 2 分
        hits = [w for w in _desc_terms(desc) if w in needle][:2]
        if hits:
            why["desc"] = hits
            score += len(hits)
    return score, why


def _desc_terms(desc: str) -> List[str]:
    """从描述里抽「可匹配片段」：中文按 2-6 字滑窗？成本太高。

    折中：按标点切段，取每段前 6 个字做子串（够用且稳），短段直接整段用。
    """
    out: List[str] = []
    for chunk in desc.replace("，", "|").replace(",", "|").replace("、", "|") \
                     .replace("；", "|").replace(";", "|").split("|"):
        piece = chunk.strip()
        if len(piece) < 2:
            continue
        out.append(piece[:6])
    return out[:6]


def candidates(items: Iterable[Dict[str, Any]],
               off: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
    """筛出可参与自动加载的技能（`auto_load` 且不在停用名单里）。"""
    off_set = {_norm(s) for s in (off or [])}
    out: List[Dict[str, Any]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        slug = str(it.get("slug") or "").strip()
        if not slug or slug in off_set:
            continue
        if not bool(it.get("auto_load")):
            continue
        out.append(it)
    return out


def select(text: str, items: Optional[Iterable[Dict[str, Any]]] = None,
           off: Optional[Iterable[str]] = None,
           limit: int = DEFAULT_LIMIT,
           min_score: int = MIN_SCORE) -> Dict[str, Any]:
    """挑出本轮要自动加载的默认技能。

    Args:
        text: 用户本轮原文。
        items: `skills.list_skills()` 结果；None/空 -> 返回空结果（调用方负责读盘）。
        off: 用户停用自动加载的 slug 集合（`config.auto_skill_off`）。
        limit: 最多几个（会被 clamp 到 `0..MAX_LIMIT`）。
        min_score: 命中阈值。

    Returns:
        `{"skills": [slug...], "matched": {slug: {...}}, "scores": {...},
          "limit": n}`；未命中时 `skills == []`。
    """
    try:
        want = int(limit)
    except (TypeError, ValueError):
        want = DEFAULT_LIMIT
    want = max(0, min(want, MAX_LIMIT))
    pool = candidates(items or [], off)
    if want == 0 or not pool:
        return {"skills": [], "matched": {}, "scores": {}, "limit": want}

    scored: List[Tuple[int, int, str, Dict[str, Any], Dict[str, Any]]] = []
    for it in pool:
        sc, why = score_skill(text, it)
        if sc < max(1, int(min_score)):
            continue
        try:
            pr = int(it.get("priority") or 0)
        except (TypeError, ValueError):
            pr = 0
        scored.append((sc, pr, str(it.get("slug") or ""), it, why))
    # 分数高的先；同分取 priority 高的；再同则按 slug 保证确定性
    scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
    picked = scored[:want]
    return {"skills": [p[2] for p in picked],
            "matched": {p[2]: p[4] for p in picked},
            "scores": {p[2]: p[0] for p in picked},
            "limit": want}


def excerpt(body: str, limit: int = BODY_EXCERPT_CHARS) -> str:
    """技能正文摘录（去空行、截断），用于注入 system。"""
    lines = [ln.rstrip() for ln in str(body or "").splitlines()]
    text = "\n".join(ln for ln in lines if ln.strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    return cut + "\n…（技能正文较长，已截断；需要完整步骤请调用 skill_run）"


def build_hint(picked: Dict[str, Any], metas: Dict[str, Dict[str, Any]],
               excerpt_body: str = "",
               disabled_tools: Optional[Iterable[str]] = None) -> str:
    """拼「本轮自动加载技能」的 system 段。

    Args:
        picked: `select()` 的返回值。
        metas: `{slug: {"name":…, "tools":[…], "description":…}}`。
        excerpt_body: 首个命中技能的正文摘录（为空则只报名字）。
        disabled_tools: 用户禁用的工具集合——技能建议的工具若被禁用，
            在这里明说，避免模型反复尝试一个不会下发的工具。
    """
    slugs: List[str] = list(picked.get("skills") or [])
    if not slugs:
        return ""
    disabled = {_norm(x) for x in (disabled_tools or [])}
    lines: List[str] = []
    for slug in slugs:
        meta = metas.get(slug) or {}
        name = str(meta.get("name") or slug)
        why = (picked.get("matched") or {}).get(slug) or {}
        trig: List[str] = list(why.get("triggers") or [])
        tail = f"（命中：{'、'.join(trig)}）" if trig else ""
        lines.append(f"- {slug}（{name}）{tail}")
        missing = [t for t in (meta.get("tools") or []) if _norm(t) in disabled]
        if missing:
            lines.append(f"  · 注意：建议使用的 {'、'.join(missing)} 已被用户禁用，"
                         "请改用可用工具或说明限制，不要反复尝试调用它们。")
    head = ("\n## 本轮自动加载的默认技能\n"
            "系统已按本轮需求自动选中下列技能（用户无需手动指定）。"
            "请**直接按技能要求执行**，不要调用 skill_list 反复枚举技能；"
            "需要完整步骤时可调用 skill_run 运行同名技能。\n")
    body = ""
    if excerpt_body:
        body = f"\n### 技能要点（{slugs[0]}）\n{excerpt_body}\n"
    return head + "\n".join(lines) + "\n" + body


def summarize(picked: Dict[str, Any], metas: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """给 `start.auto_loaded` 用的精简载荷（前端只用 name/slug）。"""
    slugs: List[str] = list(picked.get("skills") or [])
    if not slugs:
        return {}
    return {"skills": slugs,
            "names": [str((metas.get(s) or {}).get("name") or s) for s in slugs],
            "source": "default",
            "matched": {s: (picked.get("matched") or {}).get(s, {}) for s in slugs},
            "scores": {s: (picked.get("scores") or {}).get(s, 0) for s in slugs}}
