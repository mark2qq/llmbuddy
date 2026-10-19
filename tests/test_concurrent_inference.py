# -*- coding: utf-8 -*-
"""
回归用例：同实例连续多次推理（修复「仅第 1 次成功、之后全部挂起超时」缺陷）。

缺陷根因：llama.cpp 引擎默认仅 1 个 slots，且后端用 httpx 默认 keep-alive 长连接
占用该 slot；第 1 次推理后连接不释放，导致后续请求新建连接却抢不到 slot 而挂起，
直到客户端 ReadTimeout（50~60s）。修复：关闭后端→引擎的 keep-alive（Connection: close）。

本用例的关键点：用**同一个已加载实例**连续完成多次推理（中途不 unload），
这正是旧用例因「每用例结束都 unload+kill 引擎、每用例全新引擎」而掩盖的缺陷路径。

注意端口：后端 8051 / 引擎基端口 8059，避开默认 8000/7999 与 conftest 共享的 8011/8019。
"""

import os
import sys
import time
import subprocess

import httpx
import pytest

# ---------------------------------------------------------------------------
# 端口与路径（避开 8000/7999 以及 conftest 共享的 8011/8019）
# ---------------------------------------------------------------------------
TEST_PORT = 8051
TEST_ENGINE_PORT = 8059
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # E:/llm-service
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
LIGHT_MODEL = "qwen3-4b"

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


def _start_server() -> subprocess.Popen:
    """启动临时 uvicorn 服务（serve:app），返回子进程。"""
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
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with httpx.Client(base_url=BASE_URL, trust_env=False, timeout=2) as c:
                r = c.get("/api/v1/health")
            if r.status_code == 200:
                return proc
        except Exception:
            time.sleep(0.5)
    # 超时：输出子进程日志便于排查
    try:
        out, _ = proc.communicate(timeout=2)
    except Exception:
        out = ""
    _terminate(proc)
    pytest.fail("uvicorn server did not become ready in 40s. stdout:\n%s" % (out or "(none)"))


def _kill_engines() -> None:
    """终止测试区间（8019~8099）内游离的 llama-server.exe。

    与 conftest._kill_engines 一致：不再按映像名全杀，避免误杀用户安装版引擎（7999）。
    """
    try:
        from helpers import iter_test_engines
        for p in list(iter_test_engines()):
            try:
                p.kill()
            except Exception:
                pass
    except Exception:
        pass


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10)
            _kill_engines()
        else:
            proc.terminate()
            proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@pytest.fixture
def backend():
    """启动后端服务（端口 8051），测试结束终止并清理引擎子进程。"""
    proc = _start_server()
    yield BASE_URL
    _terminate(proc)


def _wait_model_ready(client: httpx.Client, model: str, timeout: float = 180.0) -> str:
    """轮询 M10 状态直到 ready / error / 超时，返回最终状态。"""
    deadline = time.time() + timeout
    status = "loading"
    while time.time() < deadline:
        try:
            r = client.get(f"/api/v1/pool/{model}", timeout=10)
        except Exception:
            break
        if r.status_code == 200:
            status = r.json()["data"]["status"]
            if status in ("ready", "error"):
                break
        elif r.status_code == 404:
            status = "unloaded"
            break
        time.sleep(1.0)
    return status


def test_concurrent_inference_same_instance(backend):
    """
    同实例连续多次推理回归：验证 keep-alive 修复后，第 2~N 次请求不再挂起。

    流程（全程不 unload，使用同一已加载实例）：
      1. 加载 qwen3-4b（n_ctx=4096, n_threads=8），轮询直至 ready（180s）。
      2. 连续 3 次 网关 chat/completions（stream=false），每次断言 200 且 content 非空、60s 内返回。
      3. 连续 2 次 M12 /test 直连，断言 data.ok == True。
      4. 卸载。
    """
    with httpx.Client(base_url=backend, trust_env=False, timeout=30) as client:
        # ---- 1) 加载并等待就绪 ----
        r = client.post("/api/v1/pool/load", json={
            "model": LIGHT_MODEL, "n_ctx": 4096, "n_threads": 8}, timeout=15)
        assert r.status_code == 200, r.text
        assert r.json()["code"] == 0, r.json()

        status = _wait_model_ready(client, LIGHT_MODEL, timeout=180)
        assert status == "ready", f"模型未能就绪（status={status}），无法验证连续推理"

        # ---- 2) 连续 3 次网关 chat/completions ----
        chat_payload = {
            "model": LIGHT_MODEL,
            "messages": [{"role": "user", "content": "1+1=? 只答数字"}],
            "max_tokens": 32,
            "stream": False,
        }
        for i in range(3):
            t0 = time.time()
            r = client.post("/v1/chat/completions", json=chat_payload, timeout=60)
            elapsed = time.time() - t0
            assert r.status_code == 200, f"第 {i+1} 次网关推理失败: {r.text}"
            data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content")
            assert content is not None and content.strip() != "", \
                f"第 {i+1} 次网关推理 content 为空: {data}"
            assert elapsed < 60, f"第 {i+1} 次网关推理耗时 {elapsed:.1f}s 超过 60s 阈值"
            print(f"\n[regression] 第 {i+1} 次网关推理 OK，content={content!r}，耗时 {elapsed:.1f}s")

        # ---- 3) 连续 2 次 M12 直连测试 ----
        for i in range(2):
            t0 = time.time()
            r = client.get(f"/api/v1/pool/{LIGHT_MODEL}/test",
                           params={"prompt": "hi"}, timeout=70)
            elapsed = time.time() - t0
            assert r.status_code == 200, f"第 {i+1} 次 M12 测试失败: {r.text}"
            body = r.json()
            assert body["code"] == 0, body
            assert body["data"]["ok"] is True, \
                f"第 {i+1} 次 M12 测试 ok != True: {body}"
            assert elapsed < 70, f"第 {i+1} 次 M12 测试耗时 {elapsed:.1f}s 超阈值"
            print(f"\n[regression] 第 {i+1} 次 M12 直连 OK，sample={body['data'].get('sample')!r}，"
                  f"耗时 {elapsed:.1f}s")

        # ---- 4) 卸载 ----
        r = client.post(f"/api/v1/pool/{LIGHT_MODEL}/unload", timeout=10)
        assert r.status_code == 200, r.text
        assert r.json()["code"] == 0, r.json()
