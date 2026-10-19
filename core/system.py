# -*- coding: utf-8 -*-
"""
系统资源检测（detect_system）与并发模型数估算（estimate_max_models）。

通过 psutil 获取 CPU / 内存信息；通过 subprocess 调用 nvidia-smi 探测 GPU。
检测结果为 dict（SystemInfo），并带进程内缓存避免重复探测开销。
"""

import os
import math
import platform
import threading
import subprocess
from typing import Dict, Any, Optional, Tuple

import psutil

# 系统保留内存（GB）：留给操作系统与其它进程
OS_RESERVE_GB: float = 4.0
# 单模型默认预估内存（GB）：在无 GGUF 实际大小时使用
_DEFAULT_MODEL_EST_GB: float = 6.0

_system_cache: Optional[Dict[str, Any]] = None
_cache_lock = threading.Lock()


def _detect_gpu() -> Tuple[bool, str, float, float]:
    """
    探测 NVIDIA GPU。
    返回 (是否可用, 名称, 总显存GB, 空闲显存GB)；不可用则全部返回假值。
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=8,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
            parts = [p.strip() for p in lines[0].split(",")]
            name = parts[0]
            total = float(parts[1]) / 1024.0
            used = float(parts[2]) / 1024.0
            free = max(0.0, total - used)
            return True, name, total, free
    except Exception:
        pass
    return False, "", 0.0, 0.0


def detect_system() -> Dict[str, Any]:
    """
    检测本机资源，返回 SystemInfo dict。
    字段：cpu_count, cpu_threads, total_ram_gb, free_ram_gb, gpu_available,
          gpu_name, total_vram_gb, free_vram_gb, os_reserve_gb, suggested_max_models
    """
    global _system_cache
    vm = psutil.virtual_memory()
    cpu_count = psutil.cpu_count(logical=False) or os.cpu_count() or 1
    cpu_threads = psutil.cpu_count(logical=True) or (cpu_count * 2)

    gpu_avail, gpu_name, total_vram, free_vram = _detect_gpu()

    free_ram = vm.available / (1024 ** 3)
    info: Dict[str, Any] = {
        "cpu_count": int(cpu_count),
        "cpu_threads": int(cpu_threads),
        "total_ram_gb": round(vm.total / (1024 ** 3), 2),
        "free_ram_gb": round(free_ram, 2),
        "gpu_available": gpu_avail,
        "gpu_name": gpu_name,
        "total_vram_gb": round(total_vram, 2),
        "free_vram_gb": round(free_vram, 2),
        "os_reserve_gb": OS_RESERVE_GB,
        "suggested_max_models": 1,
    }
    # 依据空闲内存给出并发模型建议数（默认按 6GB 单模型估算）
    info["suggested_max_models"] = estimate_max_models(free_ram_gb=info["free_ram_gb"])

    with _cache_lock:
        _system_cache = info
    return info


def estimate_max_models(model_size_gb: float = _DEFAULT_MODEL_EST_GB,
                        free_ram_gb: Optional[float] = None) -> int:
    """
    估算可同时运行的模型数。
    口径：可用内存 = 空闲内存 - 系统保留；单模型预估 = gguf大小 * 1.2；
          至少运行 1 个。
    """
    if free_ram_gb is None:
        free_ram_gb = psutil.virtual_memory().available / (1024 ** 3)
    est = model_size_gb * 1.2
    free = free_ram_gb - OS_RESERVE_GB
    if free <= 0 or est <= 0:
        return 1
    return max(1, int(math.floor(free / est)))


def get_cached_system() -> Dict[str, Any]:
    """返回进程内缓存的系统信息（若未探测则触发一次）。"""
    with _cache_lock:
        if _system_cache is not None:
            return _system_cache
    return detect_system()
