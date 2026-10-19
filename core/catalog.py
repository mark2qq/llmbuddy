# -*- coding: utf-8 -*-
"""
curated 模型目录服务（CatalogService）。

设计采用「curated manifest」模式：内置清单 catalog_builtin.json 即为本地
维护的精选模型目录；可选地通过环境变量 LLM_CATALOG_URL 指向一个远程 JSON
（结构同内置）做远程合并。结果缓存到 cache/catalog.json（默认 24h 有效），
M3 接口可 force 刷新。返回的每条 ModelMeta 会附带 local 字段（本机是否已下载）。
"""

import os
import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

from core.config import MODELS_DIR, CACHE_DIR

logger = logging.getLogger("llm-service")

BUILTIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_builtin.json")
CACHE_TTL = 24 * 3600  # 缓存有效期（秒）


class CatalogService:
    def __init__(self, builtin_path: str = BUILTIN_PATH,
                 cache_path: str = os.path.join(CACHE_DIR, "catalog.json"),
                 remote_url: Optional[str] = None):
        self.builtin_path = builtin_path
        self.cache_path = cache_path
        self.remote_url = remote_url or os.environ.get("LLM_CATALOG_URL")
        self._lock = threading.Lock()

    # ------------------------- 内置清单 -------------------------
    def _load_builtin(self) -> List[Dict[str, Any]]:
        try:
            with open(self.builtin_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error("load builtin catalog failed: %s", e)
            return []

    # ------------------------- 远程（可选） -------------------------
    def _load_remote(self) -> List[Dict[str, Any]]:
        if not self.remote_url:
            return []
        try:
            import httpx
            with httpx.Client(timeout=15, follow_redirects=True) as c:
                r = c.get(self.remote_url)
                if r.status_code == 200:
                    data = r.json()
                    return data.get("items", data) if isinstance(data, dict) else data
        except Exception as e:
            logger.warning("load remote catalog failed (fallback builtin): %s", e)
        return []

    # ------------------------- 本地标记 -------------------------
    def _merge_local(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        known = set()
        for it in items:
            gguf = os.path.join(MODELS_DIR, it.get("gguf", f"{it['name']}.gguf"))
            it["local"] = os.path.exists(gguf)
            known.add(os.path.normcase(os.path.abspath(gguf)))
        # 扫描目录中不在清单内的 gguf（手动下载/导入），自动补充为本地模型
        try:
            if os.path.isdir(MODELS_DIR):
                for fn in os.listdir(MODELS_DIR):
                    if not fn.lower().endswith(".gguf"):
                        continue
                    p = os.path.normcase(os.path.abspath(os.path.join(MODELS_DIR, fn)))
                    if p in known:
                        continue
                    items.append({
                        "name": os.path.splitext(fn)[0], "repo": "本地导入",
                        "quant": "", "size_gb": round(os.path.getsize(p) / 1e9, 1),
                        "min_ram_gb": 0, "params_b": 0, "gguf": fn,
                        "local": True, "source": "manual",
                    })
        except Exception:
            pass
        return items

    # ------------------------- 主入口 -------------------------
    def fetch(self, force: bool = False, auth: Optional[Dict[str, Any]] = None
              ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        获取模型目录。
        返回 (items, cached)：cached=True 表示直接命中本地缓存（未重新合并）。
        force=True 或缓存过期/缺失则重新合并 builtin + remote + local 标记并写缓存。
        """
        # 1) 尝试读缓存
        if not force and os.path.exists(self.cache_path):
            try:
                mtime = os.path.getmtime(self.cache_path)
                if time.time() - mtime < CACHE_TTL:
                    with open(self.cache_path, "r", encoding="utf-8") as f:
                        cached = json.load(f)
                    items = cached.get("items", cached) if isinstance(cached, dict) else cached
                    return self._merge_local(items), True
            except Exception:
                pass
        # 2) 重新合并
        items = self._load_builtin()
        remote = self._load_remote()
        if remote:
            items = items + [r for r in remote if r.get("name") not in {i.get("name") for i in items}]
        items = self._merge_local(items)
        # 3) 写缓存
        try:
            os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
            with self._lock:
                with open(self.cache_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "items": items,
                        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("write catalog cache failed: %s", e)
        return items, False

    def search(self, keyword: str) -> List[Dict[str, Any]]:
        items, _ = self.fetch(force=False)
        if not keyword:
            return items
        kw = keyword.lower()
        return [it for it in items
                if kw in it.get("name", "").lower() or kw in it.get("repo", "").lower()]

    def find(self, name: str) -> Optional[Dict[str, Any]]:
        items, _ = self.fetch(force=False)
        for it in items:
            if it.get("name") == name:
                return it
        return None


# ----------------------------------------------------------------------------
# 单例访问
# ----------------------------------------------------------------------------
_service: Optional[CatalogService] = None
_service_lock = threading.Lock()


def get_catalog_service() -> CatalogService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = CatalogService()
    return _service
