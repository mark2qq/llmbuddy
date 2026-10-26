# -*- coding: utf-8 -*-
"""
后端装配层回归（1.0.8 增量 T02）：工具裁剪同源 / 自动加载 / 技能原子安装 / 路由。

覆盖：
    - `select_schemas()` 裁剪优先级（§6.2）与上限、`enabled` 字段
    - `_build_messages` 的工具清单与 schema **同源**（A3 核心，防 R-E1）
    - `list_skills()` 跳过 `.` 目录（A8，防 R-E2）
    - `_atomic_install` 失败回滚 / 覆盖备份 + `prune_backups`（防 R-E3）
    - `mounted_skills` 进 system 不进 message；None 时行为与今天一致（A4）
    - `start.auto_loaded` 字段（S3 接缝）与 `auto_load=false`
    - profile 收窄白名单但**不改 risk**（安全红线）
    - 12 个 REST 端点（不联网；不用 TestClient，见架构 §1.1 D8）

全部用例都在临时目录里跑，网络函数一律 monkeypatch。
"""
import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import agent as agent_mod
from core.assistant import catalog as catalog_mod
from core.assistant import mcp as mcp_mod
from core.assistant import skills as skills_mod
from core.assistant import store as store_mod
from core.assistant.providers import ChatReply
from core.assistant.tools import Tool, ToolRegistry, ToolResult

SKILL_MD_V1 = """---
name: pdf-extract
description: 从 PDF 提取正文
version: 1.0.0
---
# 要求
1. 抽正文
"""

SKILL_MD_V2 = """---
name: pdf-extract
description: 从 PDF 提取正文（v2）
version: 2.0.0
---
# 要求
1. 抽正文，保留页码
"""


# ----------------------------------------------------------------------------
# 夹具
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
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(agent_mod, "_ASSISTANT", None)
    monkeypatch.setattr(mcp_mod, "_MANAGER", None)
    from core.assistant import approval as approval_mod
    monkeypatch.setattr(approval_mod, "_MANAGER", None)
    store_mod.ensure_dirs()
    skills_mod.ensure_builtin_skills()
    return {"root": root, "workspace": root / "workspace",
            "skills": root / "skills"}


class FakeProvider:
    """按队列返回预设回复的假模型；记录收到的 messages / tools 便于断言。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []

    def resolve_model(self, preferred=""):
        return preferred or "fake-model"

    def chat(self, messages, tools=None, model="", temperature=0.3,
             max_tokens=2048, on_delta=None, cancel_check=None, timeout=300):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.replies:
            return ChatReply(content="完成")
        return self.replies.pop(0)


def _system_of(provider) -> str:
    msgs = provider.calls[-1]["messages"]
    return "\n".join(m["content"] for m in msgs if m.get("role") == "system")


def _schema_names(schemas) -> set:
    return {(s.get("function") or {}).get("name") for s in (schemas or [])
            if isinstance(s, dict)}


# ----------------------------------------------------------------------------
# T02-1 工具裁剪
# ----------------------------------------------------------------------------
def test_select_schemas_priority(env):
    """max_tools=4 -> 只留最优先的 4 个低风险只读内置工具（§6.2）。"""
    reg = ToolRegistry(str(env["workspace"]))
    names = [s["function"]["name"] for s in reg.select_schemas(max_tools=4)]
    assert names == ["fs_glob", "fs_grep", "fs_info", "fs_list"] or \
        names == ["fs_glob", "fs_grep", "fs_info", "fs_list"]
    assert set(names) == {"fs_glob", "fs_grep", "fs_info", "fs_list"}
    assert "fs_write" not in names


def test_select_schemas_full_priority_order(env):
    """完整优先级：内置 low -> web_fetch -> skill_* -> 写类 -> 高危 -> MCP。"""
    reg = ToolRegistry(str(env["workspace"]))
    reg.register(Tool(name="mcp__fs__write", description="mcp 写",
                      parameters={"type": "object", "properties": {}},
                      risk="medium", handler=lambda a, c: ToolResult(True),
                      source="mcp:fs"))
    groups = [reg._priority_key(reg.get(n))[0]
              for n in [s["function"]["name"] for s in reg.select_schemas(max_tools=0)]]
    assert groups == sorted(groups), "下发顺序必须单调非降"
    names = [s["function"]["name"] for s in reg.select_schemas(max_tools=0)]
    assert names.index("fs_info") < names.index("web_fetch")
    assert names.index("web_fetch") < names.index("skill_list")
    assert names.index("skill_run") < names.index("fs_write")
    assert names.index("fs_write") < names.index("shell_run")
    assert names.index("shell_run") < names.index("mcp__fs__write")


def test_select_schemas_disabled_and_max(env):
    reg = ToolRegistry(str(env["workspace"]))
    out = reg.select_schemas(disabled={"shell_run", "fs_write"}, max_tools=6)
    names = _schema_names(out)
    assert len(out) <= 6
    assert not ({"shell_run", "fs_write"} & names)


def test_select_schemas_allow_whitelist(env):
    reg = ToolRegistry(str(env["workspace"]))
    out = reg.select_schemas(allow=["fs_read", "web_fetch"], max_tools=24)
    assert _schema_names(out) == {"fs_read", "web_fetch"}


def test_select_schemas_large_limit_keeps_all(env):
    """max_tools=100 -> 全量、不报错（PRD：小于内置数量才裁）。"""
    reg = ToolRegistry(str(env["workspace"]))
    out = reg.select_schemas(max_tools=100)
    assert _schema_names(out) == set(reg.names())


def test_set_filter_makes_openai_schemas_consistent(env):
    reg = ToolRegistry(str(env["workspace"]))
    reg.set_filter(disabled={"shell_run"}, allow=None, max_tools=5)
    assert _schema_names(reg.openai_schemas()) == _schema_names(
        reg.select_schemas(disabled={"shell_run"}, max_tools=5))
    assert "shell_run" not in _schema_names(reg.openai_schemas())


def test_list_has_enabled_field(env):
    """UI 直接读 `enabled`，不再二次计算（S2 接缝）。"""
    reg = ToolRegistry(str(env["workspace"]))
    reg.set_filter(disabled={"shell_run"}, allow=None, max_tools=4)
    rows = {r["name"]: r for r in reg.list()}
    assert rows["fs_read"]["enabled"] is True
    # 只看 disabled，不看 max_tools 裁剪（被上限裁掉另行标记）
    assert rows["fs_write"]["enabled"] is True
    assert rows["shell_run"]["enabled"] is False


# ----------------------------------------------------------------------------
# T02-2 同源（A3 核心 / R-E1）
# ----------------------------------------------------------------------------
def test_build_messages_matches_schemas(env):
    """system 里的工具名集合 == schema 名集合，且不含被禁用的 shell_run。"""
    ass = agent_mod.get_assistant()
    cfg = store_mod.save_config({"tool_prefs": {"disabled": ["shell_run"],
                                                "max_tools": 6}})
    reg = ass.registry()
    schemas = reg.select_schemas(
        disabled={"shell_run"},
        max_tools=int(cfg["tool_prefs"]["max_tools"]))
    assert len(schemas) <= 6
    assert "shell_run" not in _schema_names(schemas)

    session = ass.new_session()
    store_mod.save_session({**store_mod.load_session(session["id"]),
                            "messages": [{"role": "user", "content": "看看目录"}]})
    msgs = ass._build_messages(store_mod.load_session(session["id"]), reg, cfg,
                              schemas)
    system = msgs[0]["content"]
    import re
    listed = set(re.findall(r"^- ([A-Za-z0-9_]+)（", system, re.M))
    assert listed == _schema_names(schemas), (sorted(listed),
                                             sorted(_schema_names(schemas)))
    assert "shell_run" not in system
    assert "（暂无工具）" not in system


def test_build_messages_uses_passed_schemas_not_registry(env):
    """显式传入的 schemas 必须被原样采用（不能偷偷回读 registry 全量）。"""
    ass = agent_mod.get_assistant()
    cfg = store_mod.load_config()
    reg = ass.registry()
    fake = [{"type": "function", "function": {
        "name": "only_one", "description": "唯一工具", "parameters": {}}}]
    msgs = ass._build_messages({"messages": []}, reg, cfg, fake)
    assert "only_one" in msgs[0]["content"]
    assert "fs_read" not in msgs[0]["content"]


def test_provider_chat_gets_same_schemas_as_system_prompt(env, monkeypatch):
    """start_turn 里 provider 与 system 用同一份 schemas（同源端到端）。"""
    provider = FakeProvider([ChatReply(content="好的")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"tool_prefs": {"disabled": ["shell_run"],
                                         "max_tools": 5}})
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    ass.start_turn(session["id"], "随便问问", emit=lambda e: None)
    sent = provider.calls[-1]["tools"]
    import re
    listed = set(re.findall(r"^- ([A-Za-z0-9_]+)（", _system_of(provider), re.M))
    assert listed == _schema_names(sent)
    assert len(sent) <= 5


# ----------------------------------------------------------------------------
# T02-3 技能目录（A8 / R-E2）
# ----------------------------------------------------------------------------
def test_list_skills_skips_dot_dirs(env):
    skills_dir = str(env["skills"])
    for name in (".installing-abc", ".backup-123"):
        d = os.path.join(skills_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
            f.write(SKILL_MD_V1)
    slugs = [s["slug"] for s in skills_mod.list_skills()]
    assert ".installing-abc" not in slugs
    assert ".backup-123" not in slugs
    # 既有防线不变：delete_skill / export_zip 天然拒绝 `.` 开头
    assert skills_mod.delete_skill(".backup-123") is False
    with pytest.raises(skills_mod.SkillError):
        skills_mod.export_zip(".backup-123")


# ----------------------------------------------------------------------------
# T02-4 原子安装（R-E3）
# ----------------------------------------------------------------------------
def test_install_rollback_on_failure(env, monkeypatch):
    """写文件阶段炸掉 -> 目标保持旧内容、临时目录清空、备份回滚。"""
    res = skills_mod._atomic_install("pdf-extract", {"SKILL.md": SKILL_MD_V1},
                                     overwrite=False)
    assert res["slug"] == "pdf-extract"
    target = os.path.join(str(env["skills"]), "pdf-extract")
    before = open(os.path.join(target, "SKILL.md"), encoding="utf-8").read()
    assert "v1" not in before and "从 PDF 提取正文" in before

    real_write = skills_mod._write_files

    def boom(root, files):
        real_write(root, files)
        raise OSError("模拟写盘中断")

    monkeypatch.setattr(skills_mod, "_write_files", boom)
    with pytest.raises(skills_mod.SkillError):
        skills_mod._atomic_install("pdf-extract", {"SKILL.md": SKILL_MD_V2},
                                   overwrite=True)
    left = open(os.path.join(target, "SKILL.md"), encoding="utf-8").read()
    assert left == before, "失败后目标目录必须保持安装前内容"
    names = os.listdir(str(env["skills"]))
    assert not [n for n in names if n.startswith(".installing-")], names
    assert not [n for n in names if n.startswith(".backup-")], names


def test_install_overwrite_backup(env):
    skills_mod._atomic_install("pdf-extract", {"SKILL.md": SKILL_MD_V1})
    second = skills_mod._atomic_install("pdf-extract", {"SKILL.md": SKILL_MD_V2},
                                        overwrite=True)
    assert second["overwritten"] is True
    assert second["backup"].startswith(".backup-")
    target = os.path.join(str(env["skills"]), "pdf-extract")
    assert "v2" in open(os.path.join(target, "SKILL.md"), encoding="utf-8").read()
    backups = [n for n in os.listdir(str(env["skills"]))
               if n.startswith(".backup-")]
    assert len(backups) == 1


def test_install_requires_overwrite_flag(env):
    skills_mod._atomic_install("pdf-extract", {"SKILL.md": SKILL_MD_V1})
    with pytest.raises(skills_mod.SkillError) as ei:
        skills_mod._atomic_install("pdf-extract", {"SKILL.md": SKILL_MD_V2},
                                   overwrite=False)
    assert "已存在" in str(ei.value)


def test_install_rejects_missing_skill_md_or_name(env):
    with pytest.raises(skills_mod.SkillError):
        skills_mod._atomic_install("x1", {"notes.md": "hi"})
    with pytest.raises(skills_mod.SkillError):
        skills_mod._atomic_install("x2", {"SKILL.md": "# 没有 frontmatter"})
    with pytest.raises(skills_mod.SkillError):
        skills_mod._atomic_install(".dotted", {"SKILL.md": SKILL_MD_V1})


def test_prune_backups_keeps_latest(env):
    skills_dir = str(env["skills"])
    for i in range(8):
        d = os.path.join(skills_dir, f".backup-2026010100000{i}")
        os.makedirs(d, exist_ok=True)
        os.utime(d, (1_700_000_000 + i, 1_700_000_000 + i))
    removed = skills_mod.prune_backups(keep=5)
    assert removed == 3
    left = [n for n in os.listdir(skills_dir) if n.startswith(".backup-")]
    assert len(left) == 5
    assert ".backup-20260101000007" in left


def test_install_from_github_records_source(env):
    bundle = {"slug": "pdf-extract", "name": "pdf-extract",
              "files": {"SKILL.md": SKILL_MD_V1, "notes.md": "备注"},
              "bytes": 100, "has_scripts": False, "commit": "abc123",
              "ref": "main", "repo": "acme/skills", "dir": "skills/pdf"}
    out = skills_mod.install_from_github(
        bundle, "", False,
        {"repo": "acme/skills", "ref": "main", "dir": "skills/pdf",
         "commit": "abc123", "has_scripts": False})
    assert out["slug"] == "pdf-extract" and out["commit"] == "abc123"
    src = store_mod.load_config()["skill_sources"]["pdf-extract"]
    assert src["repo"] == "acme/skills" and src["kind"] == "github"
    assert out["slug"] in [s["slug"] for s in skills_mod.list_skills()]


def test_install_from_dir_copies_binary(env, tmp_path):
    """本地目录导入现在复制全部文件（含二进制 SVG/PNG），并排除 .git。"""
    src = tmp_path / "my-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(SKILL_MD_V1, encoding="utf-8")
    (src / "notes.md").write_text("备注", encoding="utf-8")
    (src / "icon.svg").write_bytes(b"<svg></svg>")
    (src / "pic.png").write_bytes(b"\x89PNG\r\n")
    (src / "scripts").mkdir()
    (src / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    # .git 不应被装进来
    (src / ".git").mkdir()
    (src / ".git" / "config").write_text("garbage", encoding="utf-8")
    out = skills_mod.install_from_dir(str(src))
    assert out["slug"] == "pdf-extract"
    assert out["has_scripts"] is True
    d = os.path.join(str(env["skills"]), "pdf-extract")
    assert os.path.isfile(os.path.join(d, "notes.md"))
    assert os.path.isfile(os.path.join(d, "icon.svg"))
    assert os.path.isfile(os.path.join(d, "pic.png"))
    assert not os.path.exists(os.path.join(d, ".git"))
    with pytest.raises(skills_mod.SkillError):
        skills_mod.install_from_dir(str(tmp_path / "nowhere"))


def test_install_from_github_binary(env, monkeypatch):
    """二进制整包（files 为 bytes + binary=True）原样落盘，不损坏。"""
    bundle = {"slug": "pdf-extract", "name": "pdf-extract",
              "files": {"SKILL.md": SKILL_MD_V1.encode("utf-8"),
                        "icon.svg": b"<svg>bytes</svg>",
                        "sound.wav": bytes(range(256))},
              "bytes": 300, "has_scripts": False, "commit": "abc123",
              "ref": "main", "repo": "acme/skills", "dir": "skills/pdf",
              "skipped": 0, "binary": True}
    out = skills_mod.install_from_github(
        bundle, "", False,
        {"repo": "acme/skills", "ref": "main", "dir": "skills/pdf",
         "commit": "abc123", "has_scripts": False})
    assert out["slug"] == "pdf-extract"
    d = os.path.join(str(env["skills"]), "pdf-extract")
    with open(os.path.join(d, "icon.svg"), "rb") as f:
        assert f.read() == b"<svg>bytes</svg>"
    with open(os.path.join(d, "sound.wav"), "rb") as f:
        assert f.read() == bytes(range(256))
    src = store_mod.load_config()["skill_sources"]["pdf-extract"]
    assert src["kind"] == "github"


def test_fetch_skill_archive_extracts_binary(env, monkeypatch):
    """fetch_skill_archive 抽取子树 tarball：二进制保留、.. 逃逸与符号链接跳过、
    受 max_bytes 预算约束。"""
    import io
    import tarfile

    SKILL_MD = """---
name: ppt-master
description: 大技能
version: 6.7.3
---
# 要求
1. 干活
"""
    # 构造子树 tarball（顶层目录 ppt-master-main/）
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def add_dir(p):
            ti = tarfile.TarInfo(name=p)
            ti.type = tarfile.DIRTYPE
            tf.addfile(ti)
        def add_file(p, data):
            b = data if isinstance(data, bytes) else data.encode("utf-8")
            ti = tarfile.TarInfo(name=p)
            ti.size = len(b)
            tf.addfile(ti, io.BytesIO(b))
        def add_sym(p, target):
            ti = tarfile.TarInfo(name=p)
            ti.type = tarfile.SYMTYPE
            ti.linkname = target
            tf.addfile(ti)
        add_dir("ppt-master-main")
        add_dir("ppt-master-main/skills")
        add_dir("ppt-master-main/skills/ppt-master")
        add_file("ppt-master-main/skills/ppt-master/SKILL.md", SKILL_MD)
        add_file("ppt-master-main/skills/ppt-master/icon.svg", b"<svg></svg>")
        add_file("ppt-master-main/skills/ppt-master/sound.wav", bytes(range(200)))
        add_file("ppt-master-main/skills/ppt-master/notes.md", "备注")
        # 越界尝试：.. 逃逸
        add_file("ppt-master-main/skills/ppt-master/../escape.txt", "evil")
        # 符号链接（应跳过）
        add_sym("ppt-master-main/skills/ppt-master/link.md", "SKILL.md")
    raw = buf.getvalue()

    class _Resp:
        status_code = 200
        headers: dict = {}
        content = raw

    monkeypatch.setattr(catalog_mod, "_do_get",
                        lambda *a, **k: _Resp())
    monkeypatch.setattr(catalog_mod, "_get_text",
                        lambda *a, **k: SKILL_MD)
    monkeypatch.setattr(catalog_mod, "list_repo_skills",
                        lambda *a, **k: {
                            "commit": "deadbeef", "ref": "main",
                            "items": [{"slug": "ppt-master", "dir": "skills/ppt-master",
                                       "name": "ppt-master", "description": "大技能",
                                       "path": "skills/ppt-master/SKILL.md",
                                       "files": ["icon.svg", "sound.wav", "notes.md"],
                                       "file_count": 3, "has_scripts": False}]})

    bundle = catalog_mod.fetch_skill_archive(
        None, "hugohe3", "ppt-master", "main", "skills/ppt-master")
    assert bundle["binary"] is True
    assert "SKILL.md" in bundle["files"]
    assert bundle["files"]["icon.svg"] == b"<svg></svg>"
    assert bundle["files"]["sound.wav"] == bytes(range(200))
    assert "notes.md" in bundle["files"]
    # 越界与符号链接被计入 skipped，不出现在 files
    assert "escape.txt" not in bundle["files"]
    assert "link.md" not in bundle["files"]
    assert bundle["skipped"] >= 2
    assert bundle["commit"] == "deadbeef"

    # 预算约束：max_bytes 介于「仅够 SKILL.md」与「全装下」之间时，
    # SKILL.md 保留、其余附件被计入 skipped（SKILL.md 自身占 82 字节）。
    small = catalog_mod.fetch_skill_archive(
        None, "hugohe3", "ppt-master", "main", "skills/ppt-master", max_bytes=90)
    assert "SKILL.md" in small["files"]
    assert small["skipped"] >= 3
    assert "icon.svg" not in small["files"]


# ----------------------------------------------------------------------------
# T02-5 mounted_skills（A4 / R-E6）
# ----------------------------------------------------------------------------
def test_start_turn_mounted_skills_none_is_noop(env, monkeypatch):
    """不传 mounted_skills：system 里没有「本轮指定技能」段（老客户端零影响）。"""
    provider = FakeProvider([ChatReply(content="好")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"auto_load": False})
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    ass.start_turn(session["id"], "普通的一句话", emit=lambda e: None)
    assert "本轮指定技能" not in _system_of(provider)
    assert "可用技能" not in _system_of(provider)


def test_start_turn_mounted_skills_written_to_system(env, monkeypatch):
    """挂载技能只进 system，落库 message 是原文（无「用 xx 技能：」前缀）。"""
    provider = FakeProvider([ChatReply(content="收到")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"auto_load": False})
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    res = ass.start_turn(session["id"], "看看这个项目", emit=lambda e: None,
                         mounted_skills=["repo-digest"])
    assert res["ok"]
    system = _system_of(provider)
    assert "本轮指定技能" in system and "repo-digest" in system
    saved = store_mod.load_session(session["id"])
    user_msgs = [m for m in saved["messages"] if m["role"] == "user"]
    assert user_msgs[-1]["content"] == "看看这个项目"
    assert "技能" not in user_msgs[-1]["content"]


def test_chat_mounted_skills_passthrough(env, monkeypatch):
    """POST /chat 透传 mounted_skills（非流式路径）。"""
    captured = {}

    # 注意：必须打在类上（打在实例上不会生效 —— run_in_threadpool 传参走的是
    # 类属性查找到的函数），且要接住隐式 self。这是本用例曾经 KeyError 的根因。
    def fake_start_turn(self_, sid, text, provider_id="", model="", emit=None,
                        mounted_skills=None):
        captured.update({"sid": sid, "text": text,
                         "mounted": mounted_skills})
        return {"ok": True, "content": "ok"}

    ass = agent_mod.get_assistant()
    monkeypatch.setattr(agent_mod.Assistant, "start_turn", fake_start_turn)
    from routers import assistant as api
    session = ass.new_session()
    body = asyncio.run(api.chat({"session_id": session["id"], "message": "x",
                                 "mounted_skills": ["a", "b"], "stream": False}))
    assert body["code"] == 0
    assert captured["mounted"] == ["a", "b"]
    assert captured["text"] == "x"


def test_chat_without_mounted_skills_is_none(env, monkeypatch):
    """老客户端不带 mounted_skills -> 透传 None（行为与 1.0.7 一致）。"""
    captured = {}

    def fake_start_turn(self_, sid, text, provider_id="", model="", emit=None,
                        mounted_skills=None):
        captured["mounted"] = mounted_skills
        return {"ok": True, "content": ""}

    ass = agent_mod.get_assistant()
    monkeypatch.setattr(agent_mod.Assistant, "start_turn", fake_start_turn)
    from routers import assistant as api
    session = ass.new_session()
    body = asyncio.run(api.chat({"session_id": session["id"], "message": "y",
                                 "stream": False}))
    assert body["code"] == 0
    assert captured["mounted"] is None


# ----------------------------------------------------------------------------
# T02-6 自动加载（start.auto_loaded / S3 接缝）
# ----------------------------------------------------------------------------
def test_auto_loaded_event(env, monkeypatch):
    provider = FakeProvider([ChatReply(content="好的")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"auto_load": True,
                           "tool_prefs": {"disabled": [], "max_tools": 24}})
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    ass.start_turn(session["id"], "帮我整理目录", emit=events.append)
    starts = [e for e in events if e.get("type") == "start"]
    assert len(starts) == 1
    al = starts[0]["auto_loaded"]
    assert al["profile"] == "file-organize"
    assert al["name"] == "文件整理"
    assert al["skills"] == ["workspace-organizer"]
    assert isinstance(al["tools"], int) and 0 < al["tools"] <= 24


def test_auto_loaded_empty_when_disabled(env, monkeypatch):
    provider = FakeProvider([ChatReply(content="好的")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"auto_load": False})
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    ass.start_turn(session["id"], "帮我整理目录", emit=events.append)
    start = [e for e in events if e.get("type") == "start"][0]
    assert start["auto_loaded"] == {}


def test_profile_reduces_allow_list(env, monkeypatch):
    """命中 web-research -> schema 名集合 ⊆ profile.tools；risk 不被降低。"""
    provider = FakeProvider([ChatReply(content="好的")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"auto_load": True})
    ass = agent_mod.get_assistant()
    reg = ass.registry()
    before = reg.risk_of("skill_run", {"name": "web-brief", "task": "x"})
    session = ass.new_session()
    ass.start_turn(session["id"], "帮我抓取这个网页并总结一下", emit=lambda e: None)
    names = _schema_names(provider.calls[-1]["tools"])
    assert names <= {"web_fetch", "fs_write", "skill_run"}
    assert reg.risk_of("skill_run", {"name": "web-brief", "task": "x"}) == before


def test_no_profile_writes_available_skills_hint(env, monkeypatch):
    """未命中 profile 且 auto_load=true -> system 写「可用技能」（L3 降级）。"""
    provider = FakeProvider([ChatReply(content="好的")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    store_mod.save_config({"auto_load": True})
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    ass.start_turn(session["id"], "今天天气怎么样", emit=lambda e: None)
    system = _system_of(provider)
    assert "可用技能" in system
    assert "workspace-organizer" in system


# ----------------------------------------------------------------------------
# T02-7 REST 端点（不联网）
# ----------------------------------------------------------------------------
def test_route_catalog_smoke(env):
    """12 个新端点都挂在 serve.app.routes 上；GET 端点可用缓存路径不联网。"""
    import serve
    paths = {getattr(r, "path", "") for r in serve.app.routes}
    expected = {
        "/api/v1/assistant/catalog/mcp",
        "/api/v1/assistant/catalog/skills",
        "/api/v1/assistant/catalog/skills/{owner}/{repo}",
        "/api/v1/assistant/catalog/skills/install",
        "/api/v1/assistant/catalog/mcp/install",
        "/api/v1/assistant/catalog/refresh",
        "/api/v1/assistant/catalog/skills/from_dir",
        "/api/v1/assistant/catalog/skills/from_url",
        "/api/v1/assistant/tools/prefs",
        "/api/v1/assistant/profiles",
    }
    assert expected <= paths, sorted(expected - paths)

    from routers import assistant as api
    catalog_mod._cache_write(catalog_mod.CACHE_MCP,
                             {"items": [{"registry_name": "a/b"}],
                              "cursor": None})
    body = asyncio.run(api.catalog_mcp(q="", cursor="", force=False))
    assert body["code"] == 0 and len(body["data"]["items"]) == 1


def test_route_catalog_mcp_unreachable_returns_2021(env, monkeypatch):
    """无缓存 + 拉取失败 -> fail(2020)；缓存过期 -> stale 且 code==0。"""
    def boom(*_a, **_kw):
        raise catalog_mod.CatalogError(2020, "无法连接在线目录（代理：x）")

    monkeypatch.setattr(catalog_mod, "_get_json", boom)
    from routers import assistant as api
    body = asyncio.run(api.catalog_mcp(q="", cursor="", force=True))
    assert body["code"] == 2020 and "代理" in body["message"]

    catalog_mod._cache_write(catalog_mod.CACHE_MCP,
                             {"items": [{"registry_name": "a/b"}], "cursor": None})
    body2 = asyncio.run(api.catalog_mcp(q="", cursor="", force=True))
    assert body2["code"] == 0 and body2["data"]["stale"] is True
    assert len(body2["data"]["items"]) == 1


def test_route_catalog_skills_and_repo(env, monkeypatch):
    def fake_get_json(_cfg, url, params=None, timeout=5.0):
        if "search/repositories" in url:
            return ({"total_count": 1, "items": [{
                "full_name": "acme/skills", "name": "skills",
                "owner": {"login": "acme"}, "stargazers_count": 9,
                "default_branch": "main", "description": "技能",
                "topics": ["claude-skills"]}]}, {})
        if "git/trees" in url:
            return ({"sha": "c0ffee", "tree": [
                {"path": "skills/pdf/SKILL.md", "type": "blob"},
                {"path": "skills/pdf/notes.md", "type": "blob"}]}, {})
        return {"default_branch": "main"}, {}

    monkeypatch.setattr(catalog_mod, "_get_json", fake_get_json)
    monkeypatch.setattr(catalog_mod, "_get_text", lambda *a, **k: SKILL_MD_V1)
    from routers import assistant as api
    repos = asyncio.run(api.catalog_skills(q="", force=True))
    assert repos["code"] == 0 and repos["data"]["items"][0]["stars"] == 9
    detail = asyncio.run(api.catalog_repo_skills("acme", "skills", "main"))
    assert detail["code"] == 0
    assert detail["data"]["items"][0]["dir"] == "skills/pdf"


def test_route_skill_install_end_to_end(env, monkeypatch):
    """在线安装技能全链路：fetch_skill_archive -> 原子落盘 -> 出现在 list_skills。"""
    monkeypatch.setattr(catalog_mod, "fetch_skill_archive", lambda *a, **k: {
        "slug": "pdf-extract", "name": "pdf-extract",
        "files": {"SKILL.md": SKILL_MD_V1}, "bytes": 120,
        "has_scripts": False, "commit": "c0ffee", "ref": "main",
        "repo": "acme/skills", "dir": "skills/pdf", "skipped": 0})
    from routers import assistant as api
    body = asyncio.run(api.catalog_install_skill(
        {"repo": "acme/skills", "ref": "main", "dir": "skills/pdf"}))
    assert body["code"] == 0, body
    assert body["data"]["slug"] == "pdf-extract"
    assert "pdf-extract" in [s["slug"] for s in skills_mod.list_skills()]
    # 已存在且 overwrite=false -> 2023
    again = asyncio.run(api.catalog_install_skill(
        {"repo": "acme/skills", "ref": "main", "dir": "skills/pdf"}))
    assert again["code"] == 2023
    # 非法 repo -> 2022
    bad = asyncio.run(api.catalog_install_skill({"repo": "noslash"}))
    assert bad["code"] == 2022


def test_route_skill_from_dir(env, tmp_path):
    src = tmp_path / "local-skill"
    src.mkdir()
    (src / "SKILL.md").write_text(SKILL_MD_V1, encoding="utf-8")
    from routers import assistant as api
    body = asyncio.run(api.catalog_skill_from_dir({"path": str(src)}))
    assert body["code"] == 0 and body["data"]["slug"] == "pdf-extract"
    again = asyncio.run(api.catalog_skill_from_dir({"path": str(src)}))
    assert again["code"] == 2023
    missing = asyncio.run(api.catalog_skill_from_dir({"path": str(tmp_path / "no")}))
    assert missing["code"] == 2022


def test_route_skill_from_url(env, monkeypatch):
    monkeypatch.setattr(catalog_mod, "fetch_bundle_from_raw", lambda *a, **k: {
        "slug": "pdf-extract", "name": "pdf-extract",
        "files": {"SKILL.md": SKILL_MD_V1}, "bytes": 120,
        "has_scripts": False, "commit": "", "ref": "main",
        "repo": "acme/skills", "dir": "skills/pdf"})
    from routers import assistant as api
    body = asyncio.run(api.catalog_skill_from_url(
        {"url": "https://github.com/acme/skills/blob/main/skills/pdf/SKILL.md"}))
    assert body["code"] == 0 and body["data"]["slug"] == "pdf-extract"

    # 不受支持的地址 -> 2026
    def bad_url(*_a, **_kw):
        raise catalog_mod.CatalogError(catalog_mod.ERR_BAD_URL,
                                      "只支持 GitHub / Gitee 的 SKILL.md 直链")
    monkeypatch.setattr(catalog_mod, "fetch_bundle_from_raw", bad_url)
    bad = asyncio.run(api.catalog_skill_from_url({"url": "https://x.com/a.md"}))
    assert bad["code"] == 2026


def test_route_mcp_install_env_missing_2024(env):
    from routers import assistant as api
    item = catalog_mod.normalize_mcp_server({
        "name": "com.acme/need-key", "title": "需要密钥", "version": "1.0.0",
        "packages": [{"registryType": "npm", "runtimeHint": "npx",
                      "identifier": "acme-mcp", "version": "1.0.0",
                      "environmentVariables": [{"name": "API_KEY",
                                                "isRequired": True}]}]})
    body = asyncio.run(api.catalog_install_mcp(
        {"registry_name": "com.acme/need-key", "item": item, "connect": False}))
    assert body["code"] == 2024
    assert body["data"]["missing"] == ["API_KEY"]


def test_route_mcp_install_success_without_connect(env):
    from routers import assistant as api
    item = catalog_mod.normalize_mcp_server({
        "name": "com.example/remote-http", "title": "远程 MCP", "version": "1.0.0",
        "remotes": [{"type": "streamable-http", "url": "https://mcp.example.com/v1"}]})
    body = asyncio.run(api.catalog_install_mcp(
        {"registry_name": "com.example/remote-http", "item": item,
         "connect": False, "risk": "high"}))
    assert body["code"] == 0, body
    assert body["data"]["connected"] is False
    servers = mcp_mod.get_mcp_manager().servers()
    assert any(s["id"] == "com.example-remote-http" for s in servers)
    assert next(s for s in servers
                if s["id"] == "com.example-remote-http")["transport"] == "http"


def test_route_refresh_always_ok(env, monkeypatch):
    def boom(*_a, **_kw):
        raise catalog_mod.CatalogError(2020, "断网")

    monkeypatch.setattr(catalog_mod, "list_mcp", boom)
    monkeypatch.setattr(catalog_mod, "list_repos", boom)
    from routers import assistant as api
    body = asyncio.run(api.catalog_refresh())
    assert body["code"] == 0                     # 永 ok()，错误在 data 里
    assert body["data"]["ok"] is False
    assert body["data"]["errors"]


# ----------------------------------------------------------------------------
# P2-1：catalog_repo_skills 的 force 必须透传到 list_repo_skills
# ----------------------------------------------------------------------------
def test_route_catalog_repo_skills_passes_force(env, monkeypatch):
    """?force=1 必须透传到 list_repo_skills(force=True)，不被静默丢弃。"""
    seen = {}

    def fake_list_repo_skills(cfg, owner, repo, ref="", force=False):
        seen.update(owner=owner, repo=repo, ref=ref, force=force)
        return {"commit": "", "ref": ref, "items": [], "fetched_at": 0,
                "stale": False, "error": ""}

    monkeypatch.setattr(catalog_mod, "list_repo_skills", fake_list_repo_skills)
    from routers import assistant as api
    body = asyncio.run(api.catalog_repo_skills("acme", "skills", "main",
                                               force=True))
    assert body["code"] == 0
    assert seen["force"] is True
    assert (seen["owner"], seen["repo"], seen["ref"]) == ("acme", "skills", "main")

    # 默认不带 force -> False
    asyncio.run(api.catalog_repo_skills("acme", "skills"))
    assert seen["force"] is False


# ----------------------------------------------------------------------------
# P2-3：手动 POST /mcp 必须与在线安装共用命令白名单，拒绝任意命令
# ----------------------------------------------------------------------------
def test_add_server_rejects_non_whitelisted_command(env):
    """add_server 对 stdio 的 command 兜底校验：curl / rm / bash 一律拒绝。"""
    mgr = mcp_mod.get_mcp_manager()
    for bad in ("curl", "rm", "bash", "powershell", "", "/usr/bin/curl"):
        with pytest.raises(mcp_mod.MCPError):
            mgr.add_server({"id": "x", "transport": "stdio", "command": bad})
    # 合法命令放行
    ok_cfg = mgr.add_server({"id": "ok1", "transport": "stdio",
                             "command": "npx", "args": ["-y", "pkg"]})
    assert ok_cfg["id"] == "ok1"
    # http 传输不校验 command
    http_cfg = mgr.add_server({"id": "ok2", "transport": "http",
                               "url": "https://x/mcp"})
    assert http_cfg["transport"] == "http"


def test_route_mcp_add_rejects_curl(env):
    """POST /mcp 带 command=curl -> fail(2022)，不落配置。"""
    from routers import assistant as api
    body = asyncio.run(api.mcp_add({"id": "evil", "transport": "stdio",
                                    "command": "curl",
                                    "args": ["http://evil"]}))
    assert body["code"] == 2022
    assert "白名单" in body["message"] or "只允许" in body["message"]
    ids = [s.get("id") for s in mcp_mod.get_mcp_manager().servers()]
    assert "evil" not in ids

    # 合法 npx 放行
    ok_body = asyncio.run(api.mcp_add({"id": "good", "transport": "stdio",
                                       "command": "npx", "args": ["-y", "pkg"],
                                       "connect": False}))
    assert ok_body["code"] == 0
    assert "good" in [s.get("id") for s in mcp_mod.get_mcp_manager().servers()]


# ----------------------------------------------------------------------------
# P2-4：_do_get 对传输层抖动重试（仅 Connection/Timeout/TLS），4xx 不重试
# ----------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._payload


class _FakeClient:
    """按队列返回 response 或抛异常的假 httpx.Client。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        self.calls += 1
        item = self.script.pop(0) if self.script else _FakeResp()
        if isinstance(item, Exception):
            raise item
        return item


def test_get_json_retries_transport_error_then_succeeds(env, monkeypatch):
    """第一次抛 ConnectError，第二次成功 -> 最终成功且只多打一次。"""
    import httpx
    fake = _FakeClient([httpx.ConnectError("boom"),
                        _FakeResp(200, {"servers": []}, headers={})])
    monkeypatch.setattr(catalog_mod, "_client",
                        lambda *a, **k: fake)
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda *_a, **_k: None)
    data, _h = catalog_mod._get_json({}, "https://registry/x")
    assert data == {"servers": []}
    assert fake.calls == 2                       # 首败 + 1 次重试 = 2 次请求


def test_get_json_retries_exhausted_raises_2020(env, monkeypatch):
    """连续传输层失败 -> 2 次重试后仍失败 -> CatalogError(2020)。"""
    import httpx
    fake = _FakeClient([httpx.ConnectError("a"), httpx.ConnectError("b"),
                        httpx.ConnectError("c")])
    monkeypatch.setattr(catalog_mod, "_client", lambda *a, **k: fake)
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod._get_json({}, "https://registry/x")
    assert ei.value.code == 2020
    assert fake.calls == 1 + catalog_mod.RETRY_MAX


def test_get_json_4xx_not_retried(env, monkeypatch):
    """HTTP 403（非传输层）不重试，只请求一次，报 2021。"""
    fake = _FakeClient([_FakeResp(403, {}, headers={})])
    monkeypatch.setattr(catalog_mod, "_client", lambda *a, **k: fake)
    monkeypatch.setattr(catalog_mod.time, "sleep", lambda *_a, **_k: None)
    with pytest.raises(catalog_mod.CatalogError) as ei:
        catalog_mod._get_json({}, "https://api.github.com/x")
    assert ei.value.code == 2021
    assert fake.calls == 1                       # 4xx 不重试


def test_tool_prefs_roundtrip(env):
    from routers import assistant as api
    put = asyncio.run(api.put_tool_prefs({"disabled": ["shell_run"],
                                          "max_tools": 16}))
    assert put["code"] == 0, put
    get = asyncio.run(api.get_tool_prefs())
    assert get["data"]["disabled"] == ["shell_run"]
    assert get["data"]["max_tools"] == 16
    assert isinstance(get["data"]["total"], int) and get["data"]["total"] > 0

    state = asyncio.run(api.state())
    rows = {t["name"]: t for t in state["data"]["tools"]}
    assert rows["shell_run"]["enabled"] is False

    # 参数非法 -> 2002
    assert asyncio.run(api.put_tool_prefs({"max_tools": 999}))["code"] == 2002
    assert asyncio.run(api.put_tool_prefs({"max_tools": "abc"}))["code"] == 2002
    assert asyncio.run(api.put_tool_prefs({"disabled": "shell_run"}))["code"] == 2002


def test_profiles_roundtrip(env):
    from routers import assistant as api
    get = asyncio.run(api.get_profiles())
    assert get["code"] == 0
    assert get["data"]["auto_load"] is True
    assert {p["id"] for p in get["data"]["profiles"]} == {
        "file-organize", "code-read", "web-research"}

    put = asyncio.run(api.put_profiles({"auto_load": False}))
    assert put["code"] == 0 and put["data"]["auto_load"] is False

    cur = get["data"]["profiles"]
    patch = [{"id": "file-organize", "enabled": False},
             {"id": "code-read", "enabled": True, "priority": 5},
             {"id": "web-research", "enabled": True}]
    put2 = asyncio.run(api.put_profiles({"profiles": patch}))
    assert put2["code"] == 0
    rows = {p["id"]: p for p in put2["data"]["profiles"]}
    assert rows["file-organize"]["enabled"] is False
    assert rows["code-read"]["priority"] == 5

    assert asyncio.run(api.put_profiles({"profiles": "x"}))["code"] == 2002
    assert asyncio.run(api.put_profiles(
        {"profiles": [{"id": "nope"}]}))["code"] == 2002
    assert asyncio.run(api.put_profiles(
        {"profiles": [{"id": "code-read", "priority": "high"}]}))["code"] == 2002


# ---------------------------------------------------------------------------
# web_fetch 出网必须走 catalog 同源代理（防 R-F1：trust_env=True 在本机=全失败）
# ---------------------------------------------------------------------------
def test_web_fetch_uses_catalog_proxy(monkeypatch):
    """web_fetch 不得再依赖环境变量：必须 trust_env=False + 显式 proxy。

    背景：本机代理不在环境变量里，旧实现 trust_env=True 导致 api.github.com
    等全部请求失败（用户截图里的「访问网络 … 失败」×6 + 撞 12 次上限）。
    """
    captured = {}

    class _Resp:
        status_code = 200
        text = "hello"
        url = "https://api.github.com/repos/x/y"

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            captured["url"] = url
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    reg = ToolRegistry(workspace=os.getcwd())
    out = reg.call("web_fetch", {"url": "https://api.github.com/repos/x/y"})

    assert out.ok and out.content == "hello"
    assert captured["trust_env"] is False
    assert captured["proxy"] == catalog_mod._proxy()
    assert captured["url"] == "https://api.github.com/repos/x/y"
    # 代理解析优先级：env 优先于默认值
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    assert catalog_mod._proxy() == "http://127.0.0.1:9999"


def test_web_fetch_error_reports_proxy(monkeypatch):
    """请求失败时报错里必须带代理值，便于用户自查（不静默直连）。"""
    import httpx

    def _boom(**kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(httpx, "Client", _boom)
    reg = ToolRegistry(workspace=os.getcwd())
    out = reg.call("web_fetch", {"url": "https://example.com"})
    assert not out.ok
    assert "代理" in out.error and catalog_mod._proxy() in out.error


# ---------------------------------------------------------------------------
# web_fetch 出网必须走 catalog 同源代理（防 R-F1：trust_env=True 在本机=全失败）
# ---------------------------------------------------------------------------
def test_web_fetch_uses_catalog_proxy(monkeypatch):
    """web_fetch 不得再依赖环境变量：必须 trust_env=False + 显式 proxy。

    背景：本机代理不在环境变量里，旧实现 trust_env=True 导致 api.github.com
    等全部请求失败（用户截图里的「访问网络 … 失败」×6 + 撞 12 次上限）。
    """
    captured = {}

    class _Resp:
        status_code = 200
        text = "hello"
        url = "https://api.github.com/repos/x/y"

    class _Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            captured["url"] = url
            return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", _Client)
    reg = ToolRegistry(workspace=os.getcwd())
    out = reg.call("web_fetch", {"url": "https://api.github.com/repos/x/y"})

    assert out.ok and out.content == "hello"
    assert captured["trust_env"] is False
    assert captured["proxy"] == catalog_mod._proxy()
    assert captured["url"] == "https://api.github.com/repos/x/y"
    # 代理解析优先级：env 优先于默认值
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    assert catalog_mod._proxy() == "http://127.0.0.1:9999"


def test_web_fetch_error_reports_proxy(monkeypatch):
    """请求失败时报错里必须带代理值，便于用户自查（不静默直连）。"""
    import httpx

    def _boom(**kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(httpx, "Client", _boom)
    reg = ToolRegistry(workspace=os.getcwd())
    out = reg.call("web_fetch", {"url": "https://example.com"})
    assert not out.ok
    assert "代理" in out.error and catalog_mod._proxy() in out.error
