# -*- coding: utf-8 -*-
"""模型价格抓取 / 缓存 / 免费推导的回归测试（1.0.10）。

背景：用户拿价格页截图反馈「Qwen3-14B 明明 ¥0.5/¥2 却被客户端标成免费」。
根因是「免费」靠手工清单维护。修法改成：抓官方价格页的 SSR 数据 → 0 价才算免费。
这里锁三件事：
    ① 解析器能从真实结构里抠出价格，且遇到脏数据**不瞎猜**（宁可为空）；
    ② 缓存能落盘/读回，过期不生效；
    ③ 免费清单由价格表推导，收费模型不会被误标。
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import model_info, model_pricing  # noqa: E402


def _page(*models) -> str:
    """构造与官方价格页同构的片段：JS 字符串里塞**转义过**的 JSON。

    models 每项：(模型名, subType, 输入价, 输出价)
    """
    parts = []
    for name, sub, inp, out in models:
        obj = {"modelName": name, "subType": sub,
               "pricing": [{"price": inp, "specification": "prompt",
                            "unitOfGood": "/ M Tokens"},
                           {"price": out, "specification": "completion",
                            "unitOfGood": "/ M Tokens"}]}
        parts.append(json.dumps(obj, ensure_ascii=False).replace('"', '\\"'))
    return '<script>self.__next_f.push([1,"%s"])</script>' % "".join(parts)


def test_parse_pricing_reads_real_shape():
    html = _page(("Qwen/Qwen3-14B", "chat", "0.5", "2"),
                 ("Qwen/Qwen3-8B", "chat", "0", "0"),
                 ("zai-org/GLM-5.3-Flash", "chat", "0", "0"))
    table = model_pricing.parse_pricing(html)
    assert table["Qwen/Qwen3-14B"] == (0.5, 2.0)
    assert table["Qwen/Qwen3-8B"] == (0.0, 0.0)
    assert table["zai-org/GLM-5.3-Flash"] == (0.0, 0.0)


def test_parse_pricing_skips_non_chat_and_bad_values():
    html = _page(("BAAI/bge-m3", "embedding", "0", "0"),      # 非 chat：跳过
                 ("Qwen/Qwen3-8B", "chat", "0", "0"),
                 ("broken/model", "chat", "??", "1"))          # 价格不可解析：跳过
    table = model_pricing.parse_pricing(html)
    assert "BAAI/bge-m3" not in table
    assert "broken/model" not in table
    assert table["Qwen/Qwen3-8B"] == (0.0, 0.0)


def test_parse_pricing_returns_empty_on_unknown_html():
    """页面改版 / 拿到错误页时返回空表——绝不能把「解析失败」当成「全都免费」。"""
    assert model_pricing.parse_pricing("") == {}
    assert model_pricing.parse_pricing("<html>maintenance</html>") == {}


def test_free_models_derived_from_prices():
    """免费 = 输入输出都为 0；收费的一个都不能混进来。"""
    free = model_info.free_models()
    assert "Qwen/Qwen3-8B" in free
    assert "zai-org/GLM-5.3-Flash" in free
    for paid in ("Qwen/Qwen3-14B", "Qwen/Qwen3-32B", "deepseek-ai/DeepSeek-R1",
                 "deepseek-ai/DeepSeek-V3"):
        assert paid not in free


def test_live_pricing_overrides_snapshot():
    """在线价格优先于内置快照（服务商调价后不用等新版本）。"""
    old = model_info.price_of("Qwen/Qwen3-8B")
    try:
        model_info.set_live_pricing({"Qwen/Qwen3-8B": (1.0, 1.0)}, "2026-10-26 16:00")
        assert model_info.price_of("Qwen/Qwen3-8B") == (1.0, 1.0)
        assert not model_info.is_free("Qwen/Qwen3-8B")      # 涨价后不再免费
        assert model_info.live_pricing_meta()["count"] == 1
    finally:
        model_info.set_live_pricing({}, "")
    assert model_info.price_of("Qwen/Qwen3-8B") == old


def test_live_pricing_rejects_dirty_rows():
    """脏数据（负数 / 非数字）直接丢掉，避免把「免费」标错。"""
    model_info.set_live_pricing({"a/b": (-1, 2), "c/d": ("x", "y")}, "t")
    try:
        assert model_info.price_of("a/b") is None
        assert model_info.price_of("c/d") is None
        assert model_info.live_pricing_meta()["count"] == 0
    finally:
        model_info.set_live_pricing({}, "")


def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    """缓存能读回；超过 TTL 不生效（回退快照）。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    table = {"Qwen/Qwen3-8B": (0.0, 0.0), "Qwen/Qwen3-14B": (0.5, 2.0)}
    model_pricing._write_cache(table)
    assert os.path.exists(model_pricing.cache_path())
    try:
        assert model_pricing.apply_cached() == 2
        assert model_info.price_of("Qwen/Qwen3-14B") == (0.5, 2.0)
        # TTL 设成极小 → 视为过期，不再注入
        model_info.set_live_pricing({}, "")
        assert model_pricing.apply_cached(max_age_hours=1e-9) == 0
        assert model_info.live_pricing_meta()["count"] == 0
    finally:
        model_info.set_live_pricing({}, "")


def test_cache_path_under_user_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = model_pricing.cache_path()
    assert path.endswith(os.path.join("llmBuddy", "cache", "siliconflow_pricing.json"))
    assert str(tmp_path) in path


def test_snapshot_is_consistent_with_known_free():
    """兼容常量 KNOWN_FREE 必须是快照推导出来的（防止有人又手写一份）。"""
    import importlib
    mod = importlib.reload(model_info)
    try:
        expected = tuple(sorted(m for m, p in mod.PRICE_SNAPSHOT.items()
                                if p == (0, 0)))
        assert mod.KNOWN_FREE == expected
        assert "Qwen/Qwen3-14B" not in mod.KNOWN_FREE
    finally:
        importlib.reload(model_info)


@pytest.mark.parametrize("mid,free", [
    ("Qwen/Qwen3-8B", True),
    ("Qwen/Qwen3-14B", False),
    ("Qwen/Qwen3-32B", False),
    ("deepseek-ai/DeepSeek-R1-0528-Qwen3-8B", True),
    ("deepseek-ai/DeepSeek-R1", False),
    ("THUDM/GLM-4-9B-0414", True),
    ("THUDM/GLM-Z1-9B-0414", True),
    ("tencent/Hunyuan-MT-7B", True),
    ("Qwen/Qwen3.5-4B", True),
    ("XingChenAGI/Xing4.0-29B", True),
    ("deepseek-ai/DeepSeek-OCR", True),
    ("PaddlePaddle/PaddleOCR-VL-1.5", True),
    ("deepseek-ai/DeepSeek-V4.1-Flash", False),
    ("Pro/zai-org/GLM-5.1", False),
])
def test_free_flag_matches_official_price(mid, free):
    assert model_info.is_free(mid) is free
