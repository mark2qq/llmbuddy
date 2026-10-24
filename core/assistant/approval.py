# -*- coding: utf-8 -*-
"""
高危动作审批（core/assistant/approval.py）。

规则（policy 见 store.POLICIES）：
    readonly_auto   low 风险直接执行；medium/high 必须人工批准
    ask_all         任意工具调用都要批准
    yolo            全部放行，仅落审计（本机私有沙箱场景）

产物：
    - PendingApproval：一次挂起的审批，HTTP 层据此向前端推审批卡片
    - 批准可选「本会话内同类工具免审」（remember），降低重复打断
    - 所有批准/拒绝/超时都写审计（谁、何时、对什么动作、结果）

并发：Agent 循环跑在 fastapi 的线程池线程里，等审批用 threading.Event，
approve API 在另一线程 set()，不需要 asyncio 跨线程原语。
"""
import time
import uuid
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.assistant.store import audit

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}


@dataclass
class PendingApproval:
    """一次等待人工决策的工具调用。"""

    id: str
    session_id: str
    tool: str
    args: Dict[str, Any]
    risk: str
    reason: str = ""
    created_at: float = field(default_factory=time.time)
    status: str = "pending"           # pending / approved / denied / timeout
    note: str = ""
    remember: bool = False
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "session_id": self.session_id, "tool": self.tool,
            "args": self.args, "risk": self.risk, "reason": self.reason,
            "created_at": self.created_at, "status": self.status,
            "note": self.note,
        }


class ApprovalManager:
    """审批台：挂起 / 决策 / 会话级免审。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._pending: Dict[str, PendingApproval] = {}
        # session_id -> set(tool)：本会话已「记住允许」的工具
        self._session_allow: Dict[str, set] = {}
        # 新审批产生的回调（Agent 用它推 SSE 事件）
        self.on_created: Optional[Callable[[PendingApproval], None]] = None

    # ------------------------------------------------------------------ #
    # 策略判定
    # ------------------------------------------------------------------ #
    @staticmethod
    def needs_approval(policy: str, risk: str) -> bool:
        risk = (risk or "low").lower()
        if policy == "yolo":
            return False
        if policy == "ask_all":
            return True
        return RISK_ORDER.get(risk, 0) > RISK_ORDER["low"]

    def is_session_allowed(self, session_id: str, tool: str) -> bool:
        with self._lock:
            return tool in self._session_allow.get(session_id, set())

    def remember_for_session(self, session_id: str, tool: str) -> None:
        with self._lock:
            self._session_allow.setdefault(session_id, set()).add(tool)

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            self._session_allow.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # 挂起与决策
    # ------------------------------------------------------------------ #
    def request(self, session_id: str, tool: str, args: Dict[str, Any],
                risk: str, reason: str = "", policy: str = "readonly_auto",
                timeout: int = 300) -> PendingApproval:
        """创建审批并阻塞等待（最长 timeout 秒）。

        返回值始终是 PendingApproval，调用方看 status：
            approved 放行 / denied 拒绝 / timeout 超时（按拒绝处理）
        """
        if not self.needs_approval(policy, risk) or self.is_session_allowed(session_id, tool):
            p = PendingApproval(id=uuid.uuid4().hex[:12], session_id=session_id,
                                tool=tool, args=args, risk=risk, reason=reason,
                                status="approved")
            return p

        p = PendingApproval(id=uuid.uuid4().hex[:12], session_id=session_id,
                            tool=tool, args=args, risk=risk, reason=reason)
        with self._lock:
            self._pending[p.id] = p
        if self.on_created:
            try:
                self.on_created(p)
            except Exception:
                pass
        try:
            p._event.wait(timeout=timeout)
        finally:
            with self._lock:
                self._pending.pop(p.id, None)
        if p.status == "pending":
            p.status = "timeout"
            audit("approval_timeout", session_id=session_id, tool=tool,
                  approval_id=p.id, args=args)
        elif p.status == "approved" and p.remember:
            self.remember_for_session(session_id, tool)
        return p

    def resolve(self, approval_id: str, approve: bool, remember: bool = False,
                note: str = "") -> Optional[PendingApproval]:
        """人工决策入口（HTTP 调用）。返回更新后的对象，找不到返回 None。"""
        with self._lock:
            p = self._pending.get(approval_id)
            if p is None:
                return None
            if p.status != "pending":
                return p
            p.status = "approved" if approve else "denied"
            p.remember = bool(remember)
            p.note = note or ""
            audit("approval_resolved",
                  approval_id=p.id, session_id=p.session_id, tool=p.tool,
                  approved=bool(approve), remember=bool(remember),
                  note=p.note, args=p.args)
            p._event.set()
            return p

    def list_pending(self, session_id: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            return [p.to_dict() for p in self._pending.values()
                    if not session_id or p.session_id == session_id]

    def cancel_session(self, session_id: str) -> None:
        """会话结束时把仍在等待的审批判为拒绝，避免 Agent 线程挂死。"""
        with self._lock:
            targets = [p for p in self._pending.values()
                       if p.session_id == session_id and p.status == "pending"]
        for p in targets:
            self.resolve(p.id, approve=False, note="会话已结束")


_MANAGER: Optional[ApprovalManager] = None
_MANAGER_LOCK = threading.Lock()


def get_approval_manager() -> ApprovalManager:
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = ApprovalManager()
    return _MANAGER
