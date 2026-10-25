# -*- coding: utf-8 -*-
"""
在线目录层（catalog / profiles）与配置字段回归（1.0.8 增量 T01）。

覆盖：
    - `_default_config()` 新增 5 个 key，落盘可回读（防 D7 静默失效）
    - 代理来源优先级（cfg.catalog.proxy > env > PROXY_DEFAULT），且
      `_client` 是 `trust_env=False`（防 R-E4「代理未生效 -> 全 2020」）
    - 代理探测失败 -> `CatalogError(2020)` 带代理值，不静默直连
    - 缓存原子读写、TTL 判定、损坏 JSON 不抛
    - 离线退化：缓存过期 + 拉取失败 -> 返回过期缓存并标 stale=True
    - Registry 条目归一化（stdio / http）、免费分级、必填 env、id 冲突
    - GitHub tree -> 技能列表（has_scripts / 文本类附件过滤）
    - 技能包下载上限（文件数 / 总字节 / SKILL.md 超大）
    - URL 归一（raw / blob / tree / Gitee / 非法）
    - profile 匹配（命中数优先、平局取 priority、技能缺失走 L3）

**零外网**：所有出网函数一律 monkeypatch，另有 1 个默认跳过的联网冒烟用例。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import catalog as catalog_mod
from core.assistant import profiles as profiles_mod
from core.assistant import store as store_mod


# ----------------------------------------------------------------------------
# 夹具：助理数据目录整体指到 tmp_path
# ----------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    for sub in ("skills", "sessions", "audit", "workspace", "catalog"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "ASSISTANT_DIR", str(root))
    monkeypatch.setattr(store_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(store_mod, "SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(store_mod, "AUDIT_DIR", str(root / "audit"))
    monkeypatch.setattr(store_mod, "CONFIG_PATH", str(root / "config.json"))
    monkeypatch.setattr(store_mod, "DEFAULT_WORKSPACE", str(root / "workspace"))
    monkeypatch.setattr(store_mod, "CATALOG_DIR", str(root / "catalog"))
    from core.assistant import skills as skills_mod
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(root / "skills"))
    store_mod.ensure_dirs()
    skills_mod.ensure_builtin_skills()
    return {"root": root, "workspace": root / "workspace",
            "catalog": root / "catalog"}


@pytest.fixture()
def cfg(env):
    """固定代理的配置（避免测试机环境变量把断言带跑偏）。"""
    return {"catalog": {"proxy": "http://127.0.0.1:7897",
                        "ttl_hours": {"mcp": 24, "gh_repos": 24, "gh_skills": 168},
                        "allow_nonfree": False}}


class _FakeResp:
    """httpx.Response 的最小替身：只被用 status_code / headers / json() / text。"""

    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# ----------------------------------------------------------------------------
# T01-1 配置字段
# ----------------------------------------------------------------------------
def test_default_config_has_new_keys(env):
    """5 个新字段必须在 _default_config() 里，否则 load_config 读不到（D7）。"""
    cfg = store_mod.load_config()
    for key in ("tool_prefs", "auto_load", "profiles", "catalog", "skill_sources"):
        assert key in cfg, key
    assert cfg["tool_prefs"]["max_tools"] == store_mod.DEFAULT_MAX_TOOLS
    assert cfg["tool_prefs"]["disabled"] == []
    assert cfg["auto_load"] is True
    assert isinstance(cfg["profiles"], list) and len(cfg["profiles"]) == 3
    assert {p["id"] for p in cfg["profiles"]} == {"file-organize", "code-read",
                                                  "web-research"}
    assert cfg["catalog"]["ttl_hours"]["gh_skills"] == 168
    assert cfg["skill_sources"] == {}
    # 落盘后 json 文件里也必须有（save_config 不能把新字段吞掉）
    store_mod.save_config({})
    with open(store_mod.CONFIG_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)
    for key in ("tool_prefs", "auto_load", "profiles", "catalog", "skill_sources"):
        assert key in raw, key


def test_catalog_dir_created(env):
    """CATALOG_DIR 必须进 ensure_dirs()，否则首屏读缓存会打不开目录。"""
    import shutil
    shutil.rmtree(str(env["catalog"]), ignore_errors=True)
    store_mod.ensure_dirs()
    assert os.path.isdir(store_mod.CATALOG_DIR)
    assert catalog_mod.catalog_dir() == store_mod.CATALOG_DIR


# ----------------------------------------------------------------------------
# T01-2 代理
# ----------------------------------------------------------------------------
def test_proxy_prefers_cfg_over_env(monkeypatch, cfg):
    monkeypatch.setenv("HTTP_PROXY", "http://evil:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://evil:2")
    assert catalog_mod._proxy(cfg) == "http://127.0.0.1:7897"


def test_proxy_falls_back_to_env_then_default(monkeypatch):
    for key in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
    assert catalog_mod._proxy({"catalog": {"proxy": ""}}) == catalog_mod.PROXY_DEFAULT
    monkeypatch.setenv("HTTPS_PROXY", "http://env-proxy:8899")
    assert catalog_mod._proxy({"catalog": {"proxy": ""}}) == "http://env-proxy:8899"
    # cfg 为空（None）也不能炸
    assert catalog_mod._proxy(None) == "http://env-proxy:8899"


def test_catalog_client_trust_env_false(monkeypatch, cfg):
    """`_client` 必须 trust_env=False 且带显式 proxy（R-E4）。

    断言构造入参而不是内部 `_pool` 属性：httpx 0.28 的代理对象是延迟创建的
    （`_pool._proxy` 初始为 None，首次请求才填），读内部字段既不稳定也和
    「我们显式传了 proxy」这一事实无关。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://evil:1")
    captured = {}
    real_client = catalog_mod.httpx.Client

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(catalog_mod.httpx, "Client", spy)
    with catalog_mod._client(cfg) as c:
        assert c.trust_env is False
    assert captured.get("trust_env") is False
    assert captured.get("proxy") == "http://127.0.0.1:7897"   # 用 cfg，不吃 env


def test_probe_failure_raises_2020(cfg, monkeypatch):
    """探测代理时实际发请求失败 -> CatalogError(2020)，文案带代理值。"""
    def boom(*_args, **_kw):
        raise RuntimeError("All connection attempts failed")

    monkeypatch.setattr(catalog_mod.httpx, "Client", boom)
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod._probe(cfg)
    assert ei.value.code == catalog_mod.ERR_UNREACHABLE == 2020
    assert "127.0.0.1:7897" in ei.value.message
    assert "无法连接在线目录" in ei.value.message


def test_probe_ok_on_200(cfg, monkeypatch):
    class _FakeClient:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_e):
            return False

        def get(self, *_a, **_kw):
            return _FakeResp(200, {"servers": []})

    monkeypatch.setattr(catalog_mod.httpx, "Client", _FakeClient)
    catalog_mod._probe(cfg)      # 不抛即通过


# ----------------------------------------------------------------------------
# T01-3 缓存 TTL / 损坏容错
# ----------------------------------------------------------------------------
def test_cache_ttl(env, monkeypatch):
    catalog_mod._cache_write("t.json", {"items": [1, 2]})
    cache = catalog_mod._cache_read("t.json")
    assert cache["items"] == [1, 2] and cache["fetched_at"] > 0
    assert catalog_mod._fresh(cache, 3600) is True

    cache["fetched_at"] = 1.0                # 1970 年，必然过期
    assert catalog_mod._fresh(cache, 3600) is False
    assert catalog_mod._fresh({}, 3600) is False
    assert catalog_mod._fresh({"fetched_at": "abc"}, 3600) is False


def test_cache_read_corrupt_returns_empty(env):
    path = os.path.join(store_mod.CATALOG_DIR, "broken.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ not json at all")
    assert catalog_mod._cache_read("broken.json") == {}
    assert catalog_mod._cache_read("never-existed.json") == {}
    # 顶层不是 dict 也按空处理
    with open(path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    assert catalog_mod._cache_read("broken.json") == {}


# ----------------------------------------------------------------------------
# T01-4 MCP Registry 归一化
# ----------------------------------------------------------------------------
def _npm_raw():
    return {
        "name": "com.pulsemcp/remote-filesystem",
        "title": "Remote Filesystem",
        "description": "读写 GCS 上的文件",
        "version": "0.1.2",
        "packages": [{
            "registryType": "npm",
            "runtimeHint": "npx",
            "identifier": "remote-filesystem-mcp-server",
            "version": "0.1.2",
            "runtimeArguments": [{"value": "-y"}],
            "environmentVariables": [
                {"name": "GCS_BUCKET", "description": "桶名", "isRequired": True},
                {"name": "LOG_LEVEL", "description": "日志级别"},
            ],
        }],
    }


def _http_raw():
    return {
        "name": "com.example/remote-http",
        "title": "Remote HTTP",
        "description": "远程 MCP",
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http",
                     "url": "https://mcp.example.com/v1"}],
    }


def _http_required_header_raw():
    """远程 streamable-http，但 header 有必填项 -> need_register（PRD §5.3）。"""
    return {
        "name": "com.example/remote-auth",
        "title": "Remote Auth",
        "description": "需要授权 token 的远程 MCP",
        "version": "1.0.0",
        "remotes": [{"type": "streamable-http",
                     "url": "https://api.example.com/mcp",
                     "headers": [{"name": "Authorization",
                                  "description": "Bearer token",
                                  "isRequired": True}]}],
    }


def test_normalize_mcp_server_stdio():
    item = catalog_mod.normalize_mcp_server(_npm_raw())
    assert item["transport"] == "stdio"
    assert item["suggest"]["command"] == "npx"
    assert item["suggest"]["args"] == ["-y", "remote-filesystem-mcp-server@0.1.2"]
    assert item["free_class"] == "need_key"
    req = [e["name"] for e in item["suggest"]["required_env"]]
    assert req == ["GCS_BUCKET"]
    optional = [e["name"] for e in item["suggest"]["optional_env"]]
    assert optional == ["LOG_LEVEL"]
    assert item["registry_name"] == "com.pulsemcp/remote-filesystem"


def test_normalize_mcp_server_http():
    item = catalog_mod.normalize_mcp_server(_http_raw())
    assert item["transport"] == "http"
    assert item["suggest"]["url"] == "https://mcp.example.com/v1"
    assert item["suggest"]["command"] == ""


def test_normalize_npm_without_required_env_is_zero_config():
    raw = _npm_raw()
    raw["packages"][0]["environmentVariables"] = [
        {"name": "LOG_LEVEL", "description": "日志级别"}]
    item = catalog_mod.normalize_mcp_server(raw)
    assert item["free_class"] == "zero_config"
    assert item["suggest"]["required_env"] == []


def test_free_class():
    assert catalog_mod.free_class_of(_npm_raw()) == "need_key"
    raw = _npm_raw()
    raw["packages"][0]["environmentVariables"] = []
    assert catalog_mod.free_class_of(raw) == "zero_config"
    # P2-2：无必填 header 的 streamable-http 是「零配置、一键可连」（PRD §5.3）
    assert catalog_mod.free_class_of(_http_raw()) == "zero_config"
    # 只有 header 有必填项才判 need_register
    assert catalog_mod.free_class_of(_http_required_header_raw()) == "need_register"
    assert catalog_mod.free_class_of({"name": "empty"}) == "unknown"


def test_zero_config_streamable_http_not_hidden():
    """P2-2：零配置远程 MCP 不应落入客户端默认隐藏的分类。

    客户端 `CATALOG_HIDDEN_CLASSES` 含 need_register；若把无 headers 的
    streamable-http 判成 need_register，用户「抓全部免费 MCP」会漏掉这类
    一键可连的服务器。断言其 free_class 不在隐藏集合内。
    """
    hidden = ("need_key", "need_register")     # 与 assistant_page 同口径
    fc = catalog_mod.free_class_of(_http_raw())
    assert fc == "zero_config"
    assert fc not in hidden


def test_zero_config_http_installable_via_build_config():
    """P2-2：zero_config 远程条目能经 build_mcp_config 落成 http 配置（可装）。"""
    item = catalog_mod.normalize_mcp_server(_http_raw())
    assert item["free_class"] == "zero_config"
    conf = catalog_mod.build_mcp_config(item, existing_ids=[])
    assert conf["transport"] == "http"
    assert conf["url"] == "https://mcp.example.com/v1"
    assert conf["command"] == ""              # http 分支不写 command（不是 stdio）


def test_build_mcp_config_stdio_and_env_required():
    item = catalog_mod.normalize_mcp_server(_npm_raw())
    # 缺必填 env -> 2024，且 data.missing 列出缺失项
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.build_mcp_config(item, existing_ids=[])
    assert ei.value.code == 2024
    assert ei.value.data["missing"] == ["GCS_BUCKET"]

    conf = catalog_mod.build_mcp_config(item, env={"GCS_BUCKET": "my-bucket"},
                                       existing_ids=[])
    assert conf["id"] == "com.pulsemcp-remote-filesystem"
    assert conf["transport"] == "stdio" and conf["command"] == "npx"
    assert conf["args"] == ["-y", "remote-filesystem-mcp-server@0.1.2"]
    assert conf["env"] == {"GCS_BUCKET": "my-bucket"}
    assert conf["risk"] == "high" and conf["enabled"] is True


def test_build_mcp_config_id_conflict_appends_suffix():
    item = catalog_mod.normalize_mcp_server(_http_raw())
    conf = catalog_mod.build_mcp_config(
        item, existing_ids=["com.example-remote-http", "com.example-remote-http-2"])
    assert conf["id"] == "com.example-remote-http-3"
    assert conf["transport"] == "http"
    assert conf["url"] == "https://mcp.example.com/v1"


def test_build_mcp_config_rejects_bad_command():
    item = {"registry_name": "x", "suggest": {"transport": "stdio",
                                             "command": "curl", "args": ["http://x"]}}
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.build_mcp_config(item, existing_ids=[])
    assert ei.value.code == 2022


def test_list_mcp_uses_cache_and_stale_fallback(env, cfg, monkeypatch):
    """拉取成功写缓存；再次 force 拉取失败 -> 返回过期缓存 + stale=True。"""
    calls = {"n": 0}

    def fake_get_json(_cfg, url, params=None, timeout=5.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return ({"servers": [{"server": _npm_raw()}],
                     "metadata": {"nextCursor": "cur-2"}}, {})
        raise catalog_mod.CatalogError(2020, "模拟断网")

    monkeypatch.setattr(catalog_mod, "_get_json", fake_get_json)
    first = catalog_mod.list_mcp(cfg, force=True)
    assert first["stale"] is False and len(first["items"]) == 1
    assert first["cursor"] == "cur-2"

    # 缓存新鲜：force=False 直接返缓存，不再出网
    before = calls["n"]
    cached = catalog_mod.list_mcp(cfg, force=False)
    assert calls["n"] == before and cached["stale"] is False
    assert len(cached["items"]) == 1

    # 拉到失败：过期缓存兜底（这里用 fetch 失败，缓存刚写故 fresh，但 force 强制重拉）
    stale = catalog_mod.list_mcp(cfg, force=True)
    assert stale["stale"] is True
    assert len(stale["items"]) == 1          # 绝不返回空页
    assert "模拟断网" in stale["error"]


def test_list_mcp_raises_when_no_cache(env, cfg, monkeypatch):
    def fake_get_json(*_a, **_kw):
        raise catalog_mod.CatalogError(2020, "模拟断网")

    monkeypatch.setattr(catalog_mod, "_get_json", fake_get_json)
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.list_mcp(cfg, force=True)
    assert ei.value.code == 2020


def test_list_repos_stale_fallback(env, cfg, monkeypatch):
    state = {"ok": True}

    def fake_get_json(_cfg, url, params=None, timeout=5.0):
        if state["ok"]:
            return ({"total_count": 1, "items": [{
                "full_name": "acme/skills", "name": "skills",
                "owner": {"login": "acme"}, "stargazers_count": 42,
                "default_branch": "main", "description": "技能集",
                "topics": ["claude-skills"]}]}, {})
        raise catalog_mod.CatalogError(2021, "GitHub 接口限流")

    monkeypatch.setattr(catalog_mod, "_get_json", fake_get_json)
    first = catalog_mod.list_repos(cfg, force=True)
    assert first["stale"] is False and first["items"][0]["stars"] == 42
    state["ok"] = False
    stale = catalog_mod.list_repos(cfg, force=True)
    assert stale["stale"] is True and stale["items"][0]["full_name"] == "acme/skills"


# ----------------------------------------------------------------------------
# T01-5 GitHub tree -> 技能列表 / 技能包
# ----------------------------------------------------------------------------
TREE = {
    "sha": "abc123def456",
    "tree": [
        {"path": "README.md", "type": "blob"},
        {"path": "a/b/SKILL.md", "type": "blob"},
        {"path": "a/b/notes.md", "type": "blob"},
        {"path": "a/b/extra.json", "type": "blob"},
        {"path": "a/b/scripts/run.py", "type": "blob"},
        {"path": "a/b/image.png", "type": "blob"},
        {"path": "a", "type": "tree"},
    ],
}

SKILL_MD = """---
name: pdf-extract
description: 从 PDF 提取正文
---
# 要求
1. 抽正文
"""


def _mock_tree_and_raw(monkeypatch, tree=TREE, texts=None):
    texts = texts or {"/a/b/SKILL.md": SKILL_MD}
    seen = {"json": [], "text": []}

    def fake_get_json(_cfg, url, params=None, timeout=5.0):
        seen["json"].append((url, params))
        if "git/trees" in url:
            return tree, {}
        if "/repos/" in url:
            return {"default_branch": "main"}, {}
        return {}, {}

    def fake_get_text(_cfg, url, timeout=5.0):
        seen["text"].append(url)
        for suffix, body in texts.items():
            if url.endswith(suffix):
                return body
        raise catalog_mod.CatalogError(2021, f"下载失败：{url}")

    monkeypatch.setattr(catalog_mod, "_get_json", fake_get_json)
    monkeypatch.setattr(catalog_mod, "_get_text", fake_get_text)
    return seen


def test_list_repo_skills_tree_parse(env, cfg, monkeypatch):
    _mock_tree_and_raw(monkeypatch)
    out = catalog_mod.list_repo_skills(cfg, "acme", "skills", "main", force=True)
    assert out["commit"] == "abc123def456"
    assert len(out["items"]) == 1
    item = out["items"][0]
    assert item["name"] == "pdf-extract"
    assert item["dir"] == "a/b"
    assert item["has_scripts"] is True
    # files 只含技能目录下的文本类附件（png 被过滤，SKILL.md 由 bundle 必取）
    assert item["files"] == ["notes.md", "extra.json", "scripts/run.py"]
    assert item["description"] == "从 PDF 提取正文"


def test_fetch_skill_bundle_limits(env, cfg, monkeypatch):
    """25 个文件 / 单文件超大 -> 只取有限数量，skipped>0。"""
    many = {"sha": "c1", "tree": [{"path": "SKILL.md", "type": "blob"}]
            + [{"path": f"f{i}.md", "type": "blob"} for i in range(25)]}
    texts = {"/SKILL.md": SKILL_MD}
    texts.update({f"/f{i}.md": "x" * 100 for i in range(25)})
    _mock_tree_and_raw(monkeypatch, tree=many, texts=texts)
    bundle = catalog_mod.fetch_skill_bundle(cfg, "acme", "skills", "main", "",
                                           max_files=5)
    assert bundle["slug"] == "pdf-extract"
    assert "SKILL.md" in bundle["files"]
    assert len(bundle["files"]) <= 6          # SKILL.md + max_files 个附件
    assert bundle["skipped"] > 0


def test_fetch_skill_bundle_skips_oversize_total(env, cfg, monkeypatch):
    tree = {"sha": "c2", "tree": [{"path": "SKILL.md", "type": "blob"},
                                 {"path": "big.md", "type": "blob"}]}
    texts = {"/SKILL.md": SKILL_MD, "/big.md": "y" * 5000}
    _mock_tree_and_raw(monkeypatch, tree=tree, texts=texts)
    bundle = catalog_mod.fetch_skill_bundle(cfg, "acme", "skills", "main", "",
                                           max_bytes=1000)
    assert "big.md" not in bundle["files"] and bundle["skipped"] >= 1


def test_fetch_skill_bundle_rejects_huge_skill_md(env, cfg, monkeypatch):
    tree = {"sha": "c3", "tree": [{"path": "SKILL.md", "type": "blob"}]}
    _mock_tree_and_raw(monkeypatch, tree=tree,
                       texts={"/SKILL.md": SKILL_MD + ("z" * (300 * 1024))})
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.fetch_skill_bundle(cfg, "acme", "skills", "main", "")
    assert ei.value.code == 2022


def test_fetch_skill_bundle_rejects_missing_name(env, cfg, monkeypatch):
    tree = {"sha": "c4", "tree": [{"path": "skills/x/SKILL.md", "type": "blob"}]}
    _mock_tree_and_raw(monkeypatch, tree=tree,
                       texts={"/skills/x/SKILL.md": "# 没有 frontmatter\n"})
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.fetch_skill_bundle(cfg, "acme", "skills", "main", "skills/x")
    assert ei.value.code == 2022


def test_fetch_skill_bundle_missing_dir_is_2022(env, cfg, monkeypatch):
    _mock_tree_and_raw(monkeypatch)
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.fetch_skill_bundle(cfg, "acme", "skills", "main", "no/such/dir")
    assert ei.value.code == 2022


# ----------------------------------------------------------------------------
# T01-6 URL 归一
# ----------------------------------------------------------------------------
def test_url_to_raw():
    a = catalog_mod.url_to_raw(
        "https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md")
    assert a["raw_url"] == "https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md"
    assert (a["owner"], a["repo"], a["ref"], a["dir"]) == ("o", "r", "main", "skills/pdf")

    b = catalog_mod.url_to_raw("https://github.com/o/r/blob/main/skills/pdf/SKILL.md")
    assert b["raw_url"] == "https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md"
    assert b["dir"] == "skills/pdf"

    c = catalog_mod.url_to_raw("https://github.com/o/r/tree/main/skills/pdf")
    assert c["dir"] == "skills/pdf"

    d = catalog_mod.url_to_raw("https://gitee.com/o/r/raw/main/skills/pdf/SKILL.md")
    assert d["provider"] == "gitee" and d["dir"] == "skills/pdf"

    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod.url_to_raw("https://example.com/a.md")
    assert ei.value.code == 2026
    with pytest.raises(catalog_mod.CatalogError) as ei2:
        catalog_mod.url_to_raw("")
    assert ei2.value.code == 2026


# ----------------------------------------------------------------------------
# T01-7 profile 匹配
# ----------------------------------------------------------------------------
ALL_SKILLS = ["workspace-organizer", "repo-digest", "web-brief"]


def test_match_profile_file_organize():
    got = profiles_mod.match_profile("帮我把下载目录整理一下", None, ALL_SKILLS)
    assert got is not None and got["id"] == "file-organize"
    assert got["skills"] == ["workspace-organizer"]
    assert got["matched"] == ["整理"]


def test_match_profile_code_read():
    got = profiles_mod.match_profile("分析一下这个仓库的架构", None, ALL_SKILLS)
    assert got is not None and got["id"] == "code-read"


def test_match_profile_multi_hit_takes_more_hits():
    # 「整理」命中 file-organize 1 个；「这个项目」+「代码」命中 code-read 2 个
    got = profiles_mod.match_profile("整理这个项目的代码", None, ALL_SKILLS)
    assert got is not None and got["id"] == "code-read"
    assert len(got["matched"]) == 2


def test_match_profile_tie_breaks_on_priority():
    """命中数相同 -> priority 数值高者胜（file-organize=10 / web-research=30）。"""
    procs = [
        {"id": "a", "name": "A", "enabled": True, "priority": 10,
         "triggers": ["整理"], "skills": ["workspace-organizer"], "tools": []},
        {"id": "b", "name": "B", "enabled": True, "priority": 30,
         "triggers": ["整理"], "skills": ["workspace-organizer"], "tools": []},
    ]
    got = profiles_mod.match_profile("帮我整理一下", procs, ALL_SKILLS)
    assert got is not None and got["id"] == "b"


def test_match_profile_empty_available_returns_none():
    """技能一个都没装 -> None（走 L3 降级，不硬挂一个跑不起来的 profile）。"""
    assert profiles_mod.match_profile("帮我把下载目录整理一下", None, []) is None
    assert profiles_mod.match_profile("帮我把下载目录整理一下", None,
                                      ["some-other-skill"]) is None


def test_match_profile_none_available_skips_skill_check():
    got = profiles_mod.match_profile("帮我把下载目录整理一下", None, None)
    assert got is not None and got["id"] == "file-organize"


def test_match_profile_disabled_and_empty_triggers():
    procs = [{"id": "a", "name": "A", "enabled": False, "priority": 99,
              "triggers": ["整理"], "skills": ["workspace-organizer"], "tools": []},
             {"id": "b", "name": "B", "enabled": True, "priority": 5,
              "triggers": [], "skills": ["workspace-organizer"], "tools": []}]
    assert profiles_mod.match_profile("帮我整理一下", procs, ALL_SKILLS) is None


def test_match_profile_blank_text():
    assert profiles_mod.match_profile("", None, ALL_SKILLS) is None
    assert profiles_mod.match_profile("   ", None, ALL_SKILLS) is None


def test_match_profile_is_case_insensitive():
    got = profiles_mod.match_profile("please REVIEW this", None, ALL_SKILLS)
    assert got is not None and got["id"] == "code-read"


def test_ensure_builtin_profiles_does_not_overwrite(env):
    store_mod.save_config({"profiles": [{"id": "file-organize", "name": "我改的名字",
                                        "enabled": True, "priority": 99,
                                        "triggers": ["自定义"], "skills": [],
                                        "tools": []}]})
    profiles_mod.ensure_builtin_profiles()
    cur = {p["id"]: p for p in store_mod.load_config()["profiles"]}
    assert cur["file-organize"]["name"] == "我改的名字"      # 用户改动保留
    assert {"code-read", "web-research"} <= set(cur)          # 缺失的补齐


def test_refresh_all_never_raises(env, cfg, monkeypatch):
    """refresh_all 单源失败只记 errors[]，函数本身永不抛。"""
    def fake_list_mcp(*_a, **_kw):
        raise catalog_mod.CatalogError(2020, "断网")

    def fake_list_repos(*_a, **_kw):
        return {"items": [{"full_name": "a/b"}], "total_count": 1,
                "fetched_at": 1.0, "stale": False, "error": ""}

    def fake_list_repo_skills(*_a, **_kw):
        raise catalog_mod.CatalogError(2021, "限流")

    monkeypatch.setattr(catalog_mod, "list_mcp", fake_list_mcp)
    monkeypatch.setattr(catalog_mod, "list_repos", fake_list_repos)
    monkeypatch.setattr(catalog_mod, "list_repo_skills", fake_list_repo_skills)
    catalog_mod._cache_write(catalog_mod.CACHE_REPOS,
                             {"items": [{"full_name": "a/b"}]})
    out = catalog_mod.refresh_all(cfg, force=True)
    assert out["ok"] is False
    assert out["counts"]["gh_repos"] == 1
    assert any("mcp" in e for e in out["errors"])
    assert isinstance(out["errors"], list)


# ----------------------------------------------------------------------------
# 联网冒烟（默认跳过）：证明真代理 + trust_env=False 能打通 Registry
# ----------------------------------------------------------------------------
@pytest.mark.skipif(os.environ.get("LLM_RUN_ONLINE_TEST") != "1",
                    reason="联网冒烟：设置 LLM_RUN_ONLINE_TEST=1 才跑")
def test_online_registry_smoke():
    cfg = {"catalog": {"proxy": os.environ.get("HTTPS_PROXY") or
                       catalog_mod.PROXY_DEFAULT,
                       "ttl_hours": {"mcp": 24}}}
    catalog_mod._probe(cfg)
    out = catalog_mod.list_mcp(cfg, force=True, limit=5)
    assert out["stale"] is False
    assert out["items"], "Registry 应至少返回一条"
