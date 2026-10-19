# -*- coding: utf-8 -*-
"""
本地大模型服务 —— 核心包（core）

包含：
    - pool.py     多模型并发引擎池（ModelPool）
    - system.py   系统资源检测
    - config.py   模型注册表加载/合并/持久化 + API Key 生成
    - catalog.py  GitHub/HuggingFace curated 模型目录服务
    - download.py GGUF 流式下载服务
"""
