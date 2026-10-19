# -*- coding: utf-8 -*-
"""
测试辅助：触发模型加载并轮询状态。

本机 CPU 加载 2.5GB 级 GGUF 可能较慢，且空闲内存不足时引擎可能进入 error。
本辅助用于在「不阻塞过久」的前提下，观察加载状态，并仅在就绪时做推理，
从而稳定验证「接口非 500 / 服务不崩溃」（符合需求中「接受 error，不算 Bug」）。
"""

import time


def load_and_wait(client, model: str, timeout: float = 150.0, poll: float = 2.0) -> str:
    """
    触发加载（M9）并轮询 M10 状态，直到 ready / error / 超时。
    返回最终状态字符串：ready | error | loading（超时仍未就绪）| unloaded。
    """
    try:
        client.post(f"/api/v1/pool/load", json={"model": model}, timeout=15)
    except Exception:
        pass
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
        time.sleep(poll)
    return status


# 测试专属引擎端口区间（与 conftest.TEST_ENGINE_PORT 保持一致）。
# 只清理该区间的 llama-server 进程，避免误杀用户正在使用的安装版引擎（默认 7999）
# 或其他 teammate 手工起的 E2E 实例（8030+）。
# 测试自身最多并发 1~2 个模型，alloc_port 从 8019 起 +2 递增，故上限取 8029。
TEST_ENGINE_PORT_MIN = 8019
TEST_ENGINE_PORT_MAX = 8029


def _engine_port(cmdline) -> int:
    """从引擎命令行里解析 --port 值。"""
    try:
        for i, tok in enumerate(cmdline):
            if tok == "--port" and i + 1 < len(cmdline):
                return int(cmdline[i + 1])
    except Exception:
        pass
    return -1


def iter_test_engines(orphans_only: bool = False):
    """遍历「测试端口区间内」的 llama-server 进程（不再按映像名一把杀）。

    orphans_only=True 时只返回「父进程已消失」的游离引擎 —— 用于 pytest 收尾清理，
    避免把其他 teammate 正在跑测试的活引擎一起杀掉。
    """
    import psutil
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if "llama-server.exe" not in (p.info.get("name") or "").lower():
                continue
            port = _engine_port(p.cmdline())
            if not (TEST_ENGINE_PORT_MIN <= port <= TEST_ENGINE_PORT_MAX):
                continue
            if orphans_only:
                try:
                    parent = psutil.Process(p.pid).parent()
                    if parent is not None and parent.is_running():
                        continue
                except Exception:
                    continue
            yield p
        except Exception:
            continue


def wait_engine_gone(timeout: float = 90.0) -> bool:
    """
    轮询直到「测试专用端口区间」内不再有 llama-server.exe 进程（确保重型测试卸载后
    机器恢复空闲，避免遗留引擎占用 CPU/内存拖慢后续用例）。超时仍有残留则强制终止。

    注意：只针对 8019~8099 的测试引擎，不会影响用户安装版实例（7999）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(True for _ in iter_test_engines()):
            return True
        time.sleep(1.0)
    # 超时仍有残留：强制终止，避免遗留进程拖慢后续用例
    for p in list(iter_test_engines()):
        try:
            p.kill()
        except Exception:
            pass
    return False
