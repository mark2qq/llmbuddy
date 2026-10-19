# -*- coding: utf-8 -*-
"""
pytest 共享夹具：启动一个临时 uvicorn 服务（端口 8011，引擎基端口 8019），
供 test_manage / test_gateway / test_errors 共享。

关键环境注意（本机有 HTTP 代理）：
  - 子进程 uvicorn 通过 os.environ 注入 LLM_PORT / LLM_ENGINE_PORT 后再 import serve，
    因此 serve.py 在模块加载时就能读到正确端口（与需求一致）。
  - 测试进程用 httpx.Client(trust_env=False) 调用 127.0.0.1，避免请求被打到代理。
  - 同时设置 NO_PROXY / no_proxy，双保险。
"""

import os
import sys
import time
import signal
import subprocess
import json

import httpx
import pytest

from core.config import KEYS_PATH

# ---------------------------------------------------------------------------
# 端口与路径配置（避免与默认 8000/7999 冲突）
# ---------------------------------------------------------------------------
TEST_PORT = 8011
TEST_ENGINE_PORT = 8019
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # E:/llm-service
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"

# 双保险：本进程与子进程都不走代理访问 127.0.0.1
for _p in ("NO_PROXY", "no_proxy"):
    os.environ[_p] = "127.0.0.1,localhost"


def _build_child_env() -> dict:
    env = os.environ.copy()
    env["LLM_PORT"] = str(TEST_PORT)
    env["LLM_ENGINE_PORT"] = str(TEST_ENGINE_PORT)
    # 显式关闭代理，确保子进程内引擎回环调用不被代理拦截
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["HTTP_PROXY"] = ""
    env["HTTPS_PROXY"] = ""
    env["http_proxy"] = ""
    env["https_proxy"] = ""
    return env


@pytest.fixture(scope="session")
def server():
    """启动临时 uvicorn 服务（serve:app），返回 base_url，结束时终止子进程。"""
    env = _build_child_env()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "serve:app",
         "--host", "127.0.0.1", "--port", str(TEST_PORT), "--log-level", "warning"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # 等待就绪：轮询 /api/v1/health，最多 40s
    ready = False
    deadline = time.time() + 40
    last_err = ""
    while time.time() < deadline:
        try:
            with httpx.Client(base_url=BASE_URL, trust_env=False, timeout=2) as c:
                r = c.get("/api/v1/health")
            if r.status_code == 200:
                ready = True
                break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            time.sleep(0.5)
    if not ready:
        # 输出子进程日志便于排查
        try:
            out, _ = proc.communicate(timeout=2)
        except Exception:
            out = ""
        print("\n[conftest] uvicorn failed to start. last_err=%s" % last_err)
        print("[conftest] uvicorn stdout/stderr:\n%s" % (out or "(none)"))
        _terminate(proc)
        pytest.fail("uvicorn server did not become ready in 40s")

    yield BASE_URL

    _terminate(proc)


def _kill_engines() -> None:
    """终止所有游离的 llama-server.exe（引擎子进程可能脱离 uvicorn 树存活，占用大量内存）。"""
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name"]):
            try:
                name = (p.info.get("name") or "").lower()
                if "llama-server.exe" in name:
                    p.kill()
            except Exception:
                pass
    except Exception:
        # psutil 不可用时回退到 taskkill（用 /IM 按映像名杀，避免解析编码问题）
        try:
            subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, shell=True)
        except Exception:
            pass


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            # Windows：用 TaskKill 树杀，确保子进程（引擎）一并退出
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            # 兜底：清除可能游离的引擎子进程（llama-server.exe 占用大量内存）
            _kill_engines()
        else:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@pytest.fixture
def client(server):
    """供测试用例使用的 httpx 客户端（强制不走代理）。"""
    with httpx.Client(base_url=server, trust_env=False, timeout=30) as c:
        yield c


# ---------------------------------------------------------------------------
# 防御性夹具基础设施：把 cache/keys.json 强行重置为给定内容（默认空列表）。
#
# 背景：网关 require_key 在「无任何 Key 配置」时保持开放态；一旦 keys.json 非空，
# 网关即启用鉴权。历史回归中，test_keys.py 在 teardown 写空列表 / 删文件时，
# 因 Windows 下 uvicorn 子进程可能短暂持有文件句柄而静默失败，导致残留 Key
# 污染后续模块（如开放接口测试意外返回 401）。
#
# 这里提供带回试的可靠重置，并在每个测试模块「开始前」自动重置为 []，从而
# 无论上游模块是否清理成功，下游模块都从干净的开放态起步，彻底隔离跨模块污染。
# ---------------------------------------------------------------------------
def _reset_keys_file(data, attempts: int = 30) -> None:
    """带重试地把 keys.json 写入 data；规避 Windows 文件锁导致的单次失败。"""
    for _ in range(attempts):
        try:
            os.makedirs(os.path.dirname(KEYS_PATH), exist_ok=True)
            with open(KEYS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return
        except Exception:
            try:
                os.remove(KEYS_PATH)
            except Exception:
                pass
            time.sleep(0.1)


@pytest.fixture(scope="module", autouse=True)
def _clean_keys():
    """每个测试模块开始前重置 keys.json 为 []（开放态），带重试。"""
    _reset_keys_file([])
    yield

