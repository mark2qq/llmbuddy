# -*- coding: utf-8 -*-
"""
并行计划断点持久化（core/assistant/parallel_store.py）。

职责：把「并行多任务」的编排状态落盘，让任务被中断（进程退出 / 断电 /
取消）之后，还能从**未完成的节点**续跑——包括换模型后续跑。

存储布局：

    assistant/parallel/<plan_id>.json
    {
      "id": "p" + uuid12,
      "goal": "把 E:/资料 整理成报告",
      "title": "资料整理",
      "workspace": "E:/资料",
      "provider": "local", "model": "qwen3-4b",
      "status": "running",          # planning/running/done/failed/partial/cancelled
      "max_workers": 4, "max_retries": 1, "subtask_timeout": 300,
      "created_at": 0, "updated_at": 0,
      "subtasks": [
        {"id": "t1", "title": "扫描目录", "prompt": "...",
         "status": "done",           # pending/running/done/failed/cancelled
         "attempts": 1, "output": "...", "error": "",
         "session_id": "s...",       # 续跑靠它：历史消息还在，能接着聊
         "provider": "local", "model": "qwen3-4b",
         "started_at": 0, "finished_at": 0}
      ],
      "summary": "...", "error": "", "events": [ {...} ]
    }

设计要点（对齐 core/assistant/store.py 的会话持久化约定）：
    - 原子写：.tmp + os.replace（断电不会写坏半个文件）
    - 模块级 RLock：多个子任务线程会同时更新同一个计划文件
    - 状态机：planning → running → done / failed / partial / cancelled
    - running 的子任务在下次 resume 时视为「未完成」→ 重跑；重跑复用同一
      session_id，所以模型能看到上次的对话历史，从断点继续而不是从头再来
    - 断点文件与子任务会话文件分离：会话文件存「聊到哪」，断点文件存
      「哪些子任务还没干完」，两者合起来才是完整的续跑依据
"""
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from core.config import user_data_dir

_LOCK = threading.RLock()

PARALLEL_DIR = os.path.join(user_data_dir(), "assistant", "parallel")

#: 单个计划最多保留多少条事件（环形裁剪，避免文件无限膨胀）
MAX_EVENTS = 200


def ensure_dirs() -> None:
    try:
        os.makedirs(PARALLEL_DIR, exist_ok=True)
    except Exception:
        pass


def plan_path(plan_id: str) -> str:
    """计划文件路径（plan_id 只允许安全字符，防目录穿越）。"""
    safe = "".join(c for c in (plan_id or "") if c.isalnum() or c in "-_")
    return os.path.join(PARALLEL_DIR, f"{safe}.json")


def save_plan(plan: Dict[str, Any]) -> None:
    """原子落盘一个计划快照（失败不抛，调用方不因存档问题中断执行）。"""
    if not isinstance(plan, dict):
        return
    pid = str(plan.get("id") or "")
    if not pid:
        return
    ensure_dirs()
    plan = dict(plan)
    plan["updated_at"] = time.time()
    events = plan.get("events")
    if isinstance(events, list) and len(events) > MAX_EVENTS:
        plan["events"] = events[-MAX_EVENTS:]
    path = plan_path(pid)
    try:
        with _LOCK:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(plan, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
    except Exception:
        pass


def load_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    """读取计划快照；不存在或损坏返回 None（不抛）。"""
    path = plan_path(plan_id)
    try:
        with _LOCK:
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def list_plans(limit: int = 50) -> List[Dict[str, Any]]:
    """列出计划摘要（不含 subtasks 明细，按更新时间倒序）。"""
    ensure_dirs()
    out: List[Dict[str, Any]] = []
    try:
        names = [f for f in os.listdir(PARALLEL_DIR) if f.endswith(".json")]
    except Exception:
        return []
    for fn in names:
        data = load_plan(fn[:-5])
        if not data:
            continue
        tasks = data.get("subtasks") or []
        out.append({
            "id": data.get("id"),
            "title": data.get("title") or data.get("goal") or "并行任务",
            "goal": data.get("goal") or "",
            "status": data.get("status") or "unknown",
            "provider": data.get("provider") or "",
            "model": data.get("model") or "",
            "task_count": len(tasks),
            "done_count": sum(1 for t in tasks if t.get("status") == "done"),
            "failed_count": sum(1 for t in tasks
                                if t.get("status") in ("failed", "cancelled")),
            "created_at": data.get("created_at") or 0,
            "updated_at": data.get("updated_at") or 0,
        })
    out.sort(key=lambda r: r.get("updated_at", 0), reverse=True)
    return out[:max(1, int(limit or 50))]


def delete_plan(plan_id: str) -> bool:
    """删除计划断点文件（子任务会话文件由调用方决定是否一并清理）。"""
    path = plan_path(plan_id)
    try:
        with _LOCK:
            if os.path.exists(path):
                os.remove(path)
                return True
    except Exception:
        pass
    return False
