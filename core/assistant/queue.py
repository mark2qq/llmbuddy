# -*- coding: utf-8 -*-
"""
引导会话队列核心（core/assistant/queue.py）——1.0.9 增量。

职责（对齐 docs/智能助理-引导会话-架构设计.md 3.1）：
    QueueItem       一条排队 / 引导消息（不可变 id，FIFO 按 created_at）
    AssistantQueue  per-session deque + RLock + 暂停标志 + 已出队记忆
    QueueError      带业务错误码的队列异常（2012/2014/2015/2016）
    get_queue()     模块级单例（与 agent.get_assistant 同模式）
    hhmm()          epoch -> "HH:MM"（本机本地时区）

设计要点：
    - 生命周期 ≤ 一个忙碌期：纯内存队列，不落库（重启丢失，P2-3 持久化）
    - 引导消息（guided=True）出了队就写进会话 messages（由 start_turn 落库），
      因此在队列里即视为「已登记对话流」：不可改 / 删 / 排序，只能等出队
    - 已出队条目在 _recent 里记忆 RECENT_KEEP 条，用来区分
      「已出队（2015）」与「不存在（2014）」——编辑/删除慢半拍的竞态
    - 所有公共方法都持 RLock：路由层 REST（事件循环线程）与 gen() 的
      多轮消费循环（asyncio 主循环）会并发访问同一个单例
"""
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional

#: 单会话排队上限（主理人拍板；PRD 6.4：超出提示且不入队）
MAX_QUEUE_ITEMS = 20
#: 已出队条目记忆数（区分「已出队 2015」与「不存在 2014」）
RECENT_KEEP = 64

# ---- SSE 事件名（客户端 _on_event 用字面量字符串匹配，不 import 后端包） ----
EV_QUEUED = "queued"          # 入队确认（id, text, time, index）
EV_QUEUE = "queue"            # 队列全量快照（items[], paused）
EV_GUIDED = "guided"          # 引导消息已登记（id, text, time）
EV_DEQUEUED = "dequeued"      # 消息出队、即将作为下一轮（id, text, mode, time）
EV_QUEUE_STATE = "queue_state"  # 暂停/恢复（paused, reason?）

# ---- 业务错误码（{code, message, data}；HTTP 200 + fail()，唯一例外见下） ----
ERR_PARAM = 2002              # 参数不正确（空文本 / 排序越界 / 非法 delta）
ERR_QUEUE_FULL = 2012         # 队列满（MAX_QUEUE_ITEMS）
ERR_QUEUE_EMPTY = 2013        # 队列空（resume 无消息可出队；该路径 HTTP 400）
ERR_ITEM_NOT_FOUND = 2014     # 消息不存在
ERR_ITEM_DISPATCHED = 2015    # 消息已出队（编辑/删除/排序慢了半拍）
ERR_GUIDED_READONLY = 2016    # 引导消息只读（已登记进对话流，不可改/删/排序）
ERR_SESSION_BUSY = 2017       # 会话正在执行中（resume 撞上活跃流，竞态兜底）


def hhmm(ts: float) -> str:
    """epoch -> "HH:MM"（本机本地时区；非法输入给 "--:--" 不抛）。"""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "--:--"


@dataclass
class QueueItem:
    """一条排队 / 引导消息。

    guided=True 表示「引导档」：客户端会立即渲染用户气泡（带 HH:MM），
    但后端直到出队时才写入会话 messages（避免打乱「用户消息 → 助理回复」
    的顺序）；未消费的引导消息刷新后从对话流消失，仅队列可见（已知限制）。
    """

    id: str                 # "q" + uuid4().hex[:10]（对齐 session id 风格）
    text: str
    guided: bool            # True=引导（只读，已登记对话流）
    created_at: float       # epoch
    edited_at: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """快照字典：time 一律 HH:MM；epoch 字段仅供 P2 落库，UI 不直接渲染。"""
        return {
            "id": self.id,
            "text": self.text,
            "guided": bool(self.guided),
            "time": hhmm(self.created_at),
            "created_at": self.created_at,
            "edited_at": self.edited_at,
        }


class QueueError(Exception):
    """队列业务错误（code 见模块顶部常量；消息已中文化，可直接透出）。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)


class AssistantQueue:
    """per-session 引导会话队列：FIFO + 暂停标志 + 已出队记忆。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: Dict[str, Deque[QueueItem]] = {}
        self._paused: Dict[str, bool] = {}
        self._recent: Dict[str, "OrderedDict[str, QueueItem]"] = {}

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def snapshot(self, session_id: str) -> List[Dict[str, Any]]:
        """队列全量快照（按 FIFO）；客户端以此为单一事实来源重渲染。"""
        with self._lock:
            return [it.to_dict() for it in self._items.get(session_id, ())]

    def pending_count(self, session_id: str) -> int:
        """排队中的条数（含引导行）。"""
        with self._lock:
            return len(self._items.get(session_id, ()))

    def is_paused(self, session_id: str) -> bool:
        """停止后置暂停：不出队、不清空，等用户点「继续」（mode=resume）。"""
        with self._lock:
            return bool(self._paused.get(session_id))

    def set_paused(self, session_id: str, paused: bool) -> None:
        with self._lock:
            if paused:
                self._paused[session_id] = True
            else:
                self._paused.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # 入队
    # ------------------------------------------------------------------ #
    def enqueue(self, session_id: str, text: str,
                guided: bool = False) -> QueueItem:
        """入队一条消息；满 20 条抛 2012（消息保留在输入框，不丢）。"""
        text = (text or "").strip()
        if not text:
            raise QueueError(ERR_PARAM, "消息不能为空")
        with self._lock:
            q = self._items.setdefault(session_id, deque())
            if len(q) >= MAX_QUEUE_ITEMS:
                raise QueueError(ERR_QUEUE_FULL,
                                 f"排队过多（上限 {MAX_QUEUE_ITEMS} 条），"
                                 "建议等本轮结束再发")
            item = QueueItem(id="q" + uuid.uuid4().hex[:10], text=text,
                             guided=bool(guided), created_at=time.time())
            q.append(item)
            return item

    # ------------------------------------------------------------------ #
    # 修改（排队中的普通消息才允许；引导只读 2016；已出队 2015；不存在 2014）
    # ------------------------------------------------------------------ #
    def update(self, session_id: str, item_id: str, text: str) -> QueueItem:
        """行内编辑：文本更新并记 edited_at；保存结果等快照回来再渲染。"""
        text = (text or "").strip()
        if not text:
            raise QueueError(ERR_PARAM, "消息不能为空")
        with self._lock:
            item = self._find(session_id, item_id)
            if item.guided:
                raise QueueError(ERR_GUIDED_READONLY,
                                 "引导消息已登记进对话流，不能修改")
            item.text = text
            item.edited_at = time.time()
            return item

    def remove(self, session_id: str, item_id: str) -> str:
        """删除一条排队消息，返回被删的 id（引导不可删）。"""
        with self._lock:
            item = self._find(session_id, item_id)
            if item.guided:
                raise QueueError(ERR_GUIDED_READONLY,
                                 "引导消息已登记进对话流，不能删除")
            q = self._items[session_id]
            q.remove(item)
            return item.id

    def move(self, session_id: str, item_id: str,
             delta: int) -> List[Dict[str, Any]]:
        """上移/下移一位（delta ∈ {-1, +1}），返回移动后的全量快照。"""
        try:
            step = int(delta)
        except (TypeError, ValueError):
            raise QueueError(ERR_PARAM, "delta 只支持 -1 / +1")
        if step not in (-1, 1):
            raise QueueError(ERR_PARAM, "delta 只支持 -1 / +1")
        with self._lock:
            item = self._find(session_id, item_id)
            if item.guided:
                raise QueueError(ERR_GUIDED_READONLY, "引导消息只读，不能排序")
            q = self._items[session_id]
            idx = list(q).index(item)
            new_idx = idx + step
            if new_idx < 0 or new_idx >= len(q):
                raise QueueError(ERR_PARAM, "已经在队列边缘，无法继续移动")
            q.remove(item)
            q.insert(new_idx, item)
            return [it.to_dict() for it in q]

    def clear(self, session_id: str) -> int:
        """全部清除：只清排队中的普通消息，引导消息不清（已登记对话流，
        清了即丢失，违背「不丢失」目标）；返回实际清除条数。"""
        with self._lock:
            q = self._items.get(session_id)
            if not q:
                return 0
            keep = deque(it for it in q if it.guided)
            cleared = len(q) - len(keep)
            if keep:
                self._items[session_id] = keep
            else:
                self._items.pop(session_id, None)
            return cleared

    # ------------------------------------------------------------------ #
    # 出队（FIFO；严格按到达时间，引导不设优先权）
    # ------------------------------------------------------------------ #
    def pop_next(self, session_id: str) -> Optional[QueueItem]:
        """取下一条待处理消息并记入 _recent；空队列返回 None。"""
        with self._lock:
            q = self._items.get(session_id)
            if not q:
                return None
            item = q.popleft()
            if not q:
                self._items.pop(session_id, None)
            self._remember(session_id, item)
            return item

    # ------------------------------------------------------------------ #
    # 会话删除
    # ------------------------------------------------------------------ #
    def drop_session(self, session_id: str) -> None:
        """会话删除：清掉该会话全部队列状态（条目 / 暂停 / 已出队记忆）。"""
        with self._lock:
            self._items.pop(session_id, None)
            self._paused.pop(session_id, None)
            self._recent.pop(session_id, None)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _find(self, session_id: str, item_id: str) -> QueueItem:
        """按 id 找条目（调用方需已持锁）。

        不在队列里但出过队 -> 2015（竞态：这边编辑，那边已经开始处理）；
        从没见过 -> 2014。
        """
        q = self._items.get(session_id)
        if q:
            for it in q:
                if it.id == item_id:
                    return it
        if item_id in self._recent.get(session_id, ()):
            raise QueueError(ERR_ITEM_DISPATCHED, "消息已出队，不能再修改")
        raise QueueError(ERR_ITEM_NOT_FOUND, "消息不存在")

    def _remember(self, session_id: str, item: QueueItem) -> None:
        """记住已出队条目（超量按 FIFO 淘汰），供 2015/2014 区分。"""
        recent = self._recent.setdefault(session_id, OrderedDict())
        recent[item.id] = item
        recent.move_to_end(item.id)
        while len(recent) > RECENT_KEEP:
            recent.popitem(last=False)


_QUEUE: Optional[AssistantQueue] = None
_QUEUE_LOCK = threading.Lock()


def get_queue() -> AssistantQueue:
    """队列单例（与 agent.get_assistant 同模式；双检锁，多线程安全）。"""
    global _QUEUE
    if _QUEUE is None:
        with _QUEUE_LOCK:
            if _QUEUE is None:
                _QUEUE = AssistantQueue()
    return _QUEUE
