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


def wait_engine_gone(timeout: float = 90.0) -> bool:
    """
    轮询直到本机不再有 llama-server.exe 进程（确保重型测试卸载后机器恢复空闲，
    避免遗留引擎占用 CPU/内存导致后续轻量用例因服务端饥饿而超时）。
    超时仍有残留则强制终止，保证干净。
    """
    import psutil
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = False
        for p in psutil.process_iter(["name"]):
            if "llama-server.exe" in (p.info.get("name") or "").lower():
                alive = True
                break
        if not alive:
            return True
        time.sleep(1.0)
    # 超时仍有残留：强制终止，避免遗留进程拖慢后续用例
    try:
        for p in psutil.process_iter(["name"]):
            if "llama-server.exe" in (p.info.get("name") or "").lower():
                try:
                    p.kill()
                except Exception:
                    pass
    except Exception:
        pass
    return False
