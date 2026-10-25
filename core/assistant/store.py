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

# 硅基流动 OpenAI 兼容端点（与客户端 chat_page.SILICONFLOW_BASE_URL 保持一致）
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

# 审批策略：
#   readonly_auto  只读动作直接放行，写入/删除/命令/外发需审批（默认，安全与体验平衡）
#   ask_all        所有工具调用都要审批（最严）
#   yolo           全部放行，仅审计（高风险，适合完全信任的沙箱）
POLICIES = ("readonly_auto", "ask_all", "yolo")
DEFAULT_POLICY = "readonly_auto"


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
        "max_steps": 12,             # 单轮最多工具调用轮次
        "skill_timeout": 240,        # 单个技能的整体时限（秒），超时强制收尾
        "approval_timeout": 300,     # 审批等待上限（秒），超时按拒绝处理
        "temperature": 0.3,
        "max_tokens": 2048,
    }


def ensure_dirs() -> None:
    """创建全部工作目录（幂等）。"""
    for d in (ASSISTANT_DIR, SKILLS_DIR, SESSIONS_DIR, AUDIT_DIR, DEFAULT_WORKSPACE):
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
# 会话
# ----------------------------------------------------------------------------
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


def list_sessions(limit: int = 50) -> List[Dict[str, Any]]:
    """会话摘要列表（按更新时间倒序）。"""
    ensure_dirs()
    items: List[Dict[str, Any]] = []
    for fn in os.listdir(SESSIONS_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as f:
                s = json.load(f)
            items.append({"id": s.get("id"), "title": s.get("title") or "新会话",
                          "updated_at": s.get("updated_at", 0),
                          "message_count": len(s.get("messages") or [])})
        except Exception:
            continue
    items.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
    return items[:limit]


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
