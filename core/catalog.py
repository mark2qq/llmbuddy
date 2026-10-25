# -*- coding: utf-8 -*-
"""
curated 模型目录服务（CatalogService）。

设计采用「curated manifest」模式：内置清单 catalog_builtin.json 即为本地
维护的精选模型目录；可选地通过环境变量 LLM_CATALOG_URL 指向一个远程 JSON
（结构同内置）做远程合并。结果缓存到 cache/catalog.json（默认 24h 有效），
M3 接口可 force 刷新。返回的每条 ModelMeta 会附带 local 字段（本机是否已下载）。
"""

import os
import re
import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Tuple

from core.config import (MODELS_DIR, CACHE_DIR, REGISTRY_PATH,
                         load_registry, register_model)

logger = logging.getLogger("llm-service")

BUILTIN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog_builtin.json")
CACHE_TTL = 24 * 3600  # 缓存有效期（秒）

# 量化/格式后缀：文件名里常见 -Q4_K_M / _q5_k_s / -f16 / -bf16 / -gguf 等。
# 目录里的条目叫 qwen2.5-coder-7b-instruct，磁盘上的文件却叫
# qwen2.5-coder-7b-instruct-q4_k_m.gguf —— 只按字面拼路径永远匹配不上，
# 于是已下载的模型被显示成「未下载」，同时在列表末尾又冒出一条同名不同写法的
# 「本地导入」重复条目。下面这个正则用来把它们归一成可比对的键。
_QUANT_SUFFIX = re.compile(
    r"[-_.](?:i?q\d+(?:[-_.][a-z0-9]+)*|f16|f32|bf16|gguf)$", re.I)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def gguf_key(name: str) -> str:
    """把模型名或 GGUF 文件名压成可比对的键。

    例：``qwen2.5-coder-7b-instruct-q4_k_m.gguf`` / ``Qwen2.5-Coder-7B-Instruct-GGUF``
    都归一到 ``qwen25coder7binstruct``。用于「目录条目 ↔ 磁盘文件」的宽松匹配。
    """
    s = os.path.basename(str(name or ""))
    if s.lower().endswith(".gguf"):
        s = s[:-5]
    s = s.lower()
    # 允许一段量化后缀反复出现（-q4_k_m、-gguf、-f16）
    for _ in range(3):
        new = _QUANT_SUFFIX.sub("", s)
        if new == s:
            break
        s = new
    return _NON_ALNUM.sub("", s)


def _read_registry_file() -> Dict[str, Any]:
    """只读持久化的 cache/registry.json（不含内置默认项）。

    与 load_registry() 的区别：后者会把 BUILTIN_REGISTRY 合并进来，无法判断
    「这个名字是不是真的落过盘」，而补别名正需要这个判断。
    """
    try:
        if not os.path.exists(REGISTRY_PATH):
            return {}
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


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
    @staticmethod
    def _scan_local_gguf() -> Tuple[Dict[str, List[str]], List[str]]:
        """扫描当前 MODELS_DIR，返回 (规范化键 -> 绝对路径列表, 全部绝对路径)。"""
        by_key: Dict[str, List[str]] = {}
        allp: List[str] = []
        try:
            if not os.path.isdir(MODELS_DIR):
                return by_key, allp
            for fn in os.listdir(MODELS_DIR):
                if not fn.lower().endswith(".gguf"):
                    continue
                p = os.path.abspath(os.path.join(MODELS_DIR, fn))
                allp.append(p)
                by_key.setdefault(gguf_key(fn), []).append(p)
        except Exception as e:  # noqa: BLE001
            logger.warning("scan models dir failed: %s", e)
        return by_key, allp

    def _merge_local(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """标注「本机是否已下载」，并把目录里多出来的 gguf 补充成条目。

        匹配顺序（任一命中即算已下载）：
          1. 条目自身的 ``gguf`` 路径（相对 MODELS_DIR 或绝对路径）
          2. ``cache/registry.json`` 里同名模型的绝对路径（下载/导入时记下的）
          3. 规范化键宽松匹配（``qwen2.5-coder-7b-instruct`` ↔
             ``qwen2.5-coder-7b-instruct-q4_k_m.gguf``）

        命中后把真实文件名写回条目 ``gguf`` 并**补一条 registry 别名**，
        否则前端按条目名调用加载接口时 resolve_gguf() 会因为名字对不上报 1001。
        """
        files_by_key, all_files = self._scan_local_gguf()
        try:
            reg = load_registry()
        except Exception:  # noqa: BLE001
            reg = {}
        try:
            persisted = _read_registry_file()
        except Exception:  # noqa: BLE001
            persisted = {}
        claimed: set = set()                  # 已认领文件的 normcase 键
        aliases: Dict[str, str] = {}          # 待补写 registry 的 名字 -> 绝对路径

        root = os.path.normcase(os.path.abspath(MODELS_DIR))

        def _inside(p: str) -> bool:
            """文件是否位于当前模型目录内。

            registry.json 会长期保存历史下载记录。用户把模型目录从 A 改到 B 后，
            A 里的绝对路径仍然能 os.path.exists —— 若不过滤，模型库会把「当前目录
            根本没有的模型」显示成已下载（用户视角：改了目录却没生效）。
            所以只有落在当前 MODELS_DIR 下的记录才认。
            """
            if not p:
                return False
            try:
                ap = os.path.normcase(os.path.abspath(p))
            except Exception:  # noqa: BLE001
                return False
            return ap == root or ap.startswith(root + os.sep)

        def _abs(p: str) -> str:
            """补全成绝对路径（保留原始大小写，Windows 路径要能直接显示）。"""
            if not p:
                return ""
            try:
                return os.path.abspath(
                    p if os.path.isabs(p) else os.path.join(MODELS_DIR, p))
            except Exception:  # noqa: BLE001
                return ""

        for it in items:
            name = str(it.get("name", "") or "")
            gguf = str(it.get("gguf", "") or "")
            cands: List[str] = []
            # 1) 条目自带文件名（相对 MODELS_DIR 解析）
            if gguf:
                cands.append(gguf)
            # 2) 当前模型目录里的宽松匹配（量化后缀不同也算同一个）——
            #    放在 registry 之前：「设置里配的那个目录」才是准绳，
            #    用户换了目录就该以新目录为准，不能被旧目录的下载记录盖住。
            for k in (gguf_key(gguf or name), gguf_key(name)):
                if k:
                    cands.extend(files_by_key.get(k, []))
            # 3) registry 记录的路径（下载过 / 手动导入过）：兜底，
            #    但必须落在当前模型目录内（见 _inside 的注释）
            r = reg.get(name) or {}
            if r.get("gguf"):
                rg = _abs(str(r["gguf"]))
                if _inside(rg):
                    cands.append(rg)

            hit = ""
            for c in cands:
                ap = _abs(c)
                if not ap or not _inside(ap):
                    continue
                nk = os.path.normcase(ap)
                if nk in claimed or not os.path.exists(ap):
                    continue
                hit = ap
                claimed.add(nk)
                break
            if not hit:
                it["local"] = False
                continue

            it["local"] = True
            # 真实文件名写回：加载接口按它拼路径才不会 1002
            it["gguf"] = os.path.basename(hit)
            if not it.get("size_gb"):
                try:
                    it["size_gb"] = round(os.path.getsize(hit) / 1e9, 1)
                except Exception:  # noqa: BLE001
                    pass
            # 名字与落盘文件不一致时补别名，保证「列表里看到的模型名」能直接加载。
            # 判定依据是持久化 registry（不含 builtin 默认值）——builtin 里的
            # gguf 只是理想文件名，实际文件带量化后缀，必须用别名纠正。
            if name:
                prev = str((persisted.get(name) or {}).get("gguf", "") or "")
                if os.path.normcase(_abs(prev)) != os.path.normcase(hit):
                    aliases[name] = hit

        # 目录里没被任何条目认领的 → 作为「本地导入」补充（按文件名显示）
        for p in all_files:
            np = os.path.normcase(p)
            if np in claimed:
                continue
            fn = os.path.basename(p)
            try:
                size_gb = round(os.path.getsize(p) / 1e9, 1)
            except Exception:  # noqa: BLE001
                size_gb = 0
            items.append({
                "name": os.path.splitext(fn)[0], "repo": "本地模型目录",
                "quant": "", "size_gb": size_gb,
                "min_ram_gb": 0, "params_b": 0, "gguf": fn,
                "local": True, "source": "local",
            })
            claimed.add(np)

        # 补写 registry 别名（幂等：只在缺失时写，避免每次 fetch 都改磁盘）
        for aname, apath in aliases.items():
            try:
                register_model(aname, apath, {"source": "scan"})
            except Exception as e:  # noqa: BLE001
                logger.warning("register alias %s failed: %s", aname, e)
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
