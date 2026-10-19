# -*- coding: utf-8 -*-
"""
管理 REST 接口（routers/manage.py）—— M1 ~ M16。

统一响应包：{code, message, data}，code=0 成功。
错误码：1001 未注册 / 1002 gguf缺失 / 1003 引擎失败 / 1004 端口占用 /
        1005 并发上限 / 1006 下载失败 / 1007 目录失败 / 1008 未就绪。
可选 X-API-Key 头校验（与 LLM_API_KEY 一致，空则开放）。
"""

import os
import time
import asyncio
import logging
from typing import Optional, Dict, Any, List

import httpx
from fastapi import APIRouter, Request, Header, Depends, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import HTTPException

from core.pool import get_model_pool, ModelPoolError
from core.system import detect_system
from core.config import generate_key, list_keys
from core.catalog import get_catalog_service
from core.download import get_download_service

logger = logging.getLogger("llm-service")

# 配置
API_KEY = os.environ.get("LLM_API_KEY", "")
ENGINE_API_KEY = os.environ.get("LLM_ENGINE_KEY", "local")
GATEWAY_PORT = int(os.environ.get("LLM_PORT", "8000"))
GATEWAY_HOST = os.environ.get("LLM_HOST", "127.0.0.1")

# router 在 _mgmt_auth 定义后创建（见下方）


# ----------------------------------------------------------------------------
# 管理接口鉴权（可选 X-API-Key）
# ----------------------------------------------------------------------------
def _mgmt_auth(x_api_key: Optional[str] = Header(None),
               authorization: Optional[str] = Header(None)):
    if API_KEY:
        ok = (x_api_key == API_KEY) or (authorization in (f"Bearer {API_KEY}", API_KEY))
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


router = APIRouter(dependencies=[Depends(_mgmt_auth)])


# ----------------------------------------------------------------------------
# 统一响应辅助
# ----------------------------------------------------------------------------
def ok(data: Any = None, message: str = "ok") -> Dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


def fail(code: int, message: str, data: Any = None) -> Dict[str, Any]:
    return {"code": code, "message": message, "data": data}


def _http_status(code: int) -> int:
    return {1001: 404, 1002: 404, 1003: 503, 1004: 503, 1005: 429,
            1006: 502, 1007: 502, 1008: 503}.get(code, 400)


def _err(e: ModelPoolError) -> JSONResponse:
    return JSONResponse(status_code=_http_status(e.code), content=fail(e.code, e.message))


# ----------------------------------------------------------------------------
# M1 系统信息
# ----------------------------------------------------------------------------
@router.get("/api/v1/system/info")
def m1_system_info():
    return ok(detect_system())


# ----------------------------------------------------------------------------
# M2 端口扫描
# ----------------------------------------------------------------------------
def _scan_ports(start: int, count: int):
    import socket
    ports: List[int] = []
    used: List[int] = []
    for p in range(start, start + count):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        try:
            s.bind(("127.0.0.1", p))
            ports.append(p)
        except OSError:
            used.append(p)
        finally:
            s.close()
    return ports, used


@router.get("/api/v1/system/ports")
def m2_system_ports(count: int = Query(8, ge=1, le=200),
                    start: int = Query(7900, ge=1, le=65535)):
    ports, used = _scan_ports(start, count)
    return ok({"ports": ports, "used": used})


# ----------------------------------------------------------------------------
# M3 模型目录
# ----------------------------------------------------------------------------
@router.get("/api/v1/models/catalog")
def m3_catalog(force: bool = Query(False), keyword: str = Query("")):
    try:
        items, cached = get_catalog_service().fetch(force=force)
    except Exception as e:
        logger.exception("catalog fetch failed")
        return JSONResponse(status_code=502, content=fail(1007, f"catalog fetch failed: {e}"))
    if keyword:
        kw = keyword.lower()
        items = [it for it in items
                 if kw in it.get("name", "").lower() or kw in it.get("repo", "").lower()]
    return ok({"items": items, "cached": cached})


# ----------------------------------------------------------------------------
# M4 触发下载
# ----------------------------------------------------------------------------
@router.post("/api/v1/models/pull")
async def m4_pull(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name")
    auth = body.get("auth")
    if not name:
        return JSONResponse(status_code=400, content=fail(1001, "name required"))
    meta = get_catalog_service().find(name)
    if meta is None:
        return JSONResponse(status_code=404, content=fail(1001, f"model '{name}' not in catalog"))
    svc = get_download_service()
    task_id = svc.start(meta, auth)
    status = svc.get_status(name) or {}
    return ok({"task_id": task_id, "gguf_path": status.get("gguf_path")})


# ----------------------------------------------------------------------------
# M5 查询下载进度
# ----------------------------------------------------------------------------
@router.get("/api/v1/models/pull/{name}")
def m5_pull_status(name: str):
    status = get_download_service().get_status(name)
    if status is None:
        return ok({"status": "not_found", "progress": 0.0, "gguf_path": None, "error": None})
    return ok({
        "status": status.get("status"),
        "progress": status.get("progress", 0.0),
        "gguf_path": status.get("gguf_path"),
        "error": status.get("error"),
    })


# ----------------------------------------------------------------------------
# M6 取消下载
# ----------------------------------------------------------------------------
@router.delete("/api/v1/models/pull/{name}")
def m6_pull_cancel(name: str):
    cancelled = get_download_service().cancel(name)
    return ok({"ok": cancelled})


# ----------------------------------------------------------------------------
# M7 生成 API Key
# ----------------------------------------------------------------------------
@router.post("/api/v1/keys/generate")
async def m7_generate_key(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    model = body.get("model") or ""
    key = generate_key(model)
    return ok({"api_key": key, "model": model})


# ----------------------------------------------------------------------------
# M7.1 列出已生成的 API Key
# ----------------------------------------------------------------------------
@router.get("/api/v1/keys")
def m7_list_keys():
    """列出全部已生成 Key（按生成时间倒序）；保留 _mgmt_auth 全局鉴权。"""
    return ok({"keys": list_keys()})


# ----------------------------------------------------------------------------
# M8 池总览
# ----------------------------------------------------------------------------
@router.get("/api/v1/pool")
def m8_pool():
    pool = get_model_pool()
    return ok({
        "max_concurrent": pool.max_concurrent,
        "running": pool.running_count(),
        "engine_base_port": pool.engine_base_port,
        "loaded": pool.list_loaded(),
    })


# ----------------------------------------------------------------------------
# M9 加载模型
# ----------------------------------------------------------------------------
@router.post("/api/v1/pool/load")
async def m9_load(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    model = body.get("model")
    if not model:
        return JSONResponse(status_code=400, content=fail(1001, "model required"))
    n_ctx = body.get("n_ctx")
    n_threads = body.get("n_threads")
    n_gpu_layers = body.get("n_gpu_layers")
    pool = get_model_pool()
    try:
        entry = pool.load(model, n_ctx=n_ctx, n_threads=n_threads, n_gpu_layers=n_gpu_layers)
    except ModelPoolError as e:
        return _err(e)
    running = pool.running_count()
    return ok({
        "model": model,
        "status": entry.instance.status,
        "engine_port": entry.instance.engine_port,
        "running": running,
        "max_concurrent": pool.max_concurrent,
        "note": ("已加载但未运行：本机同时运行上限 "
                 f"{pool.max_concurrent} 个，请先停止一个模型再启动该模型"
                 if entry.instance.status == "paused" else ""),
    })


# ----------------------------------------------------------------------------
# M10 查询某模型
# ----------------------------------------------------------------------------
@router.get("/api/v1/pool/{model}")
def m10_get(model: str):
    pool = get_model_pool()
    entry = pool.get(model)
    if entry is None:
        return JSONResponse(status_code=404, content=fail(1001, f"model '{model}' not loaded"))
    return ok(entry.instance.to_dict())


# ----------------------------------------------------------------------------
# M11 卸载模型（= 取消加载，从池中移除）
# ----------------------------------------------------------------------------
@router.post("/api/v1/pool/{model}/unload")
def m11_unload(model: str):
    pool = get_model_pool()
    pool.unload(model)
    return ok({"model": model, "status": "unloaded"})


# ----------------------------------------------------------------------------
# M17 暂停模型运行（停止运行但保留加载，随时可再启动）
# ----------------------------------------------------------------------------
@router.post("/api/v1/pool/{model}/pause")
def m17_pause(model: str):
    pool = get_model_pool()
    entry = pool.pause(model)
    if entry is None:
        return JSONResponse(status_code=404, content=fail(1001, f"model '{model}' not loaded"))
    return ok({"model": model, "status": entry.instance.status,
               "running": pool.running_count(),
               "max_concurrent": pool.max_concurrent})


# ----------------------------------------------------------------------------
# M18 启动已加载模型（恢复运行；运行名额不足时报 1005）
# ----------------------------------------------------------------------------
@router.post("/api/v1/pool/{model}/resume")
def m18_resume(model: str):
    pool = get_model_pool()
    try:
        entry = pool.resume(model)
    except ModelPoolError as e:
        return _err(e)
    return ok({"model": model, "status": entry.instance.status,
               "running": pool.running_count(),
               "max_concurrent": pool.max_concurrent})


# ----------------------------------------------------------------------------
# M12 测试模型（小推理）
# ----------------------------------------------------------------------------
@router.get("/api/v1/pool/{model}/test")
async def m12_test(model: str, prompt: str = Query("hi")):
    pool = get_model_pool()
    entry = pool.get(model)
    if entry is None or entry.instance.status != "ready":
        try:
            pool.load(model)
        except ModelPoolError as e:
            return _err(e)
        # 等待就绪
        deadline = time.time() + 180
        while time.time() < deadline:
            e = pool.get(model)
            if e is None:
                return JSONResponse(status_code=503, content=fail(1008, "model unloaded"))
            if e.instance.status == "ready":
                break
            if e.instance.status == "error":
                return JSONResponse(status_code=503, content=fail(1003, "engine error"))
            await asyncio.sleep(0.4)
    base = pool.route(model)
    if base is None:
        return JSONResponse(status_code=503, content=fail(1003, "engine not reachable"))
    start = time.time()
    try:
        async with httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(
                trust_env=False, limits=httpx.Limits(max_keepalive_connections=0)),
            timeout=60,
        ) as client:
            resp = await client.post(
                f"{base}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "max_tokens": 16, "stream": False},
                # 强制 Connection: close，避免 keep-alive 长连接占用引擎 slot 导致后续请求挂起
                headers={"Authorization": f"Bearer {ENGINE_API_KEY}", "Connection": "close"},
            )
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {})
            # 思考模型（如 Qwen3）正文进 reasoning_content，content 可能为空；
            # 此处兜底取 content 或 reasoning_content，保证测试样例有内容。
            sample = (msg.get("content") or msg.get("reasoning_content") or "")
            latency = (time.time() - start) * 1000
            return ok({"ok": True, "latency_ms": round(latency, 1), "sample": sample})
    except Exception as e:
        latency = (time.time() - start) * 1000
        return ok({"ok": False, "latency_ms": round(latency, 1), "sample": "", "error": str(e)})


# ----------------------------------------------------------------------------
# M19 删除本地模型（删除 gguf 文件并清注册；模型运行中则拒绝）
# ----------------------------------------------------------------------------
@router.post("/api/v1/models/{name}/delete")
def m19_delete_model(name: str):
    from core.config import resolve_gguf, MODELS_DIR, load_registry
    pool = get_model_pool()
    entry = pool.get(name)
    if entry is not None and entry.instance.status in ("loading", "ready"):
        return JSONResponse(status_code=409, content=fail(
            1003, f"模型 '{name}' 正在运行，请先停止/取消加载后再删除"))
    # 定位文件
    path = None
    try:
        path = resolve_gguf(name)
    except KeyError:
        # 未注册：回退 models_dir/{name}.gguf
        cand = os.path.join(MODELS_DIR, f"{name}.gguf")
        path = cand if os.path.exists(cand) else None
    except FileNotFoundError:
        path = None
    if not path or not os.path.exists(path):
        return JSONResponse(status_code=404, content=fail(
            1002, f"未找到模型 '{name}' 的 gguf 文件"))
    # 安全约束：只允许删除 MODELS_DIR 内的文件
    models_dir_abs = os.path.abspath(MODELS_DIR)
    if os.path.abspath(path).startswith(models_dir_abs):
        try:
            os.remove(path)
        except Exception as e:
            return JSONResponse(status_code=500, content=fail(
                1006, f"删除文件失败: {e}"))
        # 从下载注册表（cache/registry.json）中移除该条目
        try:
            from core.config import REGISTRY_PATH
            import json
            if os.path.exists(REGISTRY_PATH):
                with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if name in data:
                    data.pop(name)
                    with open(REGISTRY_PATH, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        logger.info("Model '%s' deleted: %s", name, path)
        return ok({"model": name, "deleted": path})
    return JSONResponse(status_code=403, content=fail(
        1006, "拒绝删除模型目录之外的文件"))


# ----------------------------------------------------------------------------
# M13 并发上限查询
# ----------------------------------------------------------------------------
@router.get("/api/v1/settings/concurrency")
def m13_concurrency():
    pool = get_model_pool()
    sys_info = detect_system()
    reason = (f"依据空闲内存 {sys_info['free_ram_gb']}GB 与单模型预估内存"
              f"(约 6GB) 估算，系统保留 {sys_info['os_reserve_gb']}GB")
    return ok({
        "max_concurrent": pool.max_concurrent,
        "suggested": sys_info["suggested_max_models"],
        "reason": reason,
    })


# ----------------------------------------------------------------------------
# M14 设置并发上限
# ----------------------------------------------------------------------------
@router.put("/api/v1/settings/concurrency")
async def m14_set_concurrency(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    n = body.get("max_concurrent")
    if not isinstance(n, int) or n < 1:
        return JSONResponse(status_code=400, content=fail(1005, "max_concurrent must be int >= 1"))
    pool = get_model_pool()
    pool.set_max_concurrent(n)
    return ok({"ok": True})


# ----------------------------------------------------------------------------
# M15 优雅停机
# ----------------------------------------------------------------------------
@router.post("/api/v1/service/stop")
def m15_stop():
    pool = get_model_pool()
    pool.shutdown()
    return ok({"ok": True})


# ----------------------------------------------------------------------------
# M16 扩展健康检查（含 pool 信息）
# ----------------------------------------------------------------------------
@router.get("/api/v1/health")
def m16_health():
    pool = get_model_pool()
    sys_info = detect_system()
    loaded = pool.list_loaded()
    return ok({
        "status": "ok" if loaded else "idle",
        "gateway": {"host": GATEWAY_HOST, "port": GATEWAY_PORT},
        "engine_base_port": pool.engine_base_port,
        "max_concurrent": pool.max_concurrent,
        "pool": {"max_concurrent": pool.max_concurrent, "loaded": loaded},
        "system": {
            "free_ram_gb": sys_info["free_ram_gb"],
            "gpu_available": sys_info["gpu_available"],
            "suggested_max_models": sys_info["suggested_max_models"],
        },
    })
