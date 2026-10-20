# -*- coding: utf-8 -*-
"""
后端进程看护器（backend_launcher.py）。

职责（服务级守护）：
    - 构造 env（注入 LLM_* 环境变量）并通过 subprocess.Popen 拉起 serve.py
    - 后台线程读取子进程 stdout/stderr 转发到日志信号
    - 后台 watch 线程轮询 /api/v1/health 判定就绪，并检测崩溃
    - 崩溃时按 auto_restart 自动重启
    - stop() 优雅停机（先调 M15 再 terminate 等待）后清理

注意：后端读取的引擎路径环境变量为 LLM_ENGINE；同时按需求文档约定
也写入 LLM_ENGINE_EXE（后端忽略但不影响）。
"""
import os
import sys
import time
import signal
import subprocess
import threading
from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.api_client import ApiClient


class BackendLauncher(QObject):
    """后端子进程看护器。"""

    state_changed = Signal(str)            # starting|running|stopped|error
    log = Signal(str)                       # 日志行
    ready = Signal(bool)                   # 是否就绪

    def __init__(self, config, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.config = config
        self.proc: Optional[subprocess.Popen] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # 路径定位
    # ------------------------------------------------------------------ #
    def _install_dir(self) -> str:
        """打包态 exe 的真实所在目录（one-file 模式下 sys.executable 在临时目录，需用 argv[0]）。"""
        return os.path.dirname(os.path.abspath(sys.argv[0]))

    def _frozen(self) -> bool:
        """是否 PyInstaller 打包态（后端内嵌于 exe，以 --backend 模式运行）。"""
        return getattr(sys, "frozen", False)

    def _locate_backend_script(self) -> str:
        """定位后端入口脚本：配置 > 开发态父目录 serve.py > 打包态 backend/serve.py。"""
        cfg_script = getattr(self.config, "backend_script", "")
        if cfg_script and os.path.exists(cfg_script):
            return cfg_script
        # 开发态：client 父目录 / serve.py（E:/llm-service/serve.py）
        dev = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "serve.py")
        if os.path.exists(dev):
            return dev
        # 打包态：exe 同目录 / backend/serve.py（源码形式附带，便于高级用户直接运行）
        pkg = os.path.join(self._install_dir(), "backend", "serve.py")
        if os.path.exists(pkg):
            return pkg
        # 退路：本目录 serve.py
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "serve.py")

    def _locate_python(self) -> str:
        """定位运行后端的 Python 解释器（打包态即 exe 自身）。"""
        if self._frozen():
            return sys.executable
        exe = getattr(self.config, "python_exe", "")
        if exe and os.path.exists(exe):
            return exe
        # 开发态：backend/venv/Scripts/python.exe
        venv_py = os.path.join(self._install_dir(), "backend", "venv", "Scripts", "python.exe")
        if os.path.exists(venv_py):
            return venv_py
        return sys.executable

    def _build_cmd(self):
        """构造后端子进程命令与工作目录。

        打包态：[exe, --backend]（内嵌 serve.py，无需外部 Python 与脚本文件）；
        开发态：[python, serve.py]。
        """
        if self._frozen():
            return [sys.executable, "--backend"], self._install_dir()
        script = self._locate_backend_script()
        py = self._locate_python()
        return [py, script], os.path.dirname(script)

    # ------------------------------------------------------------------ #
    # 环境变量构造
    # ------------------------------------------------------------------ #
    def _build_env(self) -> dict:
        """基于配置构造后端进程环境变量。"""
        env = os.environ.copy()
        cfg = self.config
        env["LLM_HOST"] = str(cfg.gateway_host)
        env["LLM_PORT"] = str(cfg.gateway_port)
        env["LLM_ENGINE_PORT"] = str(cfg.engine_base_port)
        env["LLM_MODELS_DIR"] = str(cfg.models_dir)
        env["LLM_N_CTX"] = str(cfg.n_ctx)
        env["LLM_N_THREADS"] = str(cfg.n_threads)
        env["LLM_N_GPU_LAYERS"] = str(cfg.n_gpu_layers)
        if getattr(cfg, "api_key", ""):
            env["LLM_API_KEY"] = str(cfg.api_key)
        if getattr(cfg, "default_model", ""):
            env["LLM_DEFAULT_MODEL"] = str(cfg.default_model)
        # 引擎可执行文件：后端实际读取 LLM_ENGINE；同时兼容需求文档的 LLM_ENGINE_EXE
        # 打包态且未配置时，默认定位到安装目录 engine/llama-server.exe
        engine = getattr(cfg, "engine_exe", "")
        if not engine and self._frozen():
            engine = os.path.join(self._install_dir(), "engine", "llama-server.exe")
        if not engine:
            engine = r"E:\llm-service\engine\llama-server.exe"
        env["LLM_ENGINE"] = engine
        env["LLM_ENGINE_EXE"] = engine
        env["LLM_ENGINE_KEY"] = "local"
        # 保证子进程日志中文不乱码
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUNBUFFERED"] = "1"
        return env

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        """拉起后端子进程；成功返回 True。"""
        if self._running:
            return True
        cmd, cwd = self._build_cmd()
        self.state_changed.emit("starting")
        self.log.emit(f"[启动] {' '.join(cmd)}")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            self.proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                env=self._build_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
                text=True,
                bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            self.log.emit(f"[启动异常] {exc}")
            self.state_changed.emit("error")
            return False

        self._running = True
        self._stop_event.clear()
        threading.Thread(target=self._log_reader, daemon=True).start()
        self._watch_thread = threading.Thread(target=self._watch, daemon=True)
        self._watch_thread.start()
        return True

    def _spawn(self) -> None:
        """内部：重新 spawn 子进程（用于崩溃重启）。"""
        cmd, cwd = self._build_cmd()
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=self._build_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            text=True,
            bufsize=1,
        )

    def _log_reader(self) -> None:
        """后台线程：读取子进程 stdout 并转发到日志信号。"""
        proc = self.proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.log.emit(line.rstrip())
        except Exception:
            pass

    def _api(self) -> ApiClient:
        return ApiClient(self.config.gateway_host, self.config.gateway_port,
                         getattr(self.config, "api_key", ""))

    def _watch(self) -> None:
        """后台线程：轮询健康 + 崩溃检测 + 自动重启。"""
        ready_ok = False
        while not self._stop_event.is_set():
            # 进程是否还活着
            if self.proc is None or self.proc.poll() is not None:
                self._running = False
                self.log.emit("[看护] 后端进程已退出")
                self.state_changed.emit("stopped")
                self.ready.emit(False)
                if getattr(self.config, "auto_restart", True) and not self._stop_event.is_set():
                    self.log.emit("[看护] 尝试重启后端…")
                    try:
                        self._spawn()
                        self._running = True
                        threading.Thread(target=self._log_reader, daemon=True).start()
                        ready_ok = False
                        continue
                    except Exception as exc:  # noqa: BLE001
                        self.log.emit(f"[看护] 重启失败: {exc}")
                        break
                else:
                    break

            # 探测就绪
            try:
                info = self._api().health()
                ok = isinstance(info, dict) and (
                    "status" in info or "pool" in info or "gateway" in info)
                if ok and not ready_ok:
                    ready_ok = True
                    self.ready.emit(True)
                    self.state_changed.emit("running")
                    self.log.emit("[看护] 后端已就绪")
                elif not ok and ready_ok:
                    ready_ok = False
                    self.ready.emit(False)
            except Exception:
                if ready_ok:
                    ready_ok = False
                    self.ready.emit(False)
            time.sleep(1.5)

    @staticmethod
    def _kill_process_tree(proc) -> None:
        """终止后端进程及其全部子孙进程（含 llama-server.exe 推理引擎子进程）。

        仅托盘「退出」/「停止服务」时调用，避免退出后残留孤儿引擎进程占用端口与显存
        （2026-10-20 缺陷修复：原先只 kill serve.py，其派生的 llama-server.exe 变孤儿）。

        Windows 优先用 ``taskkill /F /T /PID`` 杀整棵进程树（无需额外依赖）；
        回退到 psutil 递归杀子孙；POSIX 用进程组信号。
        """
        if proc is None:
            return
        # 支持传入 Popen 对象或进程 PID（int）：外部后端只能按 PID 定位
        pid = proc if isinstance(proc, int) else getattr(proc, "pid", None)
        if pid is None:
            return
        if sys.platform == "win32":
            # taskkill /T 杀整棵进程树（含 llama-server.exe 引擎子进程），无需额外依赖
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                return
            except Exception:  # noqa: BLE001
                pass
            # 回退：psutil 递归杀子孙
            try:
                import psutil
                p = psutil.Process(pid)
                for c in p.children(recursive=True):
                    try:
                        c.kill()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                pass
        else:
            try:
                import psutil
                p = psutil.Process(pid)
                for c in p.children(recursive=True):
                    try:
                        c.kill()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------ #
    # 外部后端清理（GUI 未持有子进程句柄时）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_backend_process(pid: int) -> bool:
        """判断 PID 是否为 llmBuddy 后端进程（避免误杀恰好占用同一端口的其它程序）。"""
        try:
            import psutil
            p = psutil.Process(pid)
            name = (p.name() or "").lower()
            if "llmbuddy" in name:
                return True
            if "python" in name or "pythonw" in name:
                try:
                    cmd = " ".join(p.cmdline() or []).lower()
                except Exception:  # noqa: BLE001
                    return False
                return ("serve.py" in cmd) or ("--backend" in cmd)
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    def _find_backend_pids(port: int) -> list:
        """查找监听指定端口的 llmBuddy 后端进程 PID。"""
        pids = []
        try:
            import psutil
            for c in psutil.net_connections(kind="inet"):
                try:
                    listening = (c.status == psutil.CONN_LISTEN
                                 or c.status == "LISTEN")
                    if not listening or not c.laddr or not c.pid:
                        continue
                    if int(c.laddr.port) != int(port):
                        continue
                    if BackendLauncher._is_backend_process(int(c.pid)):
                        pids.append(int(c.pid))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass
        return list(dict.fromkeys(pids))

    def _stop_external_backend(self) -> bool:
        """停止不由本进程拉起、但目前仍在监听网关端口的后端。

        典型场景：后端由上一个 GUI 实例/手动启动，本 GUI 的 proc 为 None。
        此前 stop() 在「无 proc」时直接返回，导致点了「停止服务」后端仍在跑
        （红字缺陷：服务停止后状态栏仍显示运行中——其实服务根本没停）。

        返回是否确认服务已不可达。
        """
        try:
            port = int(getattr(self.config, "gateway_port", 8000) or 8000)
        except Exception:  # noqa: BLE001
            port = 8000
        if not self.is_ready():
            return True
        # 1) 先优雅停机（M15），能正常退出最干净
        try:
            self._api().stop_service()
        except Exception:  # noqa: BLE001
            pass
        for _ in range(20):
            time.sleep(0.25)
            if not self.is_ready():
                return True
        # 2) 仍可达 -> 按端口定位后端 PID，杀整棵进程树（含引擎子进程）
        for pid in self._find_backend_pids(port):
            try:
                self._kill_process_tree(pid)
            except Exception:  # noqa: BLE001
                pass
        # 3) 再确认一次（留出端口释放时间）
        for _ in range(12):
            if not self.is_ready():
                return True
            time.sleep(0.25)
        return not self.is_ready()

    def stop(self) -> None:
        """停止后端：优雅停机（M15）后 terminate 等待，并清理全部引擎子进程。"""
        if not self._running and self.proc is None:
            # GUI 未持有后端进程（先启动后端再开客户端 / 旧实例拉起）：
            # 按端口定位并清理，否则「停止服务」只是发了个空信号，服务继续运行
            self._stop_external_backend()
            self._running = False
            self.state_changed.emit("stopped")
            self.ready.emit(False)
            return
        self._stop_event.set()
        # 先尝试优雅停机
        try:
            self._api().stop_service()
        except Exception:
            pass
        # 终止进程（含全部子孙）
        if self.proc is not None:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=8)
                except Exception:
                    pass
            except Exception:
                pass
            # 兜底：无论是否优雅退出，都确保 serve.py 及其全部引擎子进程
            #（llama-server.exe）被清理，避免退出后残留孤儿进程占用端口/显存
            #（托盘「退出」缺陷修复）
            try:
                self._kill_process_tree(self.proc)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        self._running = False
        self.state_changed.emit("stopped")
        self.ready.emit(False)

    def is_running(self) -> bool:
        """子进程是否仍在运行。"""
        return self._running and self.proc is not None and self.proc.poll() is None

    def is_ready(self) -> bool:
        """后端是否可服务（探测 health）。"""
        try:
            self._api().health()
            return True
        except Exception:
            return False

    def kill_hard(self) -> None:
        """强制杀掉后端（仅托盘退出时使用，不等优雅停机，并清理全部引擎子进程）。

        关键：用 _kill_process_tree 一并杀掉 serve.py 派生的 llama-server.exe，
        否则退出后引擎进程残留成孤儿，占用 7999+ 端口与显存（2026-10-20 缺陷修复）。
        """
        self._stop_event.set()
        if self.proc is not None:
            try:
                self._kill_process_tree(self.proc)
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        else:
            # 未持有句柄（外部后端）：按端口清理，避免退出后服务仍在跑
            try:
                self._stop_external_backend()
            except Exception:  # noqa: BLE001
                pass
        self._running = False
