# -*- coding: utf-8 -*-
"""
目录本地扫描测试（test_catalog_scan.py）。

覆盖 CatalogService._merge_local() 的新行为：
  1) 清单内模型按本机是否存在 gguf 标记 local；
  2) MODELS_DIR 中不在清单内的 .gguf（手动下载/导入）自动补充为
     local=True、source="manual" 的本地模型（支撑客户端「手动添加」直链下载）；
  3) 已在清单内的 gguf 不被重复追加；
  4) 非 .gguf 文件不被误识别为模型。
"""
import json
import os

import pytest

import core.catalog as catalog_mod
from core.catalog import CatalogService, BUILTIN_PATH


def _builtin_first() -> dict:
    """读取内置清单第一条（用于构造「清单内模型」的 gguf 文件名）。"""
    with open(BUILTIN_PATH, "r", encoding="utf-8") as f:
        builtin = json.load(f)
    return builtin[0]


def test_merge_local_scans_manual_gguf(tmp_path, monkeypatch):
    """清单内 gguf 标记 local=True；额外 gguf 补充为 source=manual。"""
    models = tmp_path / "models"
    models.mkdir()

    # 1) 清单内模型：builtin 无 gguf 字段，_merge_local 回退为 f"{name}.gguf"
    first = _builtin_first()
    (models / f"{first['name']}.gguf").write_bytes(b"fake-listed")

    # 2) 手动导入的额外 gguf
    (models / "mymodel.gguf").write_bytes(b"fake-manual" * 1000)

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    svc = CatalogService(cache_path=str(tmp_path / "cache.json"))
    items, _cached = svc.fetch(force=True)

    listed = [it for it in items if it.get("name") == first["name"]]
    assert listed, "清单内模型应出现在目录中"
    assert listed[0]["local"] is True

    manual = [it for it in items if it.get("name") == "mymodel"]
    assert len(manual) == 1, "mymodel 应且仅应出现一次"
    it = manual[0]
    assert it["local"] is True
    assert it["source"] == "manual"
    assert it["gguf"] == "mymodel.gguf"
    assert it["repo"] == "本地导入"
    assert it["size_gb"] >= 0


def test_merge_local_no_dupe_for_listed(tmp_path, monkeypatch):
    """已在清单内且已下载的 gguf，不应再被重复追加一条 manual 记录。"""
    models = tmp_path / "models"
    models.mkdir()
    first = _builtin_first()
    (models / f"{first['name']}.gguf").write_bytes(b"fake")

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    svc = CatalogService(cache_path=str(tmp_path / "cache_dup.json"))
    items, _cached = svc.fetch(force=True)

    same = [it for it in items if it.get("name") == first["name"]]
    assert len(same) == 1, f"清单内模型被重复追加: {same}"


def test_merge_local_ignores_non_gguf(tmp_path, monkeypatch):
    """非 .gguf 文件不应被当作模型扫描进来。"""
    models = tmp_path / "models"
    models.mkdir()
    (models / "readme.txt").write_bytes(b"x")
    (models / "notes.md").write_bytes(b"y")
    (models / "mymodel.gguf").write_bytes(b"z")

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    svc = CatalogService(cache_path=str(tmp_path / "cache_non.txt.json"))
    items, _cached = svc.fetch(force=True)

    names = {it.get("name") for it in items}
    assert "mymodel" in names
    assert "readme" not in names
    assert "notes" not in names


def test_merge_local_missing_dir_is_safe(tmp_path, monkeypatch):
    """MODELS_DIR 不存在时不应抛异常，且仅返回清单内容。"""
    missing = tmp_path / "nonexistent"
    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(missing))
    svc = CatalogService(cache_path=str(tmp_path / "cache_missing.json"))
    items, _cached = svc.fetch(force=True)

    assert not os.path.isdir(str(missing))
    assert all(it.get("local") is False for it in items)


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
