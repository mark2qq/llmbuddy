# -*- coding: utf-8 -*-
"""
多模型并发引擎池（ModelPool）—— 替代原 serve.py 的单例 engine_proc。

设计要点：
    - 每个「已加载模型」对应一个 PoolEntry，内部为一个 llama-server.exe 子进程，
      独占一个动态分配的内部引擎端口（基端口 LLM_ENGINE_PORT，依次 +2，仅 127.0.0.1）。
    - load() 异步：先置 status=loading，后台线程负责 spawn + 解析日志进度 + 轮询
      /health 直到 200 置 ready；支持 cancel_flag 取消。
    - watchdog：后台线程每 ~15s 检查各 PoolEntry 进程存活，掉线且未主动卸载则按原参数重启。
    - 并发上限 max_concurrent：默认由 system.estimate_max_models 给出，可 set_max_concurrent 覆盖。
"""

import os
import sys
import time
import logging
import threading
import subprocess
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any, List

import httpx

from core.config import resolve_gguf, MODELS_DIR, user_data_dir
from core.system import detect_system, estimate_max_models

logger = logging.getLogger("llm-service")

# 环境变量默认值（与 serve.py 保持一致）
ENGINE_EXE = os.environ.get("LLM_ENGINE", r"E:\llm-service\engine\llama-server.exe")
ENGINE_BASE_PORT = int(os.environ.get("LLM_ENGINE_PORT", "7999"))
ENGINE_API_KEY = os.environ.get("LLM_ENGINE_KEY", "local")
ENGINE_HOST = "127.0.0.1"
# 思考/推理模式（引擎 --reasoning，默认 auto）：
#   auto = 交模板决定（Qwen3 会思考：思考文本进 reasoning_content，正式回答进 content）；
#   on/off = 强制开关。注意：实测部分 Qwen3 GGUF 内置模板硬编码思考，on/off 均无法关闭，
#   此时由网关层将 reasoning_content 合并进 content 兜底（见 routers/gateway.py）。
REASONING = os.environ.get("LLM_REASONING", "auto").strip().lower()


@dataclass
class ModelInstance:
    """对外暴露的「已加载模型」视图（可序列化）。"""
    name: str = ""
    gguf_path: str = ""
    engine_host: str = ENGINE_HOST
    engine_port: int = 0
    engine_api_key: str = ENGINE_API_KEY
    status: str = "loading"          # loading | ready | paused(已加载未运行) | error | unloading
    progress: float = 0.0            # 0~1 加载进度
    loaded_at: Optional[str] = None
    n_ctx: int = 0
    n_threads: int = 0
    n_gpu_layers: int = 0
    reasoning: str = REASONING           # on | off | auto（引擎 --reasoning，默认 auto）

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ModelPoolError(Exception):
    """池操作错误，携带管理接口错误码（1001~1008）。"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _is_port_free(port: int) -> bool:
    """用临时 socket 绑定测试端口是否空闲（仅 loopback）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class PoolEntry:
    """池内一条记录：包裹 ModelInstance + 引擎子进程 + 锁 + 取消标志。"""

    def __init__(self, pool: "ModelPool", name: str, gguf_path: str,
                 n_ctx: int, n_threads: int, n_gpu_layers: int, engine_port: int,
                 reasoning: Optional[str] = None):
        self.pool = pool
        self.model_name = name
        self.instance = ModelInstance(
            name=name, gguf_path=gguf_path, engine_port=engine_port,
            n_ctx=n_ctx, n_threads=n_threads, n_gpu_layers=n_gpu_layers,
            reasoning=reasoning or REASONING,
        )
        self.cancel_flag = False
        self._cancel_event: Optional[threading.Event] = None
        self.proc: Optional[subprocess.Popen] = None
        self.lock = threading.Lock()
        self.log_thread: Optional[threading.Thread] = None

    def base_url(self) -> str:
        return f"http://{self.instance.engine_host}:{self.instance.engine_port}"

    def _build_args(self) -> List[str]:
        ins = self.instance
        args = [
            self.pool.engine_exe, "-m", ins.gguf_path,
            "-c", str(ins.n_ctx),
            "-t", str(ins.n_threads), "-tb", str(ins.n_threads),
            "--host", ins.engine_host, "--port", str(ins.engine_port),
            "--api-key", ins.engine_api_key,
            "--jinja",
        ]
        if ins.reasoning in ("on", "off"):
            args += ["--reasoning", ins.reasoning]
        if ins.n_gpu_layers and ins.n_gpu_layers > 0:
            args += ["-ngl", str(ins.n_gpu_layers)]
        # 注意：不显式指定 --parallel，保持引擎默认单 slot（n_parallel=auto）。
        # 4B 模型在默认 n_ctx=8192 下，多 slot 会吃光内存导致推理 stall（2026-10-19 验证结论）；
        # 真实根因修复是网关/管理接口关闭 keep-alive（连接级释放 slot）+ 引擎日志重定向文件，
        # 单 slot 即可稳定支持连续多次推理（见 tests/test_concurrent_inference.py）。
        return args

    def spawn(self) -> None:
        """拉起引擎子进程。

        ⚠️ 关键：引擎 stdout/stderr 必须重定向到「文件」或 DEVNULL，绝不能用
        subprocess.PIPE。原因：Windows 上 PIPE 缓冲仅约 64KB，引擎在多次推理后会
        突发大量日志（token 统计/计时等），若 Python 读取线程来不及排干，引擎会阻塞在
        写管道上，导致后续推理请求挂起/超时——表现为「第 1 次推理正常、之后偶发卡死」。
        重定向到文件由 OS 缓冲，绝不阻塞子进程。引擎日志写用户可写的
        %LOCALAPPDATA%\\llmBuddy\\logs，便于排查。
        """
        ins = self.instance
        args = self._build_args()
        logger.info("Engine spawn: model=%s port=%d n_ctx=%d threads=%d ngl=%d",
                    ins.name, ins.engine_port, ins.n_ctx, ins.n_threads, ins.n_gpu_layers)
        # 若之前已有日志句柄（看门狗重启场景），先关闭避免泄漏
        if getattr(self, "_engine_log", None) is not None:
            try:
                self._engine_log.close()
            except Exception:
                pass
            self._engine_log = None
        # 引擎日志目录（写入用户可写的 LOCALAPPDATA，避免安装目录不可写；
        # 数据目录名 llmBuddy，旧名 LLMDesktop 由 user_data_dir 兼容回退）
        log_dir = os.path.join(user_data_dir(), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"engine_{ins.name}_{ins.engine_port}.log")
        try:
            self._engine_log = open(log_path, "w", encoding="utf-8", errors="replace")
        except Exception:
            self._engine_log = None
        ins.progress = 0.1
        try:
            # GUI 场景：引擎子进程不得弹出命令行窗口（与 backend_launcher 一致）
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            self.proc = subprocess.Popen(
                args, stdout=self._engine_log or subprocess.DEVNULL,
                stderr=subprocess.STDOUT, cwd=os.path.dirname(self.pool.engine_exe),
                creationflags=flags,
            )
        except Exception:
            if self._engine_log:
                self._engine_log.close()
                self._engine_log = None
            raise

    def wait_ready(self, timeout: int = 180) -> bool:
        """轮询引擎 /health，直到返回 200 或超时/被取消。"""
        url = f"{self.base_url()}/health"
        start = time.time()
        while time.time() - start < timeout:
            if self.cancel_flag or (self._cancel_event is not None and self._cancel_event.is_set()):
                return False
            if self.proc is None or self.proc.poll() is not None:
                return False
            try:
                r = httpx.get(url, timeout=2, trust_env=False)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(1)
        return False

    def unload(self) -> None:
        """终止引擎子进程（不改变池中记录，调用方决定 pause 还是 unload）。"""
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except Exception:
                    self.proc.kill()
            except Exception:
                pass
            self.proc = None
        # 关闭引擎日志文件句柄，避免泄漏
        if getattr(self, "_engine_log", None) is not None:
            try:
                self._engine_log.close()
            except Exception:
                pass
            self._engine_log = None


class ModelPool:
    """多模型并发引擎池（单例）。"""

    def __init__(self, engine_exe: Optional[str] = None, models_dir: Optional[str] = None,
                 engine_base_port: Optional[int] = None, api_key: Optional[str] = None,
                 max_concurrent: Optional[int] = None):
        self.engine_exe = engine_exe or ENGINE_EXE
        self.models_dir = models_dir or MODELS_DIR
        self.engine_base_port = engine_base_port if engine_base_port is not None else ENGINE_BASE_PORT
        self.api_key = api_key or ENGINE_API_KEY

        self.entries: Dict[str, PoolEntry] = {}
        self._port_lock = threading.Lock()
        self._lock = threading.Lock()
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_started = False
        self._shutdown = threading.Event()

        # 默认并发上限：依据系统空闲内存估算
        sys_info = detect_system()
        self.max_concurrent = max_concurrent or sys_info.get("suggested_max_models", 1)

    # ------------------------- 端口分配 -------------------------
    def alloc_port(self) -> int:
        """分配一个空闲且池内未占用的内部引擎端口（基端口起，依次 +2）。"""
        with self._port_lock:
            used = {e.instance.engine_port for e in self.entries.values() if e.instance.engine_port}
            candidate = self.engine_base_port
            while True:
                if candidate not in used and _is_port_free(candidate):
                    return candidate
                candidate += 2
                if candidate > 65535:
                    raise ModelPoolError(1004, "no free engine port available")

    # ------------------------- 加载 -------------------------
    def running_count(self) -> int:
        """当前「运行中」模型数（引擎进程已拉起：loading/ready）。"""
        with self._lock:
            return self._running_locked()

    def _running_locked(self) -> int:
        return sum(1 for e in self.entries.values()
                   if e.instance.status in ("loading", "ready"))

    def can_run_one_more(self) -> bool:
        """是否还能再运行一个模型（已加载未运行的模型不占运行名额）。"""
        with self._lock:
            return self._running_locked() < self.max_concurrent

    def load(self, model_name: str, n_ctx: Optional[int] = None,
             n_threads: Optional[int] = None, n_gpu_layers: Optional[int] = None,
             cancel_event: Optional[threading.Event] = None,
             reasoning: Optional[str] = None) -> PoolEntry:
        """
        加载模型：解析 GGUF -> 分配端口 -> 后台 spawn + 等待就绪。
        立即返回 PoolEntry（status=loading），就绪由后台线程置 ready。

        并发语义（2026-10-19）：
            - 「加载」数量不受 max_concurrent 限制：可同时加载多个模型进入池中；
            - 但同时「运行」受 max_concurrent 限制：已达上限时新模型进入池但
              status=paused（已加载未运行），需先停止一个模型再启动它。
        异常以 ModelPoolError 抛出（1001 未注册 / 1002 缺失 / 1004 端口 / 1005 上限）。
        """
        # 1) 解析 GGUF
        try:
            gguf_path = resolve_gguf(model_name)
        except KeyError:
            raise ModelPoolError(1001, f"model '{model_name}' is not registered")
        except FileNotFoundError as e:
            raise ModelPoolError(1002, f"gguf missing: {e}")

        # 2) 已存在则复用；否则登记进池（运行名额不足时登记为 paused）
        with self._lock:
            existing = self.entries.get(model_name)
            if existing is not None and existing.instance.status in ("loading", "ready", "paused"):
                return existing
            if existing is not None:
                self.entries.pop(model_name, None)
            has_slot = self._running_locked() < self.max_concurrent
            port = self.alloc_port()

        # 3) 引擎参数默认值
        n_ctx = n_ctx or int(os.environ.get("LLM_N_CTX", "32768"))
        n_threads = n_threads or int(os.environ.get("LLM_N_THREADS", str(os.cpu_count() or 8)))
        n_gpu_layers = n_gpu_layers if n_gpu_layers is not None \
            else int(os.environ.get("LLM_N_GPU_LAYERS", "0"))
        reasoning = (reasoning or REASONING).strip().lower()

        entry = PoolEntry(self, model_name, gguf_path, n_ctx, n_threads, n_gpu_layers, port,
                          reasoning=reasoning)
        if cancel_event is not None:
            entry._cancel_event = cancel_event

        with self._lock:
            self.entries[model_name] = entry

        if not has_slot:
            # 运行名额已满：模型已加载进池但不运行，等用户停止一个模型后再「启动」
            entry.instance.status = "paused"
            entry.instance.progress = 0.0
            logger.info("Loaded '%s' paused: run slots full (%d/%d)",
                        model_name, self._running_locked(), self.max_concurrent)
            return entry

        entry.instance.status = "loading"
        entry.instance.progress = 0.05
        threading.Thread(target=self._load_worker, args=(entry,), daemon=True).start()
        return entry

    def _load_worker(self, entry: PoolEntry) -> None:
        """后台加载工作线程：spawn -> 等待就绪 -> 更新状态。"""
        try:
            entry.instance.progress = 0.1
            entry.spawn()
            ok = entry.wait_ready(timeout=180)
            if entry.cancel_flag or (entry._cancel_event is not None and entry._cancel_event.is_set()):
                entry.instance.status = "error"
                entry.instance.progress = 0.0
                entry.unload()
                return
            if ok:
                entry.instance.status = "ready"
                entry.instance.progress = 1.0
                entry.instance.loaded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                logger.info("Engine ready: model=%s port=%d", entry.model_name, entry.instance.engine_port)
            else:
                entry.instance.status = "error"
                logger.error("Engine failed to become ready: model=%s", entry.model_name)
                entry.unload()
        except Exception as e:
            entry.instance.status = "error"
            entry.instance.progress = 0.0
            logger.exception("load worker error: %s", e)

    # ------------------------- 暂停 / 恢复运行 -------------------------
    def pause(self, model_name: str) -> Optional[PoolEntry]:
        """暂停（停止运行）模型：终止引擎进程但保留在池中，可再次启动。"""
        with self._lock:
            entry = self.entries.get(model_name)
            if entry is None:
                return None
        if entry.instance.status == "paused":
            return entry
        entry.cancel_flag = True
        entry.unload()
        entry.cancel_flag = False
        entry.instance.status = "paused"
        entry.instance.progress = 0.0
        logger.info("Model '%s' paused (engine stopped, still loaded)", model_name)
        return entry

    def resume(self, model_name: str) -> PoolEntry:
        """启动（恢复运行）已加载的模型；受运行名额限制。"""
        with self._lock:
            entry = self.entries.get(model_name)
            if entry is None:
                raise ModelPoolError(1001, f"model '{model_name}' is not loaded")
            if entry.instance.status in ("loading", "ready"):
                return entry
            if self._running_locked() >= self.max_concurrent:
                raise ModelPoolError(
                    1005,
                    f"本机当前最多同时运行 {self.max_concurrent} 个模型，"
                    f"请先停止一个模型再启动 '{model_name}'")
            entry.instance.status = "loading"
            entry.instance.progress = 0.05
        threading.Thread(target=self._load_worker, args=(entry,), daemon=True).start()
        logger.info("Model '%s' resuming...", model_name)
        return entry

    # ------------------------- 查询/路由 -------------------------
    def unload(self, model_name: str) -> Optional[PoolEntry]:
        """卸载模型（终止引擎并从池中移除，即「取消加载」）。"""
        with self._lock:
            entry = self.entries.get(model_name)
            if entry is None:
                return None
        entry.cancel_flag = True
        entry.instance.status = "unloading"
        entry.unload()
        with self._lock:
            self.entries.pop(model_name, None)
        logger.info("Model '%s' unloaded (removed from pool)", model_name)
        return entry

    def get(self, model_name: str) -> Optional[PoolEntry]:
        with self._lock:
            return self.entries.get(model_name)

    def route(self, model_name: str) -> Optional[str]:
        """返回该模型引擎 base url；仅运行中（ready）才可路由，暂停/未就绪返回 None。"""
        entry = self.get(model_name)
        if entry is None or entry.instance.status != "ready":
            return None
        return entry.base_url()

    def list_loaded(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.instance.to_dict() for e in self.entries.values()]

    def set_max_concurrent(self, n: int) -> None:
        self.max_concurrent = max(1, int(n))

    # ------------------------- 看门狗（自愈） -------------------------
    def start_watchdog(self) -> None:
        """启动后台看门狗线程（幂等）。"""
        if self._watchdog_started:
            return
        self._watchdog_started = True
        self._shutdown.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        logger.info("Engine watchdog started")

    def _watchdog_loop(self) -> None:
        while not self._shutdown.is_set():
            self._shutdown.wait(15)
            if self._shutdown.is_set():
                break
            try:
                with self._lock:
                    snapshot = list(self.entries.values())
                for entry in snapshot:
                    if entry.instance.status in ("loading", "ready") and not entry.cancel_flag:
                        if entry.proc is None or entry.proc.poll() is not None:
                            logger.warning("Watchdog: engine for '%s' down, restarting...", entry.model_name)
                            try:
                                entry.proc = None
                                entry.instance.status = "loading"
                                entry.instance.progress = 0.1
                                entry.spawn()
                                ok = entry.wait_ready(timeout=180)
                                if ok:
                                    entry.instance.status = "ready"
                                    entry.instance.progress = 1.0
                                    entry.instance.loaded_at = time.strftime(
                                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                                    logger.info("Watchdog: restarted '%s' ok", entry.model_name)
                                else:
                                    entry.instance.status = "error"
                                    logger.error("Watchdog: restart '%s' failed", entry.model_name)
                            except Exception as e:
                                logger.error("Watchdog restart failed: %s", e)
                                entry.instance.status = "error"
            except Exception:
                pass

    def shutdown(self) -> None:
        """优雅停机：置关闭标志 + 卸载所有模型。"""
        self._shutdown.set()
        with self._lock:
            names = list(self.entries.keys())
        for name in names:
            try:
                self.unload(name)
            except Exception:
                pass
        logger.info("ModelPool shutdown: all engines unloaded")


# ----------------------------------------------------------------------------
# 单例访问
# ----------------------------------------------------------------------------
_pool: Optional[ModelPool] = None
_pool_lock = threading.Lock()


def get_model_pool() -> ModelPool:
    """获取（懒创建）ModelPool 单例。"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ModelPool()
    return _pool


def init_model_pool(engine_exe: Optional[str] = None, models_dir: Optional[str] = None,
                    engine_base_port: Optional[int] = None, api_key: Optional[str] = None,
                    max_concurrent: Optional[int] = None) -> ModelPool:
    """显式初始化 ModelPool 单例（供 serve.py 启动时调用）。"""
    global _pool
    with _pool_lock:
        _pool = ModelPool(engine_exe=engine_exe, models_dir=models_dir,
                          engine_base_port=engine_base_port, api_key=api_key,
                          max_concurrent=max_concurrent)
    return _pool
