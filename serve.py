# -*- coding: utf-8 -*-
"""
本地大模型服务（不依赖 Ollama）—— 控制面 + OpenAI 兼容网关。

架构：
    本服务（FastAPI）作为「控制面 + OpenAI 兼容网关」：
    - core/pool.py  多模型并发引擎池（ModelPool），每个已加载模型一个 llama-server.exe 子进程
    - routers/gateway.py  OpenAI 兼容 /v1/*（/health、/v1/models、chat/completions、completions）
    - routers/manage.py   管理 REST 接口 /api/v1/*（M1~M16：系统检测/端口/目录/下载/Key/池/并发/停机）

    对外端口 LLM_PORT（默认 8000）：OpenAI 兼容接口 + 管理接口，供其它智能体调用
    引擎内部端口 LLM_ENGINE_PORT（默认 7999）：仅本机 127.0.0.1，不对外

启动：
    C:\Program Files\Python311\python.exe E:\llm-service\serve.py
或双击 E:\llm-service\start.bat
"""

import os
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.pool import init_model_pool, get_model_pool
from core.system import detect_system
from routers import manage, gateway

# ----------------------------------------------------------------------------
# 配置（来自环境变量，与旧版保持一致）
# ----------------------------------------------------------------------------
ENGINE_EXE = os.environ.get("LLM_ENGINE", r"E:\llm-service\engine\llama-server.exe")
MODELS_DIR = os.environ.get("LLM_MODELS_DIR", r"E:\llama-serve\models")
HOST = os.environ.get("LLM_HOST", "127.0.0.1")
PORT = int(os.environ.get("LLM_PORT", "8000"))
ENGINE_BASE_PORT = int(os.environ.get("LLM_ENGINE_PORT", "7999"))
ENGINE_API_KEY = os.environ.get("LLM_ENGINE_KEY", "local")
API_KEY = os.environ.get("LLM_API_KEY", "")  # 网关/管理鉴权：留空=本地开放

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm-service")


# ----------------------------------------------------------------------------
# 生命周期：启动初始化池/看门狗/系统检测；关闭优雅卸载
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化模型池（并发上限默认由系统检测给出）
    pool = init_model_pool(
        engine_exe=ENGINE_EXE,
        models_dir=MODELS_DIR,
        engine_base_port=ENGINE_BASE_PORT,
        api_key=ENGINE_API_KEY,
    )
    pool.start_watchdog()
    detect_system()  # 缓存 suggested_max
    logger.info("Local LLM Service started (gateway=%s:%d, engine_base=%d, max_concurrent=%d)",
                HOST, PORT, ENGINE_BASE_PORT, pool.max_concurrent)
    try:
        yield
    finally:
        # 优雅停机：卸载所有模型
        get_model_pool().shutdown()
        logger.info("Local LLM Service stopped")


# ----------------------------------------------------------------------------
# FastAPI 应用
# ----------------------------------------------------------------------------
app = FastAPI(title="Local LLM Service", version="3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

# 挂载管理路由与 OpenAI 兼容网关路由
app.include_router(manage.router)
app.include_router(gateway.router)


def run() -> None:
    """以编程方式启动服务（供客户端 exe 的 --backend 模式调用，或本文件直接运行）。"""
    import uvicorn

    logger.info("Local LLM Service on %s:%d (engine_base=%d)", HOST, PORT, ENGINE_BASE_PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    run()
