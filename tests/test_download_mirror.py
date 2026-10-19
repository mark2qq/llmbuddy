# -*- coding: utf-8 -*-
"""下载镜像回退测试：HF 直连失败时自动切 hf-mirror，全失败时报错并提示手动添加。"""
import os
import time

import httpx
import pytest

from core.download import DownloadService, DownloadService as _DS  # noqa: F401

CHUNK = b"x" * 1024


def _wait_done(svc, name, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = svc.get_status(name) or {}
        if st.get("status") in ("done", "error", "cancelled"):
            return st
        time.sleep(0.05)
    return svc.get_status(name) or {}


def _mk_handler(mirror_ok=True, origin_ok=False):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        if "hf-mirror.com" in host:
            if mirror_ok:
                return httpx.Response(200, headers={"content-length": str(len(CHUNK))},
                                      content=CHUNK)
            raise httpx.ConnectError("mirror down")
        if "huggingface.co" in host:
            if origin_ok:
                return httpx.Response(200, headers={"content-length": str(len(CHUNK))},
                                      content=CHUNK)
            raise httpx.ConnectError("[WinError 10054] 远程主机强迫关闭了一个现有的连接。")
        raise httpx.ConnectError("unknown host")
    return handler


@pytest.fixture
def svc_factory(tmp_path, monkeypatch):
    # 防止测试把假模型写进真实 cache/registry.json
    monkeypatch.setattr("core.download.register_model", lambda *a, **k: {})

    def _make(mirror_ok=True, origin_ok=False):
        t = httpx.MockTransport(_mk_handler(mirror_ok, origin_ok))
        return DownloadService(models_dir=str(tmp_path), transport=t)
    return _make


def test_mirror_fallback_success(svc_factory, tmp_path):
    """直连 10054 → 自动切 hf-mirror 成功。"""
    svc = svc_factory(mirror_ok=True, origin_ok=False)
    svc.start({"name": "m1", "gguf_url":
               "https://huggingface.co/Qwen/R/resolve/main/m1-q4_k_m.gguf"})
    st = _wait_done(svc, "m1")
    assert st["status"] == "done"
    assert "hf-mirror.com" in st.get("via", "")
    p = os.path.join(str(tmp_path), "m1-q4_k_m.gguf")
    assert os.path.exists(p) and os.path.getsize(p) == len(CHUNK)


def test_origin_fallback_when_mirror_down(svc_factory, tmp_path):
    """镜像也挂 → 回退原直链成功。"""
    svc = svc_factory(mirror_ok=False, origin_ok=True)
    svc.start({"name": "m2", "gguf_url":
               "https://huggingface.co/Qwen/R/resolve/main/m2.gguf"})
    st = _wait_done(svc, "m2")
    assert st["status"] == "done"
    assert "huggingface.co" in st.get("via", "")


def test_all_failed_error_hint(svc_factory):
    """全失败 → error 且提示手动添加。"""
    svc = svc_factory(mirror_ok=False, origin_ok=False)
    svc.start({"name": "m3", "gguf_url":
               "https://huggingface.co/Qwen/R/resolve/main/m3.gguf"})
    st = _wait_done(svc, "m3")
    assert st["status"] == "error"
    assert "手动添加" in (st.get("error") or "")


def test_non_hf_source_untouched(svc_factory, tmp_path):
    """非 HF 源不做镜像改写。"""
    svc = svc_factory(mirror_ok=True, origin_ok=False)
    svc.start({"name": "m4", "gguf_url":
               "https://modelscope.cn/models/x/resolve/master/m4.gguf"})
    st = _wait_done(svc, "m4")
    assert st["status"] == "error"  # handler 对未知 host 抛错，说明未被改写成镜像
