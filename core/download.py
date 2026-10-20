# -*- coding: utf-8 -*-
"""
GGUF 流式下载服务（DownloadService）。

支持：后台线程流式下载、进度更新（内存 dict）、取消（cancel）。
多线程分片加速：真实网络场景（未注入 MockTransport）下若服务端支持 HTTP Range
且文件不小于阈值，自动启用多连接并行下载（默认 8 线程，环境变量 LLM_DL_THREADS
可调），显著提升 HF / hf-mirror / ModelScope 的下载速度；不支持 Range 或分片
失败时自动回退单线程流式（原逻辑），保证兼容与可靠。
完成后将模型写入 models_dir 并通过 config.register_model 持久化到注册表。
鉴权：可选 auth={user, token}，下载时以 Bearer token 形式带入请求头（适配 HF 私有库）。
"""

import os
import json
import time
import uuid
import shutil
import logging
import threading
from typing import Dict, Any, Optional

import httpx

from core.config import MODELS_DIR, register_model

logger = logging.getLogger("llm-service")

# 分片下载参数（环境变量可调）
DL_THREADS = max(1, int(os.environ.get("LLM_DL_THREADS", "8")))
# 小于该体积（字节）不启用分片，避免小文件的线程开销
RANGE_MIN_SIZE = max(0, int(os.environ.get("LLM_DL_RANGE_MIN", str(8 * 1024 * 1024))))


class _Cancelled(Exception):
    """下载被用户取消（内部信号，由 _run 统一清理）。"""


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
                # 下载可观测性：已下载字节 / 总大小 / 速度(MB/s) / 并发线程数
                "downloaded": 0, "total_bytes": 0, "speed_mbps": 0.0, "threads": 1,
                "started_at": 0.0,
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
                self.tasks[name]["started_at"] = time.time()
            with httpx.Client(transport=self._transport, follow_redirects=True,
                              timeout=30) as client:
                for idx, url in enumerate(candidates):
                    try:
                        if idx > 0:
                            logger.warning("Download fallback %s -> candidate %d: %s",
                                           name, idx, url)
                        # 多线程分片加速：仅真实网络场景（未注入 MockTransport）探测 Range
                        total = (self._probe(client, url, headers)
                                 if self._transport is None else None)
                        if total and total >= RANGE_MIN_SIZE:
                            try:
                                self._download_ranged(url, headers, total,
                                                      name, gguf_path)
                            except _Cancelled:
                                raise
                            except Exception as e:
                                # 分片失败（Range 中途异常等）：清理分片，回退单线程重试
                                logger.warning(
                                    "Ranged download failed, fallback to "
                                    "single-thread: %s (%s)", name, e)
                                self._cleanup_parts(gguf_path)
                                last_err = e
                                self._download_single(client, url, headers,
                                                      name, gguf_path)
                        else:
                            self._download_single(client, url, headers,
                                                  name, gguf_path)
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
                    except _Cancelled:
                        # 取消：清理已下载的部分文件与分片
                        self._cleanup(gguf_path)
                        logger.info("Download cancelled: %s", name)
                        return
                    except Exception as e:
                        # 该候选源失败：清理半截文件，尝试下一个
                        last_err = e
                        self._cleanup(gguf_path)
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

    # ------------------------------------------------------------------ #
    # 下载实现：单线程流式（原逻辑）/ 多线程 Range 分片（加速）
    # ------------------------------------------------------------------ #
    def _probe(self, client: httpx.Client, url: str,
               headers: Dict[str, str]) -> Optional[int]:
        """探测 URL 是否支持 Range 分片，返回总字节数；不支持/失败返回 None。"""
        resp = None
        try:
            req = client.build_request(
                "GET", url, headers={**headers, "Range": "bytes=0-0"})
            # 注意：httpx 0.28 起 Response 不再支持 with 协议，须手动 close
            resp = client.send(req, stream=True)
            if resp.status_code != 206:
                return None
            cr = resp.headers.get("content-range", "")
            if "/" not in cr:
                return None
            try:
                total = int(cr.rsplit("/", 1)[1].strip())
            except ValueError:
                return None
            resp.read()
            return total or None
        except Exception:
            return None
        finally:
            if resp is not None:
                resp.close()

    def _update_progress(self, name: str, downloaded: int, total: int = 0,
                         threads: int = 0) -> None:
        """统一更新进度 / 已下载字节 / 速度（供前端展示加速效果）。

        注意：调用方不得持有 self._lock（本方法内部会取锁）。
        """
        with self._lock:
            t = self.tasks.get(name)
            if not t:
                return
            t["downloaded"] = int(downloaded)
            if total:
                t["total_bytes"] = int(total)
                t["progress"] = round(min(1.0, downloaded / total), 4)
            if threads:
                t["threads"] = int(threads)
            started = t.get("started_at") or 0.0
            if started:
                elapsed = max(0.001, time.time() - started)
                t["speed_mbps"] = round(downloaded / elapsed / (1024 * 1024), 2)

    def _download_single(self, client: httpx.Client, url: str,
                         headers: Dict[str, str], name: str,
                         gguf_path: str) -> None:
        """单线程流式下载（原逻辑；分片不可用/失败时回退到此）。"""
        with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            done = 0
            with open(gguf_path, "wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    with self._lock:
                        if self.tasks[name]["cancel"]:
                            raise _Cancelled()
                    f.write(chunk)
                    done += len(chunk)
                    self._update_progress(name, done, total, threads=1)

    def _download_ranged(self, url: str, headers: Dict[str, str], total: int,
                         name: str, gguf_path: str) -> None:
        """多线程 Range 分片下载，完成后按序拼接为 gguf_path。

        任一分片失败向上抛异常（由 _run 回退单线程重试该源）；
        用户取消抛 _Cancelled。
        """
        # 每片至少约 4MB，避免小文件开过多线程
        n = min(DL_THREADS, max(1, total // (4 * 1024 * 1024)))
        n = max(1, n)
        step = total // n
        bounds = []
        start = 0
        for i in range(n):
            end = (start + step - 1) if i < n - 1 else (total - 1)
            if end < start:
                end = total - 1
            bounds.append((i, start, end))
            start = end + 1
        errors = []
        cancelled_box = []
        done_box = [0]
        lock = threading.Lock()

        def _worker(i: int, s: int, e: int) -> None:
            part = f"{gguf_path}.part{i}"
            try:
                with httpx.Client(transport=self._transport,
                                  follow_redirects=True, timeout=30) as c:
                    with c.stream("GET", url,
                                  headers={**headers, "Range": f"bytes={s}-{e}"}) as resp:
                        resp.raise_for_status()
                        with open(part, "wb") as f:
                            for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                                with self._lock:
                                    if self.tasks[name]["cancel"]:
                                        raise _Cancelled()
                                f.write(chunk)
                                with lock:
                                    done_box[0] += len(chunk)
                                    cur = done_box[0]
                                self._update_progress(name, cur, total, threads=n)
            except _Cancelled:
                # 记录取消，不抛出行线程（避免 threading 打印 traceback 噪音）
                cancelled_box.append(True)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        for (i, s, e) in bounds:
            t = threading.Thread(target=_worker, args=(i, s, e), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        with self._lock:
            if self.tasks[name]["cancel"] or cancelled_box:
                raise _Cancelled()
        if errors:
            raise errors[0]
        # 按序拼接并清理分片
        with open(gguf_path, "wb") as out:
            for (i, s, e) in bounds:
                part = f"{gguf_path}.part{i}"
                with open(part, "rb") as f:
                    shutil.copyfileobj(f, out, length=1024 * 1024)
                os.remove(part)

    def _cleanup_parts(self, gguf_path: str) -> None:
        """清理分片临时文件。"""
        for i in range(DL_THREADS + 2):
            part = f"{gguf_path}.part{i}"
            try:
                if os.path.exists(part):
                    os.remove(part)
            except Exception:
                pass

    def _cleanup(self, gguf_path: str) -> None:
        """清理半截文件与分片。"""
        self._cleanup_parts(gguf_path)
        try:
            if os.path.exists(gguf_path):
                os.remove(gguf_path)
        except Exception:
            pass

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
