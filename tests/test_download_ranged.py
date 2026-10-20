# -*- coding: utf-8 -*-
"""多线程 Range 分片下载测试：探测 / 分片拼接 / 取消 / 分片失败。

覆盖 2026-10-20 下载加速改造（core/download.py）：
    - _probe：识别支持 Range 的服务端（206 + content-range）并取回总大小
    - _download_ranged：多线程分片下载后按序拼接为完整文件
    - 取消：任一片检测到 cancel 即抛 _Cancelled（由 _run 统一清理）
    - 任一分片失败：向上抛异常（由 _run 回退单线程重试该源）
"""
import os
import threading

import httpx
import pytest

from core.download import DownloadService, _Cancelled, RANGE_MIN_SIZE

URL = "https://hf-mirror.com/Qwen/R/resolve/main/m-q4_k_m.gguf"

# 12MB > RANGE_MIN_SIZE(8MB)，会切成 3 片（每片至少 4MB）
TOTAL = 12 * 1024 * 1024
BODY = bytes((i * 7 + 13) % 256 for i in range(256)) * (TOTAL // 256)
assert len(BODY) == TOTAL


def _ranged_handler(request: httpx.Request) -> httpx.Response:
    """模拟支持 Range 的服务端：带 Range 头返回 206 + 对应切片。"""
    rng = request.headers.get("range", "")
    if rng.startswith("bytes="):
        s, _, e = rng[len("bytes="):].partition("-")
        start = int(s)
        end = min(int(e) if e else TOTAL - 1, TOTAL - 1)
        chunk = BODY[start:end + 1]
        return httpx.Response(206, content=chunk, headers={
            "content-range": f"bytes {start}-{end}/{TOTAL}",
            "accept-ranges": "bytes",
        })
    return httpx.Response(200, content=BODY)


def _mk(tmp_path, handler):
    return DownloadService(models_dir=str(tmp_path),
                           transport=httpx.MockTransport(handler))


def _seed_task(svc, name):
    with svc._lock:
        svc.tasks[name] = {
            "task_id": "t", "name": name, "status": "downloading",
            "progress": 0.0, "gguf_path": "", "error": None, "cancel": False,
        }


def test_probe_detects_range_support(tmp_path):
    svc = _mk(tmp_path, _ranged_handler)
    with httpx.Client(transport=svc._transport) as client:
        total = svc._probe(client, URL, {})
    assert total == TOTAL


def test_probe_returns_none_without_range(tmp_path):
    """服务端返回 200（不支持 Range）时探测返回 None。"""
    def plain(request):
        return httpx.Response(200, content=b"abc",
                              headers={"content-length": "3"})
    svc = _mk(tmp_path, plain)
    with httpx.Client(transport=svc._transport) as client:
        assert svc._probe(client, URL, {}) is None


def test_ranged_download_assembles(tmp_path):
    svc = _mk(tmp_path, _ranged_handler)
    _seed_task(svc, "m")
    path = os.path.join(str(tmp_path), "m.gguf")
    svc._download_ranged(URL, {}, TOTAL, "m", path)
    assert os.path.exists(path)
    assert os.path.getsize(path) == TOTAL
    with open(path, "rb") as f:
        assert f.read() == BODY
    # 分片临时文件应已清理
    assert not os.path.exists(path + ".part0")


def test_ranged_cancel_raises(tmp_path):
    svc = _mk(tmp_path, _ranged_handler)
    _seed_task(svc, "m")
    path = os.path.join(str(tmp_path), "m.gguf")
    # 预置取消：worker 收到第一个 chunk 即抛 _Cancelled
    assert svc.cancel("m") is True
    with pytest.raises(_Cancelled):
        svc._download_ranged(URL, {}, TOTAL, "m", path)
    assert not os.path.exists(path)
    # _run 的取消路径用 _cleanup 收尾：验证能清掉残留分片/半截文件
    open(path + ".part0", "wb").close()
    open(path, "wb").close()
    svc._cleanup(path)
    assert not os.path.exists(path + ".part0")
    assert not os.path.exists(path)


def test_ranged_failure_raises(tmp_path):
    """任一分片失败 -> 抛异常（_run 据此回退单线程）。"""
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky(request):
        rng = request.headers.get("range", "")
        if rng.startswith("bytes="):
            with lock:
                calls["n"] += 1
                idx = calls["n"]
            if idx == 2:
                raise httpx.ConnectError("boom")
        return _ranged_handler(request)

    svc = _mk(tmp_path, flaky)
    _seed_task(svc, "m")
    path = os.path.join(str(tmp_path), "m.gguf")
    with pytest.raises(Exception):
        svc._download_ranged(URL, {}, TOTAL, "m", path)
    # 未进入拼接，目标文件不应存在
    assert not os.path.exists(path)


def test_small_file_uses_single_thread(tmp_path):
    """注入 MockTransport 时 _run 跳过 Range 探测走单线程（原路径，兼容旧测试）。"""
    from core.download import register_model  # noqa: F401
    svc = _mk(tmp_path, _ranged_handler)
    # monkeypatch register_model 由调用方负责；这里直接验证 _download_single 可写完整文件
    _seed_task(svc, "s")
    path = os.path.join(str(tmp_path), "s.gguf")
    with httpx.Client(transport=svc._transport, follow_redirects=True) as client:
        svc._download_single(client, URL, {}, "s", path)
    assert os.path.getsize(path) == TOTAL


def test_status_reports_bytes_speed_and_threads(tmp_path, monkeypatch):
    """任务状态含已下载字节/总大小/速度/线程数（供前端展示加速效果）。"""
    import time as _t
    monkeypatch.setattr("core.download.register_model", lambda *a, **k: {})
    svc = _mk(tmp_path, _ranged_handler)
    svc.start({"name": "m", "gguf_url": URL})
    deadline = _t.time() + 15
    st = {}
    while _t.time() < deadline:
        st = svc.get_status("m") or {}
        if st.get("status") in ("done", "error", "cancelled"):
            break
        _t.sleep(0.05)
    assert st.get("status") == "done", st
    assert st.get("downloaded") == TOTAL
    assert st.get("total_bytes") == TOTAL
    assert st.get("threads", 0) >= 1
    assert st.get("speed_mbps", 0) > 0

