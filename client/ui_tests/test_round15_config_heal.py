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


def test_user_data_dir_migrates_legacy(tmp_path, monkeypatch):
    """数据目录改名（LLMDesktop -> llmBuddy）：旧目录应被整体迁移，内容不丢。"""
    from app_config import _user_data_dir, DATA_DIRNAME, LEGACY_DATA_DIRNAME

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / LEGACY_DATA_DIRNAME
    old.mkdir()
    (old / "config.json").write_text("{}", encoding="utf-8")
    (old / "models").mkdir()

    d = _user_data_dir()
    assert d == str(tmp_path / DATA_DIRNAME)
    # 旧目录已整体改名到新目录（模型/配置原样保留）
    assert (tmp_path / DATA_DIRNAME / "config.json").exists()
    assert (tmp_path / DATA_DIRNAME / "models").is_dir()
    assert not old.exists()
    # 再次调用（新目录已存在）保持稳定
    assert _user_data_dir() == d


def test_user_data_dir_falls_back_when_rename_fails(tmp_path, monkeypatch):
    """迁移失败（目录被占用等）时沿用旧目录，保证数据不丢。"""
    import app_config
    from app_config import _user_data_dir, LEGACY_DATA_DIRNAME

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / LEGACY_DATA_DIRNAME
    old.mkdir()
    (old / "config.json").write_text("{}", encoding="utf-8")

    def _boom(src, dst):
        raise OSError("directory in use")

    monkeypatch.setattr(app_config.os, "rename", _boom)
    assert _user_data_dir() == str(old)
    assert (old / "config.json").exists()


def test_config_heals_models_dir_after_rename(tmp_path, monkeypatch):
    """数据目录迁移后，旧配置里的 models_dir 旧路径应自愈到新目录。"""
    from app_config import Config, DATA_DIRNAME, LEGACY_DATA_DIRNAME

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    new_models = tmp_path / DATA_DIRNAME / "models"
    new_models.mkdir(parents=True)
    target = tmp_path / "config.json"
    target.write_text(json.dumps({
        "models_dir": str(tmp_path / LEGACY_DATA_DIRNAME / "models"),
    }), encoding="utf-8")

    cfg = Config.load(str(target))
    assert cfg.models_dir == str(new_models), "旧 models_dir 应自愈到迁移后的新目录"
