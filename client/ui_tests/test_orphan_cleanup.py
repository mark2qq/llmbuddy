# -*- coding: utf-8 -*-
"""
孤儿进程清理回归测试（2026-10-20 v1.0.6 缺陷修复）。

需求：托盘「退出」调用 BackendLauncher.kill_hard() 时，必须连 serve.py 派生的
llama-server.exe 引擎子进程一并杀掉，不能让它们残留成孤儿占用端口/显存。

验证点（用「父 python 派生子 python(sleep)」模拟 serve.py -> llama-server.exe 的进程树）：
    1. 调用 BackendLauncher._kill_process_tree(父) 后，父进程被终止；
    2. 父进程派生的子进程（孙子）也一并被终止，不再残留为孤儿。
"""
import os
import sys
import time
import subprocess

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _is_alive(pid):
    """跨平台判活：优先 psutil，Windows 回退 tasklist，POSIX 回退 os.kill(pid,0)。"""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001
        pass
    if sys.platform == "win32":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        ).decode("utf-8", "replace")
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _spawn_tree_parent():
    """拉起一个父 python，它会再派生一个子 python（sleep 60），并打印子 pid。"""
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "sys.stdout.write(str(child.pid) + '\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, creationflags=flags,
    )


def _force_cleanup(parent, child_pid):
    """兜底清理，避免测试异常时残留 sleep 进程。"""
    try:
        if parent.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(parent.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    except Exception:  # noqa: BLE001
        pass
    try:
        if _is_alive(child_pid):
            subprocess.run(["taskkill", "/F", "/PID", str(child_pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    except Exception:  # noqa: BLE001
        pass


def test_kill_process_tree_kills_children():
    from core.backend_launcher import BackendLauncher

    parent = _spawn_tree_parent()
    # 父进程会立即 flush 子进程 pid；读取它作为“引擎子进程”的代表
    child_pid = int(parent.stdout.readline().strip())
    assert _is_alive(child_pid), "子进程应已随父进程启动"

    try:
        # 模拟托盘退出：kill_hard 内部调用的进程树清理
        BackendLauncher._kill_process_tree(parent)
        # 给 OS 一点时间实际回收进程
        deadline = time.time() + 8
        while time.time() < deadline:
            if parent.poll() is not None and not _is_alive(child_pid):
                break
            time.sleep(0.2)
        assert parent.poll() is not None, "父进程应被终止"
        assert not _is_alive(child_pid), \
            "子进程（模拟 llama-server.exe 引擎子进程）应一并被终止，不能残留成孤儿"
    finally:
        _force_cleanup(parent, child_pid)
