# -*- coding: utf-8 -*-
"""
默认技能与默认工具的注册 / 自动加载（1.0.12）回归。

覆盖：
    - 默认技能包落盘：首次全量、二次幂等、`bundle_version` 升级、用户同名技能不覆盖
    - frontmatter 解析：triggers（list / "a, b" / "a、b"）、auto_load、default、priority
    - `defaults.select()`：触发词打分、阈值、上限、停用名单、确定性排序
    - 优先级：用户挂载 > profile > 默认技能 > L3 清单
    - 注入：system 里有「本轮自动加载的默认技能」+ 首技能正文摘录
    - 默认工具：`meta.default/group` 标记、`prefer` 只调顺序、用户禁用优先
    - 配置接缝：`auto_skill_off` / `auto_skill_limit` / `auto_load=false`

全部用例跑在临时目录，网络与真实技能目录都不碰。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import agent as agent_mod
from core.assistant import bundled as bundled_mod
from core.assistant import defaults as defaults_mod
from core.assistant import mcp as mcp_mod
from core.assistant import profiles as profiles_mod
from core.assistant import skills as skills_mod
from core.assistant import store as store_mod
from core.assistant.providers import ChatReply


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


def _skill_dir(name: str) -> str:
    return os.path.join(skills_mod.SKILLS_DIR, name, "SKILL.md")


def _write_user_skill(slug: str, body: str = "# 我的技能") -> None:
    d = os.path.join(skills_mod.SKILLS_DIR, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nname: {slug}\ndescription: 用户自建\nversion: 1.0.0\n---\n{body}\n")


# ----------------------------------------------------------------------------
# 1. 注册与落盘
# ----------------------------------------------------------------------------
def test_default_pack_installed_on_first_run(env):
    """首次启动把整包默认技能落到技能目录，且都带 default/auto_load 标记。"""
    rows = skills_mod.list_skills()
    slugs = {s["slug"] for s in rows}
    assert set(bundled_mod.DEFAULT_SKILLS) <= slugs
    for row in rows:
        if row["slug"] in bundled_mod.DEFAULT_SKILLS:
            assert row["builtin"] is True
            assert row["default"] is True
            assert row["auto_load"] is True
            assert row["triggers"], f"{row['slug']} 缺触发词"
            assert row["bundle_version"] == bundled_mod.BUNDLE_VERSION


def test_install_default_skills_is_idempotent(env):
    """再跑一次：一个都不重写（避免每次启动都碰盘 + 覆盖用户对正文的微调）。"""
    assert skills_mod.install_default_skills() == []
    assert len(skills_mod.list_skills()) >= len(bundled_mod.DEFAULT_SKILLS)


def test_install_upgrades_older_bundle_version(env):
    """老版本（没有 bundle_version 的老内置技能）会被升级，补上触发词。"""
    path = _skill_dir("doc-writer")
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\nname: doc-writer\ndescription: 旧版\nbuiltin: true\n---\n# 旧正文\n")
    assert "bundle_version" not in open(path, encoding="utf-8").read()
    written = skills_mod.install_default_skills()
    assert written == ["doc-writer"]
    text = open(path, encoding="utf-8").read()
    assert "bundle_version" in text and "triggers:" in text


def test_install_never_overwrites_user_skill_with_same_slug(env):
    """用户自建的同名技能（没 builtin 标记）绝不被默认包覆盖 —— 数据安全红线。"""
    _write_user_skill("doc-writer", "# 我自己的写法")
    assert skills_mod.install_default_skills() == []
    text = open(_skill_dir("doc-writer"), encoding="utf-8").read()
    assert "我自己的写法" in text and "bundle_version" not in text


def test_parse_skill_md_normalizes_default_fields():
    """triggers 兼容 list / "a, b" / "a、b"；auto_load / priority / default 归一。"""
    md = ("---\nname: x\ndescription: d\nbuiltin: true\n"
          "triggers: 写周报，做表格、整理文件\nauto_load: true\npriority: 7\n"
          "bundle_version: 3\n---\n正文\n")
    meta = skills_mod.parse_skill_md(md)["meta"]
    assert meta["triggers"] == ["写周报", "做表格", "整理文件"]
    assert meta["auto_load"] is True
    assert meta["default"] is True
    assert meta["priority"] == 7 and meta["bundle_version"] == 3
    plain = skills_mod.parse_skill_md("---\nname: y\ndescription: d\n---\n正文\n")["meta"]
    assert plain["triggers"] == [] and plain["auto_load"] is False
    assert plain["default"] is False


# ----------------------------------------------------------------------------
# 2. 匹配与选择
# ----------------------------------------------------------------------------
def _items():
    return skills_mod.list_skills()


def test_select_hits_by_trigger(env):
    got = defaults_mod.select("帮我写一份周报", _items())
    assert got["skills"][0] == "doc-writer"
    assert "周报" in got["matched"]["doc-writer"]["triggers"]


def test_select_respects_min_score(env):
    """没有任何触发词命中 -> 不自动加载（宁缺勿滥，避免污染 system）。"""
    assert defaults_mod.select("今天天气怎么样", _items())["skills"] == []
    assert defaults_mod.select("", _items())["skills"] == []


def test_select_limit_and_determinism(env):
    text = "帮我把这个表格做个数据透视，并写一份报告附上代码审查意见"
    got = defaults_mod.select(text, _items(), limit=2)
    assert len(got["skills"]) == 2
    again = defaults_mod.select(text, _items(), limit=2)
    assert got["skills"] == again["skills"], "同样输入必须同样输出"
    assert defaults_mod.select(text, _items(), limit=0)["skills"] == []


def test_select_tie_breaks_on_priority(env):
    """同分取 priority 大的；priority 相同时按 slug 稳定排序。"""
    rows = [
        {"slug": "b-skill", "name": "B", "description": "", "priority": 5,
         "auto_load": True, "triggers": ["周报"]},
        {"slug": "a-skill", "name": "A", "description": "", "priority": 5,
         "auto_load": True, "triggers": ["周报"]},
        {"slug": "c-skill", "name": "C", "description": "", "priority": 9,
         "auto_load": True, "triggers": ["周报"]},
    ]
    got = defaults_mod.select("写周报", rows, limit=2)
    assert got["skills"] == ["c-skill", "a-skill"]


def test_select_skips_off_and_non_auto_load(env):
    """停用名单里的不加载；没写 auto_load 的用户技能不参与自动加载。"""
    _write_user_skill("my-own")
    text = "帮我写一份周报"
    assert defaults_mod.select(text, _items(), off=["doc-writer"])["skills"] == []
    # 用户技能虽然名字里带「周报」类词，但 auto_load=false -> 不进候选池
    got = defaults_mod.select("用 my-own 处理一下周报", _items())
    assert "my-own" not in got["skills"]


def test_user_skill_can_opt_in_auto_load(env):
    """用户技能在 frontmatter 写 auto_load: true + triggers 即可参与自动加载。"""
    d = os.path.join(skills_mod.SKILLS_DIR, "my-report")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: my-report\ndescription: 我的报表流程\n"
                "auto_load: true\ntriggers: [月报模板]\npriority: 99\n---\n# 流程\n")
    got = defaults_mod.select("按我的月报模板来", _items())
    assert got["skills"][0] == "my-report"


# ----------------------------------------------------------------------------
# 3. 优先级与冲突（agent 层）
# ----------------------------------------------------------------------------
def _start(env, monkeypatch, text, **kw):
    provider = FakeProvider([ChatReply(content="好")])
    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: provider)
    ass = agent_mod.get_assistant()
    session = ass.new_session()
    events = []
    ass.start_turn(session["id"], text, emit=events.append, **kw)
    start = [e for e in events if e.get("type") == "start"][0]
    return provider, start


def test_auto_default_skill_injected(env, monkeypatch):
    """普通请求（无 profile、无挂载）→ 默认技能自动加载并注入执行要求。"""
    store_mod.save_config({"auto_load": True, "auto_skill_off": []})
    provider, start = _start(env, monkeypatch, "帮我写一份周报，说明本周进展")
    system = _system_of(provider)
    assert start["auto_loaded"]["source"] == "default"
    assert start["auto_loaded"]["skills"] == ["doc-writer"]
    assert "本轮自动加载的默认技能" in system
    assert "doc-writer" in system
    assert "技能要点（doc-writer）" in system
    # 别再把整份清单塞进去（会诱导模型 skill_list 空转）
    assert "## 可用技能" not in system
    assert isinstance(start["auto_loaded"]["tools"], int)


def test_mounted_skills_win_over_auto_defaults(env, monkeypatch):
    """用户手动挂载 -> 一票否决：不加载默认技能，system 只写「本轮指定技能」。"""
    store_mod.save_config({"auto_load": True})
    provider, start = _start(env, monkeypatch, "帮我写一份周报",
                             mounted_skills=["repo-digest"])
    system = _system_of(provider)
    assert start["auto_loaded"] == {}
    assert "本轮指定技能" in system and "repo-digest" in system
    assert "本轮自动加载的默认技能" not in system


def test_profile_wins_over_auto_defaults(env, monkeypatch):
    """命中 profile（文件整理）-> 用 profile 的技能与工具白名单，不叠加默认技能。"""
    store_mod.save_config({"auto_load": True})
    provider, start = _start(env, monkeypatch, "帮我把下载目录整理一下")
    system = _system_of(provider)
    assert start["auto_loaded"]["source"] == "profile"
    assert start["auto_loaded"]["profile"] == "file-organize"
    assert "本轮自动加载的默认技能" not in system


def test_auto_load_false_disables_defaults(env, monkeypatch):
    """总开关 auto_load=false -> profile 与默认技能都不加载（老行为不变）。"""
    store_mod.save_config({"auto_load": False})
    provider, start = _start(env, monkeypatch, "帮我写一份周报")
    assert start["auto_loaded"] == {}
    system = _system_of(provider)
    assert "本轮自动加载的默认技能" not in system
    assert "可用技能" not in system


def test_auto_skill_off_disables_one_skill(env, monkeypatch):
    """只想停掉某一个默认技能：加进 auto_skill_off，其它照常。"""
    store_mod.save_config({"auto_load": True, "auto_skill_off": ["doc-writer"]})
    provider, start = _start(env, monkeypatch, "帮我写一份周报")
    assert start["auto_loaded"] == {}
    assert "本轮自动加载的默认技能" not in _system_of(provider)


def test_auto_skill_limit_caps_count(env, monkeypatch):
    """auto_skill_limit=1 -> 最多一个默认技能（防止小模型被多套指令带跑偏）。"""
    store_mod.save_config({"auto_load": True, "auto_skill_limit": 1})
    provider, start = _start(
        env, monkeypatch, "把这个表格做数据透视，再写一份周报和一份方案")
    assert len(start["auto_loaded"]["skills"]) == 1
    assert "本轮自动加载的默认技能" in _system_of(provider)


def test_disabled_tool_not_silently_expected(env, monkeypatch):
    """技能建议的工具被用户禁用 -> system 明说，避免模型反复尝试不存在的工具。"""
    store_mod.save_config({"auto_load": True,
                           "tool_prefs": {"disabled": ["fs_write"], "max_tools": 24}})
    provider, _start_ev = _start(env, monkeypatch, "帮我写一份周报")
    system = _system_of(provider)
    assert "已被用户禁用" in system and "fs_write" in system


def test_auto_defaults_never_narrow_tools(env, monkeypatch):
    """默认技能只提前工具顺序，不裁剪下发集合（确保任务不因缺工具失败）。"""
    store_mod.save_config({"auto_load": True})
    provider, _ = _start(env, monkeypatch, "帮我写一份周报")
    names = {(s.get("function") or {}).get("name")
             for s in (provider.calls[-1]["tools"] or [])}
    assert {"fs_read", "fs_write", "shell_run", "skill_run"} <= names
    assert len(names) >= 12


# ----------------------------------------------------------------------------
# 4. 默认工具
# ----------------------------------------------------------------------------
def test_builtin_tools_marked_default(env):
    """内置工具都带 meta.default / meta.group，UI 据此打「默认」标。"""
    from core.assistant.tools import ToolRegistry
    rows = ToolRegistry(str(env["workspace"])).list()
    builtin = [r for r in rows if (r.get("source") or "builtin") == "builtin"]
    assert builtin, "内置工具不能为空"
    for row in builtin:
        assert row["meta"].get("default") is True, row["name"]
        assert row["meta"].get("group") in ("文件", "命令", "网络", "技能"), row["name"]


def test_prefer_orders_schemas_but_keeps_all(env):
    """prefer 把技能建议的工具排到最前；总数不变（只调顺序）。"""
    from core.assistant.tools import ToolRegistry
    reg = ToolRegistry(str(env["workspace"]))
    base = reg.select_schemas()
    preferred = reg.select_schemas(prefer=["shell_run"])
    assert len(base) == len(preferred)
    first = (preferred[0].get("function") or {}).get("name")
    assert first == "shell_run"


def test_prefer_never_revives_disabled_tool(env):
    """prefer 与用户禁用冲突时，禁用优先（安全红线）。"""
    from core.assistant.tools import ToolRegistry
    reg = ToolRegistry(str(env["workspace"]))
    got = reg.select_schemas(disabled={"shell_run"}, prefer=["shell_run"])
    names = {(s.get("function") or {}).get("name") for s in got}
    assert "shell_run" not in names


# ----------------------------------------------------------------------------
# 5. 配置接缝
# ----------------------------------------------------------------------------
def test_profiles_payload_exposes_auto_skill_fields(env):
    from routers import assistant as api
    import asyncio
    body = asyncio.run(api.get_profiles())
    data = body["data"]
    assert data["auto_load"] is True
    assert data["auto_skill_off"] == []
    assert data["auto_skill_limit"] == 2


def test_put_profiles_validates_auto_skill_fields(env):
    from routers import assistant as api
    import asyncio
    assert asyncio.run(api.put_profiles({"auto_skill_off": "doc"}))["code"] == 2002
    assert asyncio.run(api.put_profiles({"auto_skill_limit": "x"}))["code"] == 2002
    body = asyncio.run(api.put_profiles({"auto_skill_off": ["doc-writer"],
                                         "auto_skill_limit": 9}))
    assert body["code"] == 0
    cfg = store_mod.load_config()
    assert cfg["auto_skill_off"] == ["doc-writer"]
    assert cfg["auto_skill_limit"] == 4, "上限必须被 clamp 到 4"
