# -*- coding: utf-8 -*-
"""
多模型「加载 / 启动 / 停止 / 取消加载」语义测试（2026-10-19）。

覆盖：
    - 可同时加载多个模型进入池（不受 max_concurrent 限制）
    - 运行名额满时新模型为 paused（已加载未运行）
    - 暂停（stop）保留池记录、释放运行名额
    - 恢复运行（start）在名额满时报 1005
    - 取消加载（unload）从池移除
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.pool import ModelPool, ModelPoolError  # noqa: E402


class FakeEntry:
    """模拟引擎进程（不真正 spawn llama-server）。"""

    def __init__(self, pool, name):
        self.pool = pool
        self.model_name = name
        self.instance = type("Ins", (), {
            "status": "loading", "progress": 0.0, "engine_port": 0,
        })()
        self.proc = type("P", (), {"poll": lambda self: None})()
        self.cancel_flag = False
        self._cancel_event = None

    def base_url(self):
        return "http://127.0.0.1:%d" % self.instance.engine_port

    def unload(self):
        self.proc = None


def _make_pool(monkey_dir, max_concurrent):
    pool = ModelPool(engine_exe="llama-server.exe", models_dir=str(monkey_dir),
                     engine_base_port=7999, max_concurrent=max_concurrent)
    # 替换真实 spawn/wait_ready：直接置 ready
    import core.pool as pool_mod

    def fake_spawn(entry_self):
        entry_self.instance.progress = 1.0

    def fake_wait_ready(entry_self, timeout=180):
        return True

    pool_mod.PoolEntry.spawn = fake_spawn
    pool_mod.PoolEntry.wait_ready = fake_wait_ready
    pool_mod.PoolEntry.unload = lambda self: setattr(self, "proc", None)
    return pool


def _wait_status(pool, name, want, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        e = pool.get(name)
        if e is not None and e.instance.status == want:
            return True
        time.sleep(0.1)
    return False


def test_pool_pause_resume_semantics(tmp_path, monkeypatch):
    gguf = tmp_path / "m1.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setenv("LLM_MODELS_DIR", str(tmp_path))
    monkeypatch.setattr("core.pool.resolve_gguf", lambda name: str(gguf))

    pool = _make_pool(tmp_path, max_concurrent=1)

    # 1) 加载第一个模型 -> 运行中
    pool.load("m1")
    assert _wait_status(pool, "m1", "ready"), "m1 应进入 ready"

    # 2) 运行名额已满，仍可加载第二个模型 -> paused（已加载未运行）
    e2 = pool.load("m2")
    assert e2.instance.status == "paused", "名额满时新模型应为 paused"
    assert pool.running_count() == 1, "paused 不占运行名额"
    assert len(pool.list_loaded()) == 2, "两个模型都应在池中（支持多模型加载）"

    # 3) 运行 m2 应被拒绝（1005）
    try:
        pool.resume("m2")
        raise AssertionError("名额满时 resume 应抛 1005")
    except ModelPoolError as e:
        assert e.code == 1005, f"应为 1005，实际 {e.code}"
        assert "停止一个模型" in e.message, "错误信息应提示先停止一个模型"

    # 4) 停止 m1 -> 暂停但保留加载，运行名额释放
    pool.pause("m1")
    assert pool.get("m1") is not None, "停止（暂停）不应移除池记录"
    assert pool.get("m1").instance.status == "paused"
    assert pool.running_count() == 0, "暂停后运行数应为 0"

    # 5) 此时可启动 m2
    pool.resume("m2")
    assert _wait_status(pool, "m2", "ready"), "m2 应可启动"
    assert pool.running_count() == 1

    # 6) paused 模型不参与路由；ready 模型才路由
    assert pool.route("m1") is None, "暂停模型不应被路由"
    assert pool.route("m2") is not None

    # 7) 取消加载：从池移除
    pool.unload("m1")
    assert pool.get("m1") is None, "取消加载应从池中移除"
    pool.unload("m2")
    assert len(pool.list_loaded()) == 0

    pool.shutdown()


def test_pool_load_same_model_is_idempotent(tmp_path, monkeypatch):
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    monkeypatch.setattr("core.pool.resolve_gguf", lambda name: str(gguf))
    pool = _make_pool(tmp_path, max_concurrent=2)

    a = pool.load("m")
    assert _wait_status(pool, "m", "ready")
    b = pool.load("m")
    assert a is b, "重复 load 同一模型应复用现有记录"
    assert pool.running_count() == 1
    pool.shutdown()