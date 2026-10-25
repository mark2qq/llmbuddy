# -*- coding: utf-8 -*-
"""
目录本地扫描测试（test_catalog_scan.py）。

覆盖 CatalogService._merge_local() 的行为：
  1) 清单内模型按本机是否存在 gguf 标记 local；
  2) MODELS_DIR 中不在清单内的 .gguf（手动拷入/导入）自动补充为
     local=True、source="local" 的条目；
  3) 已在清单内的 gguf 不被重复追加；
  4) 非 .gguf 文件不被误识别为模型；
  5) 文件名带量化后缀（-q4_k_m）也能认成清单里的同一个模型；
  6) 命中后补 registry 别名，保证列表里的模型名能直接加载；
  7) 当前模型目录优先于 registry 里指向旧目录的下载记录。
"""
import json
import os

import pytest

import core.catalog as catalog_mod
from core.catalog import CatalogService, BUILTIN_PATH, gguf_key


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch):
    """隔离真实 %LOCALAPPDATA%/cache/registry.json。

    否则本机已下载的模型会让「目录为空应全是 local=False」这类断言失效，
    补别名时还会污染开发机的真实注册表。
    """
    monkeypatch.setattr(catalog_mod, "load_registry", lambda: {})
    monkeypatch.setattr(catalog_mod, "_read_registry_file", lambda: {})
    monkeypatch.setattr(catalog_mod, "register_model",
                        lambda *a, **kw: {"gguf": a[1] if len(a) > 1 else ""})


def _builtin_first() -> dict:
    """读取内置清单第一条（用于构造「清单内模型」的 gguf 文件名）。"""
    with open(BUILTIN_PATH, "r", encoding="utf-8") as f:
        builtin = json.load(f)
    return builtin[0]


def _items_by_name(items):
    out = {}
    for it in items:
        out.setdefault(it.get("name"), []).append(it)
    return out


def test_merge_local_scans_manual_gguf(tmp_path, monkeypatch):
    """清单内 gguf 标记 local=True；额外 gguf 补充为 source=local。"""
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

    names = _items_by_name(items)
    assert names.get(first["name"]), "清单内模型应出现在目录中"
    assert names[first["name"]][0]["local"] is True

    manual = names.get("mymodel") or []
    assert len(manual) == 1, "mymodel 应且仅应出现一次"
    it = manual[0]
    assert it["local"] is True
    assert it["source"] == "local"
    assert it["gguf"] == "mymodel.gguf"
    assert it["repo"] == "本地模型目录"
    assert it["size_gb"] >= 0


def test_merge_local_no_dupe_for_listed(tmp_path, monkeypatch):
    """已在清单内且已下载的 gguf，不应再被重复追加一条 local 记录。"""
    models = tmp_path / "models"
    models.mkdir()
    first = _builtin_first()
    (models / f"{first['name']}.gguf").write_bytes(b"fake")

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    svc = CatalogService(cache_path=str(tmp_path / "cache_dup.json"))
    items, _cached = svc.fetch(force=True)

    same = [it for it in items if it.get("name") == first["name"]]
    assert len(same) == 1, f"清单内模型被重复追加: {same}"


def test_quantized_filename_matches_listed_model(tmp_path, monkeypatch):
    """磁盘上是 -q4_k_m 后缀、清单里是裸名，也要认成同一个（红字缺陷根因）。

    修复前：清理器只按字面拼 MODELS_DIR/<gguf>，名字对不上 → 已下载的模型
    显示成「未下载」，点下载又下一份；同时列表末尾多出一条同名不同写法的
    「本地导入」重复条目。
    """
    models = tmp_path / "models"
    models.mkdir()
    first = _builtin_first()
    real = f"{first['name']}-q4_k_m.gguf"
    (models / real).write_bytes(b"fake-quant")

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    svc = CatalogService(cache_path=str(tmp_path / "cache_quant.json"))
    items, _cached = svc.fetch(force=True)

    assert len(items) == len([i for i in items if i.get("name") != real[:-5]]), \
        "带量化后缀的文件不应再追加成独立条目"
    hit = [it for it in items if it.get("name") == first["name"]]
    assert len(hit) == 1
    assert hit[0]["local"] is True
    # 真实文件名要写回条目，加载接口才拼得对路径
    assert hit[0]["gguf"] == real
    assert hit[0]["gguf"] != f"{first['name']}.gguf"


def test_alias_registered_for_renamed_file(tmp_path, monkeypatch):
    """命中宽松匹配后要补 registry 别名，否则按条目名加载会报 1001。"""
    models = tmp_path / "models"
    models.mkdir()
    first = _builtin_first()
    real = f"{first['name']}-q4_k_m.gguf"
    fpath = models / real
    fpath.write_bytes(b"x")

    recorded = {}

    def _fake_register(name, gguf_path, meta=None):
        recorded[name] = gguf_path
        return {"gguf": gguf_path}

    monkeypatch.setattr(catalog_mod, "register_model", _fake_register)
    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    svc = CatalogService(cache_path=str(tmp_path / "cache_alias.json"))
    svc.fetch(force=True)

    assert first["name"] in recorded, f"应补别名，实际 {recorded}"
    assert os.path.normcase(recorded[first["name"]]) == os.path.normcase(str(fpath))


def test_settings_dir_wins_over_stale_registry(tmp_path, monkeypatch):
    """registry 里记的是旧目录的文件时，当前目录里的同名文件优先。"""
    old_dir = tmp_path / "old_models"
    new_dir = tmp_path / "new_models"
    old_dir.mkdir()
    new_dir.mkdir()
    first = _builtin_first()
    real = f"{first['name']}.gguf"
    (old_dir / real).write_bytes(b"old")
    (new_dir / real).write_bytes(b"new")

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(new_dir))
    monkeypatch.setattr(catalog_mod, "load_registry",
                        lambda: {first["name"]: {"gguf": str(old_dir / real)}})
    svc = CatalogService(cache_path=str(tmp_path / "cache_win.json"))
    items, _cached = svc.fetch(force=True)

    hit = [it for it in items if it.get("name") == first["name"]][0]
    assert hit["local"] is True
    assert hit["gguf"] == real
    assert str(new_dir) in catalog_mod.os.path.join(str(new_dir), hit["gguf"])


def test_registry_outside_models_dir_is_ignored(tmp_path, monkeypatch):
    """registry 里指向「别的目录」的旧下载记录不算已下载。

    用户把模型目录从 A 换到 B 后，A 里的文件照样 os.path.exists；
    若认这些记录，模型库会把当前目录根本没有的模型显示成已下载。
    """
    other_dir = tmp_path / "another_dir"
    models = tmp_path / "models"
    other_dir.mkdir()
    models.mkdir()
    first = _builtin_first()
    real = f"{first['name']}.gguf"
    (other_dir / real).write_bytes(b"elsewhere")     # 只在别的目录有

    monkeypatch.setattr(catalog_mod, "MODELS_DIR", str(models))
    monkeypatch.setattr(catalog_mod, "load_registry",
                        lambda: {first["name"]: {"gguf": str(other_dir / real)}})
    svc = CatalogService(cache_path=str(tmp_path / "cache_out.json"))
    items, _cached = svc.fetch(force=True)

    hit = [it for it in items if it.get("name") == first["name"]][0]
    assert hit["local"] is False, "不在当前模型目录里的文件不该算已下载"


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


@pytest.mark.parametrize("raw,expect", [
    ("qwen2.5-coder-7b-instruct-q4_k_m.gguf", "qwen25coder7binstruct"),
    ("Qwen2.5-Coder-7B-Instruct.gguf", "qwen25coder7binstruct"),
    ("Llama-3.2-3B-Instruct-Q4_K_M.gguf", "llama323binstruct"),
    ("Qwen3-4B-Q8_0.gguf", "qwen34b"),
    ("model-f16.gguf", "model"),
])
def test_gguf_key_normalizes_quant(raw, expect):
    """量化后缀 / 大小写 / 分隔符都要归一到同一个键。"""
    assert gguf_key(raw) == expect


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
