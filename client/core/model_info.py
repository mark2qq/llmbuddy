# -*- coding: utf-8 -*-
"""
模型说明 / 收费提示（client/core/model_info.py）。

背景（1.0.9 用户需求）：
    模型下拉里只列名字，用户不知道「哪个免费、哪个要钱、这个模型擅长什么」，
    选完才发现余额不足或答非所问。这里给每个模型名配一段说明与收费口径，
    客户端在菜单项上加「免费」标识 + 悬停提示（QMenu.setToolTipsVisible）。

1.0.10 修正（用户反馈：Qwen3-14B 明明收费 ¥0.5/¥2 却被标成「免费」）：
    旧实现是**手工维护**一份 `KNOWN_FREE` 清单，服务商一改价就跟事实脱节，
    还会和「价格区间」文案互相矛盾。现在改成**单一事实来源：价格表**——
        · `PRICE_SNAPSHOT` 是官方价格页（https://siliconflow.cn/models）
          SSR 数据里解析出来的「输入/输出 元 per 百万 tokens」快照；
        · `KNOWN_FREE` 由快照**推导**（输入与输出都为 0 才算免费），
          不再手写，杜绝「清单说有、价格页说收费」这种自相矛盾；
        · `set_live_pricing()` 允许运行期用在线抓取的最新价格覆盖快照，
          见 `client/core/model_pricing.py`（24h 缓存 + 失败回退快照）。

边界：
    - 免费判定**只认 0 价**。价格页没收录的模型一律按「按量计费」提示，
      宁可保守也不臆造免费。
    - 价格数字随服务商调价会变，快照带日期；界面上定期刷新即可。
"""
from typing import Any, Dict, Iterable, Optional, Tuple

#: 快照抓取日期（官方价格页 https://siliconflow.cn/models）
SNAPSHOT_DATE = "2026-10-26"

#: 官方价格页（模型 + 价格，SSR 内嵌 JSON）
PRICING_PAGE = "https://siliconflow.cn/models"

#: 官方价格快照：模型 ID → (输入价, 输出价)，单位「元 / 百万 tokens」。
#: 只收录 `subType=chat` 的文本模型（embedding / rerank / 生图 / ASR 另计）。
PRICE_SNAPSHOT: Dict[str, Tuple[float, float]] = {
    "ByteDance-Seed/Seed-OSS-36B-Instruct": (1.5, 4),
    "PaddlePaddle/PaddleOCR-VL-1.5": (0, 0),
    "Pro/Qwen/Qwen2.5-7B-Instruct": (0.35, 0.35),
    "Pro/deepseek-ai/DeepSeek-R1": (4, 16),
    "Pro/deepseek-ai/DeepSeek-V3": (2, 8),
    "Pro/deepseek-ai/DeepSeek-V3.1-Terminus": (4, 12),
    "Pro/deepseek-ai/DeepSeek-V3.2": (2, 3),
    "Pro/moonshotai/Kimi-K2.6": (6.5, 27),
    "Pro/zai-org/GLM-5.1": (6, 28),
    "Qwen/Qwen2.5-14B-Instruct": (0.7, 0.7),
    "Qwen/Qwen2.5-32B-Instruct": (1.26, 1.26),
    "Qwen/Qwen2.5-72B-Instruct": (4.13, 4.13),
    "Qwen/Qwen2.5-72B-Instruct-128K": (4.13, 4.13),
    "Qwen/Qwen2.5-7B-Instruct": (0, 0),
    "Qwen/Qwen3-14B": (0.5, 2),
    "Qwen/Qwen3-30B-A3B-Instruct-2507": (0.7, 2.8),
    "Qwen/Qwen3-32B": (1, 4),
    "Qwen/Qwen3-8B": (0, 0),
    "Qwen/Qwen3-Coder-30B-A3B-Instruct": (0.7, 2.8),
    "Qwen/Qwen3-Omni-30B-A3B-Captioner": (0.7, 2.8),
    "Qwen/Qwen3-Omni-30B-A3B-Instruct": (0.7, 2.8),
    "Qwen/Qwen3-Omni-30B-A3B-Thinking": (0.7, 2.8),
    "Qwen/Qwen3-VL-30B-A3B-Instruct": (0.7, 2.8),
    "Qwen/Qwen3-VL-30B-A3B-Thinking": (0.7, 2.8),
    "Qwen/Qwen3-VL-32B-Instruct": (1, 4),
    "Qwen/Qwen3-VL-32B-Thinking": (1, 10),
    "Qwen/Qwen3-VL-8B-Instruct": (0.5, 2),
    "Qwen/Qwen3-VL-8B-Thinking": (0.5, 5),
    "Qwen/Qwen3.5-122B-A10B": (2, 16),
    "Qwen/Qwen3.5-27B": (1.8, 14.4),
    "Qwen/Qwen3.5-35B-A3B": (1.6, 12.8),
    "Qwen/Qwen3.5-4B": (0, 0),
    "Qwen/Qwen3.5-9B": (1.5, 12),
    "Qwen/Qwen3.6-27B": (1.8, 14.4),
    "Qwen/Qwen3.6-35B-A3B": (1.6, 12.8),
    "Qwen/Qwen3.8-27B": (1.8, 14.4),
    "THUDM/GLM-4-32B-0414": (1.89, 1.89),
    "THUDM/GLM-4-9B-0414": (0, 0),
    "THUDM/GLM-Z1-9B-0414": (0, 0),
    "XingChenAGI/Xing4.0-29B": (0, 0),
    "deepseek-ai/DeepSeek-OCR": (0, 0),
    "deepseek-ai/DeepSeek-R1": (4, 16),
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B": (0, 0),
    "deepseek-ai/DeepSeek-V3": (2, 8),
    "deepseek-ai/DeepSeek-V3.1-Terminus": (4, 12),
    "deepseek-ai/DeepSeek-V3.2": (2, 3),
    "deepseek-ai/DeepSeek-V4-Flash": (1, 2),
    "deepseek-ai/DeepSeek-V4-Pro": (12, 24),
    "deepseek-ai/DeepSeek-V4.1-Flash": (2, 8),
    "inclusionAI/Ling-flash-2.0": (1, 4),
    "inclusionAI/Ling-mini-2.0": (0.5, 2),
    "meituan-longcat/LongCat-2.0": (5, 20),
    "moonshotai/Kimi-K2.7-Code": (6.5, 27),
    "stepfun-ai/Step-3.5-Flash": (0.7, 2.1),
    "tencent/Hunyuan-A13B-Instruct": (1, 4),
    "tencent/Hunyuan-MT-7B": (0, 0),
    "tencent/Hy4-preview": (6, 18),
    "zai-org/GLM-4.5-Air": (1, 6),
    "zai-org/GLM-4.5V": (1, 6),
    "zai-org/GLM-5.2": (6, 28),
    "zai-org/GLM-5.3": (6, 28),
    "zai-org/GLM-5.3-Flash": (0, 0),
}

#: 运行期在线抓取的价格（覆盖快照）；为空时只用快照。
_LIVE: Dict[str, Tuple[float, float]] = {}
#: 在线价格的抓取时间（人类可读），用于悬停提示里标「价格更新于 …」
_LIVE_AT = ""


def set_live_pricing(table: Optional[Dict[str, Any]],
                     fetched_at: str = "") -> None:
    """用在线抓取的价格覆盖快照（`model_pricing` 模块调用）。

    只接受 {模型: (输入, 输出)} 且两项都能转 float 的条目，脏数据直接跳过——
    价格页解析正则一旦因页面改版失配，绝不能把「免费」标错。
    """
    global _LIVE, _LIVE_AT
    clean: Dict[str, Tuple[float, float]] = {}
    for mid, pair in (table or {}).items():
        try:
            inp, out = float(pair[0]), float(pair[1])
        except Exception:
            continue
        if inp < 0 or out < 0:
            continue
        clean[str(mid).strip()] = (inp, out)
    _LIVE = clean
    _LIVE_AT = str(fetched_at or "")


def live_pricing_meta() -> Dict[str, Any]:
    """当前在线价格状态：{count, fetched_at}（未加载时 count=0）。"""
    return {"count": len(_LIVE), "fetched_at": _LIVE_AT}


def price_of(model_id: str) -> Optional[Tuple[float, float]]:
    """(输入价, 输出价) 元/百万 tokens；未收录返回 None。在线数据优先。"""
    mid = str(model_id or "").strip()
    if not mid:
        return None
    if mid in _LIVE:
        return _LIVE[mid]
    return PRICE_SNAPSHOT.get(mid)


def _is_zero(pair: Tuple[float, float]) -> bool:
    return pair[0] == 0 and pair[1] == 0


def free_models() -> Tuple[str, ...]:
    """当前认定免费的模型（在线数据优先，否则快照里 0 价的那批）。"""
    src = _LIVE or PRICE_SNAPSHOT
    return tuple(sorted(m for m, p in src.items() if _is_zero(p)))


#: 兼容旧引用点（chat_page / assistant_page 曾直接 import 这个元组）。
#: 注意：它是**快照推导的结果**，不包含运行期在线更新；判断请用 `is_free()`。
KNOWN_FREE: Tuple[str, ...] = tuple(
    sorted(m for m, p in PRICE_SNAPSHOT.items() if _is_zero(p)))

#: 家族 →（显示名，能力简介）。按顺序匹配 model id 的小写形式。
_FAMILIES: Tuple[Tuple[str, str, str], ...] = (
    ("deepseek-r1", "DeepSeek R1", "深度推理模型：数学、代码、逻辑强，会先思考再作答，"
                                   "响应偏慢、吃 token。"),
    ("deepseek-ocr", "DeepSeek OCR", "文档识别模型：把扫描件 / 截图 / 表格转成结构化文本。"),
    ("deepseek-v3", "DeepSeek V3", "通用大模型：中英写作、代码、工具调用均衡，速度较快。"),
    ("deepseek-v4", "DeepSeek V4", "DeepSeek 新一代通用模型，综合能力更强、价格更高。"),
    ("deepseek", "DeepSeek", "DeepSeek 系列通用大模型。"),
    ("qwen3-vl", "Qwen3-VL", "多模态模型：可读图（截图、图表、扫描件）。"),
    ("qwen3-omni", "Qwen3-Omni", "全模态模型：文本 / 图像 / 音频输入。"),
    ("qwen3-coder", "Qwen3 Coder", "代码专用：写代码、读仓库、改 bug。"),
    ("qwen3", "Qwen3", "通义千问 Qwen3 系列：通用对话与写作，部分型号默认开启思考模式"
                       "（回复慢、token 消耗高）。"),
    ("qwen2.5-coder", "Qwen2.5 Coder", "代码专用模型，适合写代码 / 读仓库。"),
    ("qwen2.5", "Qwen2.5", "通义千问 Qwen2.5 系列：通用对话与写作。"),
    ("qwen", "Qwen", "通义千问系列通用模型。"),
    ("glm-z1", "GLM-Z1", "智谱推理模型，先思考后作答。"),
    ("glm", "GLM", "智谱 GLM 系列：中文理解与工具调用表现好。"),
    ("llama", "Llama", "Meta Llama 系列开源模型：英文与代码能力均衡。"),
    ("kimi", "Kimi", "月之暗面 Kimi 系列：长文本阅读与总结见长。"),
    ("internlm", "InternLM", "书生·浦语系列开源模型。"),
    ("step-", "Step", "阶跃星辰 Step 系列。"),
    ("hunyuan-mt", "混元翻译", "翻译专用模型：中英等多语互译，专款专用。"),
    ("hunyuan", "混元", "腾讯混元系列。"),
    ("minicpm", "MiniCPM", "面壁 MiniCPM 系列轻量模型。"),
    ("paddleocr", "PaddleOCR-VL", "OCR / 视觉理解模型：适合识别图片里的文字与版面。"),
    ("seed-oss", "Seed-OSS", "字节 Seed 开源系列。"),
    ("longcat", "LongCat", "美团 LongCat 系列长上下文模型。"),
    ("xing", "星尘", "星尘（XingChen）系列模型。"),
    ("ling-", "Ling", "蚂蚁 Ling 系列模型。"),
)

_PRICE_LOCAL = "免费：本地引擎推理，不计费（速度取决于本机显存 / 内存）"
_PRICE_NO_KEY = "免费：该端点无需 API Key（第三方公益聚合服务，限流较低、勿传敏感数据）"


def is_free(model_id: str) -> bool:
    """是否**确认**免费：在线/快照价格里输入输出都为 0。

    未收录的模型一律返回 False（按量计费），不做任何「名字里带 free 就算免费」
    之类的猜测——1.0.9 的 `"free" in id` 启发式正是误标来源之一。
    """
    pair = price_of(model_id)
    return bool(pair) and _is_zero(pair)


def family_of(model_id: str) -> Tuple[str, str]:
    """(家族显示名, 能力简介)；匹配不到时返回 ("通用模型", "...")。"""
    low = str(model_id or "").lower()
    for key, label, desc in _FAMILIES:
        if key in low:
            return label, desc
    return "通用模型", "OpenAI 兼容的通用对话模型。"


def price_text(model_id: str) -> str:
    """收费口径文案：免费 / 具体单价 / 未收录。"""
    pair = price_of(model_id)
    if pair is None:
        return ("未收录价格（按量计费，以服务商价格页为准）："
                f"{PRICING_PAGE}")
    if _is_zero(pair):
        return "免费：官方标价 0 元（赠送额度，限流较低，忙时可能 503）"
    inp, out = pair
    if inp == out:
        return f"按量计费：¥{inp:g} / 百万 tokens（输入输出同价，以价格页为准）"
    return (f"按量计费：输入 ¥{inp:g}、输出 ¥{out:g} 每百万 tokens"
            f"（官方价格页口径，会随调价变化）")


def price_tag(model_id: str) -> str:
    """菜单右侧的短标签：`免费` / `¥0.5→2` / 空串（未收录）。"""
    pair = price_of(model_id)
    if pair is None:
        return ""
    if _is_zero(pair):
        return "免费"
    inp, out = pair
    if inp == out:
        return f"¥{inp:g}/百万"
    return f"¥{inp:g}→{out:g}"


def describe(model_id: str, local: bool = False,
             keyless: bool = False) -> Dict[str, Any]:
    """返回 {name, free, family, desc, price, tooltip}，供菜单标识与悬停提示用。"""
    mid = str(model_id or "").strip()
    free = is_free(mid) and not local
    family, desc = family_of(mid)
    if local:
        price = _PRICE_LOCAL
    elif keyless:
        price = _PRICE_NO_KEY
    else:
        price = price_text(mid)
    lines = [mid]
    if family and family not in ("通用模型",):
        lines.append(f"类型：{family}")
    lines.append(f"能力：{desc}")
    lines.append(f"收费：{price}")
    meta = live_pricing_meta()
    if meta.get("count") and not local:
        lines.append(f"价格数据：在线（{meta.get('fetched_at') or '最近一次刷新'}）")
    elif not local and mid in PRICE_SNAPSHOT:
        lines.append(f"价格数据：内置快照（{SNAPSHOT_DATE}）")
    return {
        "name": mid,
        "free": free,
        "family": family,
        "desc": desc,
        "price": price,
        "tooltip": "\n".join(lines),
    }


def menu_label(model_id: str, local: bool = False,
               keyless: bool = False) -> str:
    """菜单显示文本：免费追加「免费」，收费追加真实单价。

    1.0.10：收费模型也带上价格（`Qwen/Qwen3-14B    ¥0.5→2`），
    这样「是不是免费」一眼可辨，不用点开 tooltip 才发现要钱。
    """
    mid = str(model_id or "")
    if local:
        return f"{mid}    本地·免费"
    if keyless:
        return f"{mid}    免 Key"
    tag = price_tag(mid)
    return f"{mid}    {tag}" if tag else mid


def free_only(pairs: Iterable[Tuple[str, str]]) -> list:
    """按「免费优先」重排 [(model, tag), ...] 的通用工具（对话页置顶用）。"""
    items = list(pairs)
    return ([p for p in items if p[1] == "免费"]
            + [p for p in items if p[1] != "免费"])
