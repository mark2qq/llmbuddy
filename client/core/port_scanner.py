# -*- coding: utf-8 -*-
"""
端口扫描（port_scanner.py）。

利用 psutil.net_connections 获取已占用 TCP 端口，并在指定区间内
分配若干空闲端口（用于后端引擎基端口 / 网关端口推荐）。
"""
import socket
from typing import List, Optional, Set, Tuple

import psutil


def scan_used_ports(start: int, count: int) -> List[int]:
    """
    返回区间 [start, start + count) 内已被占用的端口列表。

    通过 psutil 枚举本机 TCP 连接，取本地监听端口集合。
    """
    used: List[int] = []
    try:
        conns = psutil.net_connections(kind="tcp")
    except Exception:
        conns = []
    busy: Set[int] = {c.laddr.port for c in conns if c.laddr is not None}
    for p in range(start, start + count):
        if p in busy:
            used.append(p)
    return used


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    """用临时 socket 绑定测试端口是否空闲（仅 loopback）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.2)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def find_free_ports(start: int, count: int,
                    skip: Optional[Set[int]] = None) -> Tuple[List[int], List[int]]:
    """
    在区间 [start, start + count) 内寻找空闲端口。

    返回 (空闲端口列表, 已占用端口列表)。
    skip 集合中的端口视为已占用（例如希望避开某些固定端口）。
    """
    skip = skip or set()
    free: List[int] = []
    used: List[int] = []
    for p in range(start, start + count):
        if p in skip:
            used.append(p)
            continue
        if is_port_free(p):
            free.append(p)
        else:
            used.append(p)
    return free, used


def suggest_port(start: int, count: int = 8,
                 skip: Optional[Set[int]] = None) -> Tuple[int, List[int]]:
    """
    推荐一个空闲的起始端口（与 find_free_ports 类似，但返回首个空闲端口）。

    返回 (首个空闲端口, 已占用端口列表)。若区间内全占用，返回 (start, used)。
    """
    free, used = find_free_ports(start, count, skip)
    return (free[0] if free else start), used
