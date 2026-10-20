# -*- coding: utf-8 -*-
"""
模型注册表加载 / 合并 / 持久化 + API Key 生成。

注册表 = 内置默认（BUILTIN_REGISTRY）+ models_dir 扫描 + 下载清单（cache/registry.json）
三者合并，对外提供「模型名 -> GGUF 信息」的解析能力。
所有持久化均为纯 JSON 文件，不引入数据库。
"""

import os
import sys
import json
import time
import secrets
import threading
from typing import Dict, Any, Optional, List

# ----------------------------------------------------------------------------
# 路径常量
# ----------------------------------------------------------------------------
# 本文件位于 E:/llm-service/core/config.py，BASE_DIR 即 E:/llm-service
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.environ.get("LLM_MODELS_DIR", r"E:\llama-serve\models")

# 用户数据目录名（%LOCALAPPDATA% 下）：2026-10-20 起由 LLMDesktop 更名为 llmBuddy。
DATA_DIRNAME = "llmBuddy"
LEGACY_DATA_DIRNAME = "LLMDesktop"


def user_data_dir() -> str:
    """用户数据目录（打包态）：%LOCALAPPDATA%\\llmBuddy。

    与客户端 app_config._user_data_dir 行为对齐：新目录优先；若新目录尚
    不存在而旧目录（LLMDesktop）存在（例如后端先于客户端启动、或迁移失败），
    沿用旧目录，保证缓存/日志不丢。
    """
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    new_dir = os.path.join(local, DATA_DIRNAME)
    old_dir = os.path.join(local, LEGACY_DATA_DIRNAME)
    if not os.path.isdir(new_dir) and os.path.isdir(old_dir):
        return old_dir
    return new_dir


def _default_cache_dir() -> str:
    """缓存目录：打包态 _internal 只读，改用 %LOCALAPPDATA%\\llmBuddy\\cache；
    可用 LLM_CACHE_DIR 覆盖；开发态用仓库 cache/。"""
    override = os.environ.get("LLM_CACHE_DIR")
    if override:
        return override
    if getattr(sys, "frozen", False):
        return os.path.join(user_data_dir(), "cache")
    return os.path.join(BASE_DIR, "cache")


CACHE_DIR = _default_cache_dir()
REGISTRY_PATH = os.path.join(CACHE_DIR, "registry.json")
KEYS_PATH = os.path.join(CACHE_DIR, "keys.json")

# ----------------------------------------------------------------------------
# 内置默认注册表：模型名 -> GGUF 文件名（相对 MODELS_DIR）
# 保留原 serve.py 的 3 个模型，作为开箱即用的本地已知模型
# ----------------------------------------------------------------------------
BUILTIN_REGISTRY: Dict[str, Dict[str, Any]] = {
    "qwen2.5-7b-instruct": {"gguf": "qwen2.5-7b-instruct.gguf", "source": "builtin"},
    "qwen2.5-coder-7b": {"gguf": "qwen2.5-coder-7b.gguf", "source": "builtin"},
    "qwen3-4b": {"gguf": "qwen3-4b.gguf", "source": "builtin"},
}

_lock = threading.Lock()


def _ensure_cache() -> None:
    """确保缓存目录存在。"""
    os.makedirs(CACHE_DIR, exist_ok=True)


def load_registry() -> Dict[str, Dict[str, Any]]:
    """
    加载并合并模型注册表，返回 模型名 -> 信息字典 的映射。
    合并顺序：内置默认 -> models_dir 扫描 -> cache/registry.json（下载/手动添加）。
    """
    reg: Dict[str, Dict[str, Any]] = {}
    # 1) 内置默认
    for name, info in BUILTIN_REGISTRY.items():
        reg[name] = {"name": name, "gguf": info["gguf"], "source": info.get("source", "builtin")}
    # 2) models_dir 扫描：目录下所有 .gguf 自动注册
    try:
        if os.path.isdir(MODELS_DIR):
            for fn in os.listdir(MODELS_DIR):
                if fn.lower().endswith(".gguf"):
                    name = os.path.splitext(fn)[0]
                    reg.setdefault(name, {"name": name, "gguf": fn, "source": "scan"})
    except Exception:
        pass
    # 3) 下载/手动添加的清单（优先级最高，覆盖同名）
    _ensure_cache()
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, info in data.items():
                reg[name] = {"name": name, **info}
        except Exception:
            pass
    return reg


def is_registered(name: str) -> bool:
    """模型是否已注册（已知）。"""
    return name in load_registry()


def resolve_gguf(name: str) -> str:
    """
    解析模型名对应的 GGUF 绝对路径。
    未注册抛 KeyError（1001）；已注册但文件缺失抛 FileNotFoundError（1002）。
    """
    reg = load_registry()
    if name not in reg:
        raise KeyError(name)
    gguf = reg[name].get("gguf", f"{name}.gguf")
    path = gguf if os.path.isabs(gguf) else os.path.join(MODELS_DIR, gguf)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return path


def register_model(name: str, gguf_path: str, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记录一个已下载/已添加的模型到 cache/registry.json（持久化）。"""
    _ensure_cache()
    data: Dict[str, Any] = {}
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    entry: Dict[str, Any] = {"gguf": gguf_path, "source": "download"}
    if meta:
        for k, v in meta.items():
            if k != "name":
                entry[k] = v
    data[name] = entry
    with _lock:
        with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return entry


def _load_keys() -> List[Dict[str, Any]]:
    """读取 keys.json 全部条目，并对旧条目补 "model": "" 默认值。

    keys.json 不存在或解析失败时返回空列表（不抛异常，避免影响鉴权）。
    """
    if not os.path.exists(KEYS_PATH):
        return []
    try:
        with open(KEYS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        # 兼容旧条目（缺 model 视作全局 Key）
        for it in data:
            if isinstance(it, dict):
                it.setdefault("model", "")
        return data
    except Exception:
        return []


def generate_key(model: str = "") -> str:
    """生成随机 32 字节 hex API Key 并持久化到 cache/keys.json，返回该 Key。

    参数：
        model   绑定的模型名；为空字符串表示全局 Key（兼容旧条目）。
    生成的条目附带 model 字段，便于网关按模型做 API Key 绑定校验。
    """
    key = secrets.token_hex(32)
    _ensure_cache()
    with _lock:
        keys = _load_keys()
        keys.append({
            "key": key,
            "model": model,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        with open(KEYS_PATH, "w", encoding="utf-8") as f:
            json.dump(keys, f, ensure_ascii=False, indent=2)
    return key


def list_keys() -> List[Dict[str, Any]]:
    """返回 keys.json 全部条目（已补 model 默认值），按生成顺序倒序（最新在前）。

    直接反转插入顺序，避免 created_at（秒级）并列时的不稳定排序。
    """
    keys = _load_keys()
    return list(reversed(keys))


def get_key_for_model(model: str) -> Optional[str]:
    """返回该模型绑定的 key（取最新一条匹配）；无则 None。

    model 为空字符串视为查询全局 Key——但全局 Key 不限制模型，故返回 None，
    调用方应按「无绑定」处理。
    """
    if not model:
        return None
    keys = _load_keys()
    result = None
    # 迭代顺序即生成顺序，保留最后一次匹配即「最新生成」的 key（稳定，不受秒级时间戳并列影响）
    for it in keys:
        if it.get("model") == model:
            result = it.get("key")
    return result
