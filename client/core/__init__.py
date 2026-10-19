# -*- coding: utf-8 -*-
"""
客户端核心包（core）。

包含：
    - signals.py        自定义 pyqtSignal 载体
    - task_runner.py     QThread Worker 基类
    - port_scanner.py    端口占用扫描与空闲端口分配
    - api_client.py      REST + SSE 封装
    - backend_launcher.py 后端子进程看护器
"""
