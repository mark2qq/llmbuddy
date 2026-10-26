# -*- coding: utf-8 -*-
"""
本机「免 Key」OpenAI 兼容端点探测（client/core/local_endpoints.py）。

1.0.10 用户需求：除了要 Key 的云平台，还想知道「有没有完全免费、不用 Key、
能直接对接」的模型。答案有两类，这是第二类——**本机已经在跑的开源服务**：

    Ollama      http://127.0.0.1:11434/v1   （`ollama serve` 默认）
    LM Studio   http://127.0.0.1:1234/v1    （Local Server 默认）

两者都自带 OpenAI 兼容 `/v1`，不需要任何 Key，模型完全跑在本机。
本模块只负责「探测 + 列出可用模型」，落库/启用交给调用方。

注意端口冲突：8000 是 llmBuddy 自己的网关，必须排除，否则会把自家网关
当成本机免 Key 端点接进来（死循环）。
"""
from typing import Any, Dict, List, Tuple

#: (provider_id, 显示名, OpenAI 兼容根地址)
LOCAL_ENDPOINTS: Tuple[Tuple[str, str, str], ...] = (
    ("ollama", "Ollama", "http://127.0.0.1:11434/v1"),
    ("lmstudio", "LM Studio", "http://127.0.0.1:1234/v1"),
)


def probe(timeout: float = 1.5) -> List[Dict[str, Any]]:
    """探测本机端点，返回 [{id, label, base_url, models: [...]}]。

    只返回**真的能用**的（`/models` 返回 200 且至少有一个模型）。
    探测失败/超时一律静默跳过：这是「顺手发现」，不该弹错误打扰用户。
    """
    import httpx

    found: List[Dict[str, Any]] = []
    for pid, label, base in LOCAL_ENDPOINTS:
        try:
            # 本机请求，绝不能被系统代理劫持（本机常见的死代理问题）
            with httpx.Client(trust_env=False, timeout=timeout) as c:
                r = c.get(f"{base}/models")
            if r.status_code >= 400:
                continue
            data = r.json() or {}
            items = data.get("data")
            if not isinstance(items, list):
                continue
            models = [str(it.get("id")) for it in items
                      if isinstance(it, dict) and it.get("id")]
            if not models:
                continue
            found.append({"id": pid, "label": label, "base_url": base,
                          "models": models})
        except Exception:
            continue
    return found
