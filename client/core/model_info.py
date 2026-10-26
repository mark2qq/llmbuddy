# -*- coding: utf-8 -*-
"""
模型说明 / 收费提示（client/core/model_info.py）。

背景（1.0.9 用户需求）：
    模型下拉里只列名字，用户不知道「哪个免费、哪个要钱、这个模型擅长什么」，
    选完才发现余额不足或答非所问。这里给每个模型名配一段说明与收费口径，
    客户端在菜单项上加「免费」标识 + 悬停提示（QMenu.setToolTipsVisible）。

数据来源与边界：
    - **是否免费**：以 `KNOWN_FREE` 清单为准（SiliconFlow 官方标注免费的那批）。
      `GET /v1/models` 只返回 id / owned_by，不带价格字段，所以只能靠清单；
      清单没命中的一律按「按量计费」提示，不臆造免费。
    - **介绍**：按模型家族给一句能力描述（不涉及参数量等易变细节）。
    - **价格**：仅给区间口径并注明「以服务商价格页为准」，不写死具体数字——
      价格会变，写死反而误导。
"""
from typing import Any, Dict, Tuple

#: SiliconFlow 官方标注「免费」的模型 ID（与 chat_page 共用，勿重复维护）
KNOWN_FREE: Tuple[str, ...] = (
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-32B",
    "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    "deepseek-ai/DeepSeek-R1",
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
    "deepseek-ai/DeepSeek-V3",
    "THUDM/glm-4-9b-chat",
    "THUDM/GLM-4-9B-0414",
    "meta-llama/Llama-3.1-8B-Instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct",
)

#: 家族 →（显示名，能力简介）。按顺序匹配 model id 的小写形式。
_FAMILIES: Tuple[Tuple[str, str, str], ...] = (
    ("deepseek-r1", "DeepSeek R1", "深度推理模型：数学、代码、逻辑强，会先思考再作答，"
                                   "响应偏慢、吃 token。"),
    ("deepseek-v3", "DeepSeek V3", "通用大模型：中英写作、代码、工具调用均衡，速度较快。"),
    ("deepseek", "DeepSeek", "DeepSeek 系列通用大模型。"),
    ("qwen3-vl", "Qwen3-VL", "多模态模型：可读图（截图、图表、扫描件）。"),
    ("qwen3-omni", "Qwen3-Omni", "全模态模型：文本 / 图像 / 音频输入。"),
    ("qwen3-coder", "Qwen3 Coder", "代码专用：写代码、读仓库、改 bug。"),
    ("qwen3", "Qwen3", "通义千问 Qwen3 系列：通用对话与写作，部分型号默认开启思考模式"
                       "（回复慢、token 消耗高）。"),
    ("qwen2.5-coder", "Qwen2.5 Coder", "代码专用模型，适合写代码 / 读仓库。"),
    ("qwen2.5", "Qwen2.5", "通义千问 Qwen2.5 系列：通用对话与写作。"),
    ("qwen", "Qwen", "通义千问系列通用模型。"),
    ("glm", "GLM", "智谱 GLM 系列：中文理解与工具调用表现好。"),
    ("llama", "Llama", "Meta Llama 系列开源模型：英文与代码能力均衡。"),
    ("kimi", "Kimi", "月之暗面 Kimi 系列：长文本阅读与总结见长。"),
    ("internlm", "InternLM", "书生·浦语系列开源模型。"),
    ("step-", "Step", "阶跃星辰 Step 系列。"),
    ("hunyuan", "混元", "腾讯混元系列。"),
    ("minicpm", "MiniCPM", "面壁 MiniCPM 系列轻量模型。"),
    ("paddleocr", "PaddleOCR-VL", "OCR / 视觉理解模型：适合识别图片里的文字与版面。"),
    ("glm-z1", "GLM-Z1", "智谱推理模型，先思考后作答。"),
    ("seed-oss", "Seed-OSS", "字节 Seed 开源系列。"),
    ("longcat", "LongCat", "美团 LongCat 系列长上下文模型。"),
    ("xingchen", "星尘", "星尘（XingChen）系列模型。"),
)

#: 价格口径（不写死数字，只给区间与出处）
_PRICE_FREE = "免费：SiliconFlow 官方赠送额度，不扣费（限流较低，忙时可能 503）"
_PRICE_PAID = "按量计费：输入/输出分别按 token 计价（约 ¥1~20 / 百万 tokens，以服务商价格页为准）"
_PRICE_LOCAL = "免费：本地引擎推理，不计费（速度取决于本机显存 / 内存）"


def is_free(model_id: str) -> bool:
    """是否免费模型（清单命中，或 id 里带 free 字样）。"""
    mid = str(model_id or "").strip()
    if not mid:
        return False
    if mid in KNOWN_FREE:
        return True
    return "free" in mid.lower()


def family_of(model_id: str) -> Tuple[str, str]:
    """(家族显示名, 能力简介)；匹配不到时返回 ("通用模型", "...")。"""
    low = str(model_id or "").lower()
    for key, label, desc in _FAMILIES:
        if key in low:
            return label, desc
    return "通用模型", "OpenAI 兼容的通用对话模型。"


def describe(model_id: str, local: bool = False) -> Dict[str, Any]:
    """返回 {name, free, family, desc, price, tooltip}，供菜单标识与悬停提示用。"""
    mid = str(model_id or "").strip()
    free = is_free(mid) and not local
    family, desc = family_of(mid)
    if local:
        price = _PRICE_LOCAL
    elif free:
        price = _PRICE_FREE
    else:
        price = _PRICE_PAID
    lines = [mid]
    if family and family not in ("通用模型",):
        lines.append(f"类型：{family}")
    lines.append(f"能力：{desc}")
    lines.append(f"收费：{price}")
    return {
        "name": mid,
        "free": free,
        "family": family,
        "desc": desc,
        "price": price,
        "tooltip": "\n".join(lines),
    }


def menu_label(model_id: str, local: bool = False) -> str:
    """菜单显示文本：免费模型追加「免费」标识。"""
    mid = str(model_id or "")
    return f"{mid}    免费" if (is_free(mid) and not local) else mid
