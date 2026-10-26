# -*- coding: utf-8 -*-
"""
在线模型价格抓取与缓存（client/core/model_pricing.py）。

为什么需要它（1.0.10，用户反馈「明明收费却标成免费」）：
    `GET /v1/models` 只回 id / owned_by，**不带价格**，所以旧实现只能手工维护
    一份「免费清单」，服务商一调价就跟事实脱节。真正的事实来源是官方价格页
    https://siliconflow.cn/models —— 它是 Next.js SSR，页面里直接内嵌了每个
    模型的 `pricing` 明细（prompt / completion 两条，单位 ¥ / M Tokens）。

做法：
    · `parse_pricing(html)` 从 SSR 内嵌 JSON 里抠出 {模型: (输入, 输出)}，
      解析失败返回空 dict（**绝不猜**，坏的解析结果会把「免费」标错）；
    · `refresh()` 抓页面 → 解析 → 落盘缓存（24h）→ 注入 `model_info`；
    · `apply_cached()` 启动时读缓存（不联网），没有缓存就静默用内置快照。

与 `model_info` 的分工：本模块只管「拿到数字」，判断免费 / 生成菜单文案
全部在 `model_info`，两处不会各自维护一套口径。
"""
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from core import model_info

#: 价格页（SSR，内嵌模型 + 价格 JSON）
PRICING_PAGE = model_info.PRICING_PAGE

#: 缓存有效期（小时）：价格不会天天变，24h 足够，也避免每次开菜单都联网
CACHE_TTL_HOURS = 24

#: 抓取超时（秒）。页面约 480KB，够用且不拖住界面
FETCH_TIMEOUT = 25.0

# 正则统一容忍 `": "` 这类多余空白：官方页面目前是紧凑格式（`"k":"v"`），
# 但 Next.js 输出格式、压缩插件都可能变，不能因为多了个空格就解析不出来。
_MODEL_RE = re.compile(r'"modelName"\s*:\s*"')
_NAME_RE = re.compile(r'"modelName"\s*:\s*"([^"]+)"')
_SUB_RE = re.compile(r'"subType"\s*:\s*"([^"]*)"')
_PRICING_RE = re.compile(r'"pricing"\s*:\s*\[')
_PRICE_RE = re.compile(r'\{\s*"price"\s*:\s*"([^"]*)"\s*,\s*"specification"\s*:\s*"([^"]*)"')


def cache_path() -> str:
    """价格缓存文件路径（与其它用户数据同放 %LOCALAPPDATA%/llmBuddy）。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "llmBuddy", "cache", "siliconflow_pricing.json")


def parse_pricing(html: str) -> Dict[str, Tuple[float, float]]:
    """从价格页 HTML 解析 {模型: (输入价, 输出价)}（元 / 百万 tokens）。

    页面里每个模型是一段被转义的 JSON（`\\"modelName\\":\\"...\\"`），同一段里
    带 `"pricing":[{"price":"0.5","specification":"prompt"}, ...]`。
    先按 `modelName` 切段，再在每段里取 prompt / completion 两个价。

    只收 `subType=chat` 且两个价都能解析的条目；解析不出价格的一律丢弃
    （调用方会回退到内置快照，而不是把未知当免费）。
    """
    text = str(html or "")
    if not text:
        return {}
    # SSR 里是转义过的 JSON 字符串，统一还原后再按段切
    text = text.replace('\\"', '"')
    starts = [m.start() for m in _MODEL_RE.finditer(text)]
    out: Dict[str, Tuple[float, float]] = {}
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        seg = text[start:end]
        m = _NAME_RE.match(seg)
        if not m:
            continue
        name = m.group(1).strip()
        if not name:
            continue
        # 只要 chat 模型：其它类型（embedding/rerank/生图/ASR）计费单位不同
        sub = _SUB_RE.search(seg)
        if sub and sub.group(1) != "chat":
            continue
        pm = _PRICING_RE.search(seg)
        if not pm:
            continue
        specs: Dict[str, str] = {}
        # pricing 数组的右括号：从数组起点找第一个 `]` 即可（内部只有扁平对象）
        close = seg.find("]", pm.end())
        body = seg[pm.end():close if close >= 0 else len(seg)]
        for p in _PRICE_RE.finditer(body):
            specs[p.group(2)] = p.group(1)
        if "prompt" not in specs or "completion" not in specs:
            continue
        try:
            inp = float(specs["prompt"])
            oc = float(specs["completion"])
        except (TypeError, ValueError):
            continue
        if inp < 0 or oc < 0:
            continue
        out[name] = (inp, oc)
    return out


def _read_cache() -> Optional[Dict[str, Any]]:
    try:
        with open(cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("table"), dict):
            return data
    except Exception:
        pass
    return None


def _write_cache(table: Dict[str, Tuple[float, float]]) -> None:
    path = cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(),
                       "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
                       "source": PRICING_PAGE,
                       "table": {k: [v[0], v[1]] for k, v in table.items()}},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def _apply(table: Dict[str, Tuple[float, float]], fetched_at: str = "") -> None:
    """注入 model_info（空表不注入，保证「宁可没有也不标错」）。"""
    if table:
        model_info.set_live_pricing(table, fetched_at=fetched_at)


def apply_cached(max_age_hours: float = CACHE_TTL_HOURS) -> int:
    """启动时用缓存里的价格（不联网）。返回注入的模型数，0 表示无可用缓存。"""
    data = _read_cache()
    if not data:
        return 0
    if max_age_hours and max_age_hours > 0:
        age = (time.time() - float(data.get("ts") or 0)) / 3600.0
        if age > max_age_hours:
            return 0
    table: Dict[str, Tuple[float, float]] = {}
    for mid, pair in (data.get("table") or {}).items():
        try:
            table[str(mid)] = (float(pair[0]), float(pair[1]))
        except Exception:
            continue
    _apply(table, str(data.get("fetched_at") or ""))
    return len(table)


def fetch(timeout: float = FETCH_TIMEOUT) -> Dict[str, Tuple[float, float]]:
    """抓官方价格页并解析。网络/解析失败抛异常（由调用方给用户提示）。"""
    import httpx
    # 官方站是国内站，直连即可；trust_env=False 避开本机那个常年失效的代理
    with httpx.Client(trust_env=False, timeout=timeout,
                      follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (llmBuddy)"}) as c:
        r = c.get(PRICING_PAGE)
    if r.status_code >= 400:
        raise RuntimeError(f"价格页返回 HTTP {r.status_code}")
    table = parse_pricing(r.text)
    if not table:
        # 页面改版会让解析正则失配——此时保持旧数据，不能当成「全都不免费」
        raise RuntimeError("价格页结构变化，未能解析出任何价格（已保留原有数据）")
    return table


def refresh(timeout: float = FETCH_TIMEOUT) -> Dict[str, Any]:
    """联网刷新价格：{count, fetched_at, free}。抛异常表示失败。"""
    table = fetch(timeout=timeout)
    fetched_at = time.strftime("%Y-%m-%d %H:%M")
    _write_cache(table)
    _apply(table, fetched_at)
    free = [m for m, p in table.items() if p[0] == 0 and p[1] == 0]
    return {"count": len(table), "fetched_at": fetched_at,
            "free": sorted(free)}


def ensure_fresh(max_age_hours: float = CACHE_TTL_HOURS) -> bool:
    """有新鲜缓存就直接用，否则尝试刷新一次。返回是否拿到了在线数据。"""
    if apply_cached(max_age_hours=max_age_hours):
        return True
    try:
        refresh()
        return True
    except Exception:
        return False
