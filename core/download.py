# -*- coding: utf-8 -*-
"""
GGUF 流式下载服务（DownloadService）。

支持：后台线程流式下载、进度更新（内存 dict）、取消（cancel）。
完成后将模型写入 models_dir 并通过 config.register_model 持久化到注册表。
鉴权：可选 auth={user, token}，下载时以 Bearer token 形式带入请求头（适配 HF 私有库）。
"""

import os
import json
import time
import uuid
import logging
import threading
from typing import Dict, Any, Optional

import httpx

from core.config import MODELS_DIR, register_model

logger = logging.getLogger("llm-service")


class DownloadService:
    def __init__(self, models_dir: str = MODELS_DIR, transport: Optional[httpx.BaseTransport] = None):
        self.models_dir = models_dir
        self._transport = transport  # 测试可注入 httpx.MockTransport
        # name -> {task_id, name, status, progress, gguf_path, error, cancel}
        self.tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _path_for(self, meta: Dict[str, Any]) -> str:
        url = meta.get("gguf_url", "")
        fn = os.path.basename(url.split("?")[0]) if url else ""
        if not fn:
            fn = f"{meta['name']}.gguf"
        return os.path.join(self.models_dir, fn)

    def start(self, meta: Dict[str, Any], auth: Optional[Dict[str, Any]] = None) -> str:
        """
        启动后台下载，返回 task_id。
        status: pending -> downloading -> done | error | cancelled
        """
        name = meta["name"]
        gguf_path = self._path_for(meta)
        task_id = uuid.uuid4().hex
        with self._lock:
            self.tasks[name] = {
                "task_id": task_id, "name": name, "status": "pending",
                "progress": 0.0, "gguf_path": gguf_path, "error": None, "cancel": False,
            }
        threading.Thread(target=self._run, args=(task_id, name, meta, gguf_path, auth),
                         daemon=True).start()
        return task_id

    def _candidate_urls(self, url: str) -> list:
        """生成候选下载地址（依序尝试）。

        HF 直链自动前置镜像源（默认 https://hf-mirror.com，可用环境变量
        LLM_HF_MIRROR 覆盖）：国内直连 huggingface.co 常被重置
        （WinError 10054），镜像优先可显著提高成功率；镜像失败再回退原直链。
        非 HF 源（ModelScope 等）保持原样。
        """
        urls = []
        if url:
            if "huggingface.co" in url:
                mirror = os.environ.get("LLM_HF_MIRROR", "https://hf-mirror.com").rstrip("/")
                urls.append(url.replace("https://huggingface.co", mirror))
            urls.append(url)
        return urls

    def _run(self, task_id: str, name: str, meta: Dict[str, Any],
             gguf_path: str, auth: Optional[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(gguf_path), exist_ok=True)
        headers: Dict[str, str] = {}
        if auth and auth.get("token"):
            headers["Authorization"] = f"Bearer {auth['token']}"
        candidates = self._candidate_urls(meta.get("gguf_url", ""))
        last_err: Optional[Exception] = None
        try:
            with self._lock:
                self.tasks[name]["status"] = "downloading"
            with httpx.Client(transport=self._transport, follow_redirects=True,
                              timeout=30) as client:
                for idx, url in enumerate(candidates):
                    try:
                        if idx > 0:
                            logger.warning("Download fallback %s -> candidate %d: %s",
                                           name, idx, url)
                        with client.stream("GET", url, headers=headers) as resp:
                            resp.raise_for_status()
                            total = int(resp.headers.get("content-length", 0) or 0)
                            done = 0
                            with open(gguf_path, "wb") as f:
                                cancelled = False
                                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                                    with self._lock:
                                        if self.tasks[name]["cancel"]:
                                            cancelled = True
                                            self.tasks[name]["status"] = "cancelled"
                                            break
                                    f.write(chunk)
                                    done += len(chunk)
                                    if total:
                                        prog = min(1.0, done / total)
                                        with self._lock:
                                            self.tasks[name]["progress"] = round(prog, 4)
                            if cancelled:
                                # 清理已下载的部分文件
                                try:
                                    os.remove(gguf_path)
                                except Exception:
                                    pass
                                logger.info("Download cancelled: %s", name)
                                return
                        with self._lock:
                            self.tasks[name]["status"] = "done"
                            self.tasks[name]["progress"] = 1.0
                            host = url.split("/")[2] if "://" in url else url
                            self.tasks[name]["via"] = host
                        # 持久化到注册表
                        try:
                            register_model(name, gguf_path, {
                                "source": "download",
                                "repo": meta.get("repo"),
                                "quant": meta.get("quant"),
                                "size_gb": meta.get("size_gb"),
                            })
                        except Exception as e:
                            logger.error("register after download failed: %s", e)
                        logger.info("Download done: %s -> %s (via %s)",
                                    name, gguf_path, self.tasks[name].get("via"))
                        return
                    except Exception as e:
                        # 该候选源失败：清理半截文件，尝试下一个
                        last_err = e
                        try:
                            os.remove(gguf_path)
                        except Exception:
                            pass
                        with self._lock:
                            self.tasks[name]["progress"] = 0.0
                        continue
                # 所有候选源均失败
                with self._lock:
                    self.tasks[name]["status"] = "error"
                    self.tasks[name]["error"] = (
                        f"{last_err}（所有下载源均失败；可到「模型库 → 手动添加」"
                        f"粘贴 ModelScope 等直链重试）")
                logger.exception("Download failed: %s (%s)", name, last_err)
        except Exception as e:
            with self._lock:
                self.tasks[name]["status"] = "error"
                self.tasks[name]["error"] = str(e)
            logger.exception("Download failed: %s (%s)", name, e)

    def cancel(self, name: str) -> bool:
        """取消下载（仅 pending/downloading 可取消）。"""
        with self._lock:
            t = self.tasks.get(name)
            if t and t["status"] in ("pending", "downloading"):
                t["cancel"] = True
                t["status"] = "cancelled"
                return True
        return False

    def get_status(self, name: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            t = self.tasks.get(name)
            return dict(t) if t else None


# ----------------------------------------------------------------------------
# 单例访问
# ----------------------------------------------------------------------------
_service: Optional[DownloadService] = None
_service_lock = threading.Lock()


def get_download_service() -> DownloadService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = DownloadService()
    return _service
