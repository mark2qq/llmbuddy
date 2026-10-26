# -*- coding: utf-8 -*-
"""
助理配置与目录（core/assistant/store.py）。

所有状态都是纯 JSON 文件，落在 %LOCALAPPDATA%\\llmBuddy\\assistant 下：

    assistant/
        config.json          助手配置（审批策略/提供商/上限）
        skills/<slug>/SKILL.md   技能包（与 OpenClaw / Claude Skill 同构）
        sessions/<id>.json   会话记录
        audit/audit-YYYYMMDD.jsonl  审计留痕（谁在何时批准了什么）
        workspace/           默认工作区（文件工具的沙箱根）

设计约束：
    - 打包态安装目录只读，必须写用户目录；开发态也统一走用户目录，
      避免开发机上跑出来的会话/技能混进 git。
    - 所有读写加锁，uvicorn 多线程下 config.json 不会被写坏。
"""
import os
import json
import time
import shutil
import threading
from typing import Any, Dict, List, Optional

from core.config import user_data_dir

_LOCK = threading.RLock()

ASSISTANT_DIR = os.path.join(user_data_dir(), "assistant")
SKILLS_DIR = os.path.join(ASSISTANT_DIR, "skills")
SESSIONS_DIR = os.path.join(ASSISTANT_DIR, "sessions")
AUDIT_DIR = os.path.join(ASSISTANT_DIR, "audit")
CONFIG_PATH = os.path.join(ASSISTANT_DIR, "config.json")
DEFAULT_WORKSPACE = os.path.join(ASSISTANT_DIR, "workspace")
#: 在线目录缓存目录（1.0.8 增量：mcp_servers.json / gh_repos.json / gh_skills.json）
CATALOG_DIR = os.path.join(ASSISTANT_DIR, "catalog")

# 硅基流动 OpenAI 兼容端点（与客户端 chat_page.SILICONFLOW_BASE_URL 保持一致）
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

# 审批策略：
#   readonly_auto  只读动作直接放行，写入/删除/命令/外发需审批（默认，安全与体验平衡）
#   ask_all        所有工具调用都要审批（最严）
#   yolo           全部放行，仅审计（高风险，适合完全信任的沙箱）
POLICIES = ("readonly_auto", "ask_all", "yolo")
DEFAULT_POLICY = "readonly_auto"

#: 下发给模型的工具数上限（PRD §4.1 P0-5：8~64，默认 24）
DEFAULT_MAX_TOOLS = 24
MIN_MAX_TOOLS = 8
MAX_MAX_TOOLS = 64


def _default_profiles() -> List[Dict[str, Any]]:
    """3 个内置 profile 的**拷贝**（PRD §5.4.2）。

    刻意不走 profiles.BUILTIN_PROFILES 的引用：store 是最底层的模块，
    import profiles 会形成循环依赖；两份常量的一致性由 profiles 的兜底逻辑
    （BUILTIN_PROFILES = _default_profiles()）保证，只抄不改。
    """
    return [
        {"id": "file-organize", "name": "文件整理", "enabled": True,
         "priority": 10,
         "triggers": ["整理", "归档", "分类", "收拾", "重命名", "移走",
                      "清理目录"],
         "skills": ["workspace-organizer"],
         "tools": ["fs_list", "fs_read", "fs_info", "fs_mkdir", "fs_move",
                   "fs_glob", "skill_run"]},
        {"id": "code-read", "name": "代码阅读", "enabled": True,
         "priority": 20,
         "triggers": ["代码", "仓库", "项目", "分析一下", "技术栈", "架构",
                      "体检", "review"],
         "skills": ["repo-digest"],
         "tools": ["fs_list", "fs_glob", "fs_grep", "fs_read", "fs_info",
                   "skill_run"]},
        {"id": "web-research", "name": "网页调研", "enabled": True,
         "priority": 30,
         "triggers": ["网页", "链接", "文章", "抓取", "调研", "总结一下这",
                      "http", "url"],
         "skills": ["web-brief"],
         "tools": ["web_fetch", "fs_write", "skill_run"]},
    ]


def _default_catalog() -> Dict[str, Any]:
    """在线目录配置（PRD §6.1）：proxy 空 = 读环境变量，再空 = 本机默认代理。"""
    return {
        "proxy": "",
        "ttl_hours": {"mcp": 24, "gh_repos": 24, "gh_skills": 168},
        "allow_nonfree": False,
    }


def _default_config() -> Dict[str, Any]:
    return {
        "policy": DEFAULT_POLICY,
        # 默认提供商：本地引擎（复用 llmBuddy 已加载模型，零配置可用）
        "active_provider": "local",
        "providers": [
            {"id": "local", "label": "本地模型", "kind": "local", "enabled": True,
             "base_url": "", "api_key": "", "model": ""},
            {"id": "siliconflow", "label": "硅基流动", "kind": "openai", "enabled": True,
             "base_url": SILICONFLOW_BASE_URL, "api_key": "",
             "model": "Qwen/Qwen3-8B"},
            {"id": "custom", "label": "自定义（OpenAI 兼容）", "kind": "openai",
             "enabled": False, "base_url": "", "api_key": "", "model": ""},
        ],
        "workspace": DEFAULT_WORKSPACE,
        "max_steps": 12,             # 技能编排的计划步数上限（orchestrator）
        # 单轮对话「模型→工具→模型」循环轮次上限：**0 = 不限制**（默认）。
        # 多步任务（列目录→取文件→安装→验证）常超十几轮；兜底靠 900s 墙钟 +
        # 用户停止。要恢复上限就改成正整数（如 12）。
        "max_tool_rounds": 0,
        "skill_timeout": 240,        # 单个技能的整体时限（秒），超时强制收尾
        "approval_timeout": 300,     # 审批等待上限（秒），超时按拒绝处理
        "temperature": 0.3,
        "max_tokens": 2048,
        # MCP 服务器列表由 MCPManager 负责读写。这里必须声明 key，否则
        # load_config() 的 `if k in cfg` 过滤会把它丢掉（1.0.8 修复：
        # mcp_add / mcp_install 写盘后 servers() 读回空列表）。
        "mcp_servers": [],
        # ---- 1.0.8 增量：清单中心 / 工具裁剪 / 自动加载 ----
        # 注意：load_config() 只回填 _default_config() 里存在的 key，
        # 这 5 个新字段必须在这里，否则老 config.json 读不到（架构 §1.1 D7）。
        "tool_prefs": {"disabled": [], "max_tools": DEFAULT_MAX_TOOLS},
        "auto_load": True,
        "profiles": _default_profiles(),
        "catalog": _default_catalog(),
        "skill_sources": {},         # slug -> {kind, repo, ref, dir, commit, ...}
    }


def ensure_dirs() -> None:
    """创建全部工作目录（幂等）。"""
    for d in (ASSISTANT_DIR, SKILLS_DIR, SESSIONS_DIR, AUDIT_DIR, DEFAULT_WORKSPACE,
              CATALOG_DIR):
        os.makedirs(d, exist_ok=True)


def load_config() -> Dict[str, Any]:
    """读取配置并与默认值合并（缺字段补默认，未知字段保留）。"""
    ensure_dirs()
    cfg = _default_config()
    with _LOCK:
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # providers 按 id 合并：老配置缺新内置提供商时自动补上
                    saved = {p.get("id"): p for p in (data.get("providers") or [])
                             if isinstance(p, dict)}
                    merged = []
                    for p in cfg["providers"]:
                        base = dict(p)
                        if p["id"] in saved:
                            base.update({k: v for k, v in saved[p["id"]].items()
                                         if k in base})
                        merged.append(base)
                    for pid, p in saved.items():
                        if pid not in {m["id"] for m in merged}:
                            merged.append(p)
                    data["providers"] = merged
                    cfg.update({k: v for k, v in data.items() if k in cfg})
            except Exception:
                pass
    if cfg.get("policy") not in POLICIES:
        cfg["policy"] = DEFAULT_POLICY
    ws = cfg.get("workspace") or DEFAULT_WORKSPACE
    if not ws:
        ws = DEFAULT_WORKSPACE
    cfg["workspace"] = ws
    # 工作区可能落在「拔掉的移动盘 / 断开的网络位置 / 无权限目录」上。
    # 这里绝不能抛：load_config 被 state / settings / 对话全链路调用，一抛就是
    # 整个助理模块 500 —— 客户端看到的是「后端未就绪」且工作区显示成空（用户
    # 反馈的「重开页面接不上上次工作区」现象之一）。改为只记录错误、继续可用。
    try:
        os.makedirs(ws, exist_ok=True)
    except OSError as e:
        cfg["workspace_error"] = f"工作区不可用：{ws}（{e}）"
    return cfg


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """持久化配置（自动补默认字段；原子写，先写临时文件再替换）。"""
    ensure_dirs()
    cur = load_config()
    cur.update(cfg or {})
    if cur.get("policy") not in POLICIES:
        cur["policy"] = DEFAULT_POLICY
    # workspace_error 是运行期探测结果，不写进 config.json
    cur.pop("workspace_error", None)
    with _LOCK:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return cur


def get_provider(cfg: Optional[Dict[str, Any]] = None,
                 provider_id: str = "") -> Dict[str, Any]:
    """按 id 取提供商配置；未指定则取 active_provider。"""
    cfg = cfg or load_config()
    pid = provider_id or cfg.get("active_provider") or "local"
    for p in cfg.get("providers") or []:
        if p.get("id") == pid:
            return p
    return (cfg.get("providers") or [_default_config()["providers"][0]])[0]


# ----------------------------------------------------------------------------
# 审计留痕
# ----------------------------------------------------------------------------
def audit(event: str, **fields: Any) -> None:
    """追加一条审计记录（按天分文件，jsonl，失败不抛）。"""
    try:
        ensure_dirs()
        day = time.strftime("%Y%m%d")
        path = os.path.join(AUDIT_DIR, f"audit-{day}.jsonl")
        rec = {"ts": time.time(), "time": time.strftime("%Y-%m-%d %H:%M:%S"),
               "event": event}
        rec.update(fields)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_audit(limit: int = 200) -> List[Dict[str, Any]]:
    """读取最近审计记录（跨天合并，按时间倒序）。"""
    ensure_dirs()
    out: List[Dict[str, Any]] = []
    try:
        files = sorted((f for f in os.listdir(AUDIT_DIR) if f.endswith(".jsonl")),
                       reverse=True)[:7]
        for fn in files:
            with open(os.path.join(AUDIT_DIR, fn), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        return []
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------
# 任务（会话）与空间（工作区）
# ----------------------------------------------------------------------------
# 对外一律叫「任务」，内部仍沿用 session 命名（会话文件、API 路径都不变），
# 便于与 WorkBuddy 的「任务 / 空间」对齐而不改动既有数据。
SESSION_PATCHABLE = ("title", "workspace", "pinned", "archived")


def norm_workspace(path: Any) -> str:
    """工作区路径归一化：分隔符统一为 /、去尾斜杠、盘符大写。

    同一个目录可能以 E:\\x、E:/x、e:/x/ 三种形式出现，不归一化就会被拆成
    三个「空间」，左栏会出现三条同名目录。
    """
    p = str(path or "").strip().replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    p = p.rstrip("/")
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def space_name(path: str) -> str:
    """空间显示名：目录名（C:/ 这种根目录直接显示盘符）。"""
    p = norm_workspace(path)
    name = p.rsplit("/", 1)[-1]
    return name or p


def _session_path(sid: str) -> str:
    safe = "".join(c for c in (sid or "") if c.isalnum() or c in "-_")
    return os.path.join(SESSIONS_DIR, f"{safe}.json")


def save_session(session: Dict[str, Any]) -> None:
    ensure_dirs()
    sid = session.get("id")
    if not sid:
        return
    with _LOCK:
        tmp = _session_path(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _session_path(sid))


def load_session(sid: str) -> Optional[Dict[str, Any]]:
    path = _session_path(sid)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _session_summary(s: Dict[str, Any]) -> Dict[str, Any]:
    """列表用的任务摘要（不含 messages，体量可控）。"""
    msgs = s.get("messages") or []
    preview = ""
    for m in reversed(msgs):
        if m.get("role") in ("user", "assistant"):
            preview = (m.get("content") or "").strip().replace("\n", " ")
            if preview:
                break
    return {
        "id": s.get("id"),
        "title": s.get("title") or "新任务",
        "created_at": s.get("created_at") or 0,
        "updated_at": s.get("updated_at") or 0,
        "message_count": len(msgs),
        "workspace": norm_workspace(s.get("workspace")),
        "pinned": bool(s.get("pinned")),
        "archived": bool(s.get("archived")),
        "preview": preview[:60],
    }


def list_sessions(limit: int = 50, include_archived: bool = False,
                  workspace: str = "") -> List[Dict[str, Any]]:
    """任务摘要列表：置顶优先，其余按最近活动倒序（归档默认不出现）。"""
    ensure_dirs()
    want_ws = norm_workspace(workspace)
    items: List[Dict[str, Any]] = []
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                s = json.load(f)
            if not isinstance(s, dict):
                continue
            row = _session_summary(s)
            if row.get("archived") and not include_archived:
                continue
            if want_ws and row.get("workspace") != want_ws:
                continue
            items.append(row)
        except Exception:
            continue
    # 置顶的永远在最前（WorkBuddy 的「置顶」语义），组内再按最近活动排
    items.sort(key=lambda x: (not x.get("pinned"), -(x.get("updated_at") or 0)))
    return items[:limit]


def space_list(sessions: Optional[List[Dict[str, Any]]] = None,
               fallback: str = "") -> List[Dict[str, Any]]:
    """把任务按所属空间（工作区目录）分组，供左栏「空间」区渲染。

    sessions 为空时自己读一次；fallback 是当前工作区，历史任务没有记录
    workspace 字段（1.0.8 之前建的）就归到它名下，避免出现「无归属」空组。
    """
    rows = list_sessions(500, False) if sessions is None else sessions
    fb = norm_workspace(fallback)
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for s in rows:
        ws = norm_workspace(s.get("workspace")) or fb or "（未归属）"
        # 归位后的空间写回摘要：前端/外部 API 拿到的任务都带明确归属，
        # 不必各自再实现一遍 fallback。
        s = dict(s)
        s["workspace"] = ws
        groups.setdefault(ws, []).append(s)
    out: List[Dict[str, Any]] = []
    for ws, tasks in groups.items():
        tasks.sort(key=lambda x: (not x.get("pinned"), -(x.get("updated_at") or 0)))
        out.append({
            "path": ws,
            "name": space_name(ws),
            "exists": ws == "（未归属）" or os.path.isdir(ws),
            "count": len(tasks),
            "pinned": sum(1 for t in tasks if t.get("pinned")),
            "updated_at": max((t.get("updated_at") or 0) for t in tasks),
            "tasks": tasks,
        })
    out.sort(key=lambda g: -(g.get("updated_at") or 0))
    return out


def update_session(sid: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """改任务属性（标题 / 所属空间 / 置顶 / 归档），返回更新后的整条会话。

    刻意不动 updated_at：重命名或置顶如果刷新了「最近活动」时间，
    任务会莫名跳到列表最前面，与 WorkBuddy 的行为不一致。
    """
    s = load_session(sid)
    if s is None:
        return None
    for key in SESSION_PATCHABLE:
        if key not in (patch or {}):
            continue
        val = patch[key]
        if key == "title":
            val = str(val or "").strip()[:80] or s.get("title") or "新任务"
        elif key == "workspace":
            val = norm_workspace(val)
        else:
            val = bool(val)
        s[key] = val
    save_session(s)
    return s


def delete_session(sid: str) -> bool:
    path = _session_path(sid)
    try:
        if os.path.exists(path):
            os.remove(path)
            return True
    except Exception:
        pass
    return False


def reset_all() -> None:
    """清空助手数据（技能/会话/审计/配置），仅供测试与「恢复默认」使用。"""
    with _LOCK:
        if os.path.isdir(ASSISTANT_DIR):
            shutil.rmtree(ASSISTANT_DIR, ignore_errors=True)
    ensure_dirs()
