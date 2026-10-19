# -*- coding: utf-8 -*-
"""
配置自愈测试（第十五轮）。

背景：安装目录改名/迁移后（LLMDesktop -> llmBuddy），config.json 里保存的
engine_exe / python_exe 仍指向旧路径，导致引擎进程起不来、加载模型报
1003 engine error（引擎日志 0 字节）。Config.load() 现在会自动回退到当前
安装目录解析出的默认值。
"""
import json
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_DIR = os.path.dirname(_CLIENT_DIR)
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)
while _REPO_DIR in sys.path:
    sys.path.remove(_REPO_DIR)


def test_config_heals_stale_paths(tmp_path):
    """指向不存在路径的配置应被回退为默认值，并回写文件。"""
    from app_config import Config, DEFAULT_ENGINE_EXE, DEFAULT_PYTHON_EXE

    target = tmp_path / "config.json"
    target.write_text(json.dumps({
        "engine_exe": r"Z:\gone\llama-server.exe",
        "python_exe": r"Z:\gone\python.exe",
        "n_ctx": 4096,
    }), encoding="utf-8")

    cfg = Config.load(str(target))
    assert cfg.engine_exe == DEFAULT_ENGINE_EXE, "失效的引擎路径应自愈"
    assert cfg.python_exe == DEFAULT_PYTHON_EXE, "失效的解释器路径应自愈"
    assert cfg.n_ctx == 4096, "其它用户设置应保持不变"

    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["engine_exe"] == DEFAULT_ENGINE_EXE, "自愈结果应回写文件"


def test_config_keeps_valid_paths(tmp_path):
    """路径有效时不得覆盖用户配置。"""
    from app_config import Config

    target = tmp_path / "config.json"
    target.write_text(json.dumps({
        "engine_exe": sys.executable,     # 一定存在的路径
        "python_exe": sys.executable,
    }), encoding="utf-8")

    cfg = Config.load(str(target))
    assert cfg.engine_exe == sys.executable
    assert cfg.python_exe == sys.executable
