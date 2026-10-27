# -*- coding: utf-8 -*-
"""
一轮对话的阶段闸门（turn_phase.py）。

用户需求（2026-10-27）：
    「后端执行必须严格串行：思考 → 执行 → 如有问题再思考 → 再执行，
      不得思考与工具/技能调用同时发生。」

为什么需要单独的模块：
    Agent 的循环本身是顺序的（一次 `provider.chat` 完整流完，才执行工具），
    但**事件流**没有显式的阶段边界，前端只能靠「收到 tool_call 就当执行开始」
    去猜。一旦 provider 的流式实现在后台线程里把尾巴 token 继续推给
    `on_delta`（chat() 已经返回、工具已经在跑），前端就会看到「思考还在长、
    工具同时在跑」——这正是用户截图里那种错位。

    PhaseGate 做两件事：
        1. 明确阶段边界：`thinking` / `executing` 两个阶段各发一次
           `{"type": "phase", "phase": ..., "round": n}` 事件，前端按阶段
           分组渲染，不再猜；
        2. 硬闸门：执行阶段到达的 `delta` / `reasoning` 一律**缓存**，
           等回到思考阶段时按原顺序补发 —— 思考与执行在事件流上不可能重叠，
           而且内容一个 token 都不丢。

    缓存正常永远是空的（同步 provider 不会在 chat() 返回后回调）；留着它是
    为了兜住异步流式实现，宁可晚一点显示，也不能让两段内容交叉。
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List

#: 阶段常量（与前端 `TASK_PHASE_*` 一一对应）
THINKING = "thinking"      # 模型在思考 / 写正文
EXECUTING = "executing"    # 工具、技能、命令在执行

#: 只有「模型在说话」阶段才允许直接放行的流式事件
_MODEL_EVENTS = ("delta", "reasoning")


class PhaseGate:
    """阶段闸门：包装 emit，保证思考与执行互斥。

    用法::

        gate = PhaseGate(emit)
        emit = gate.emit                      # 之后所有事件都过闸门
        emit({"type": "delta", ...})           # 思考阶段：直通
        gate.enter_executing(round_no=1)       # 发 phase=executing
        emit({"type": "delta", ...})           # 执行阶段：进缓存，不放行
        gate.enter_thinking(round_no=2)        # 先补发缓存，再发 phase=thinking
    """

    def __init__(self, emit: Callable[[Dict[str, Any]], None],
                 round_no: int = 0):
        self._emit = emit
        self._phase = THINKING
        self._round = int(round_no or 0)
        self._buf: List[Dict[str, Any]] = []
        # 工具在 ThreadPoolExecutor 里并发执行、provider 可能在别的线程回调
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # 查询（测试与诊断用）
    # ------------------------------------------------------------------ #
    @property
    def phase(self) -> str:
        with self._lock:
            return self._phase

    @property
    def round(self) -> int:
        with self._lock:
            return self._round

    @property
    def buffered(self) -> int:
        """被闸门暂存的模型事件条数（正常应恒为 0）。"""
        with self._lock:
            return len(self._buf)

    # ------------------------------------------------------------------ #
    # 事件出口
    # ------------------------------------------------------------------ #
    def emit(self, ev: Dict[str, Any]) -> None:
        if not isinstance(ev, dict):
            self._emit(ev)                      # type: ignore[arg-type]
            return
        kind = ev.get("type")
        with self._lock:
            if kind == "phase":
                # 阶段只能由 enter_* 发，避免调用方塞进互相矛盾的阶段
                return
            if self._phase == EXECUTING and kind in _MODEL_EVENTS:
                self._buf.append(dict(ev))
                return
        self._emit(ev)

    def enter_executing(self, round_no: int = 0) -> None:
        """进入执行阶段（工具/技能）。

        先把思考阶段的尾巴放出去（正常没有），再宣布阶段切换，
        之后到达的模型事件全部缓存。
        """
        with self._lock:
            new_round = int(round_no) if round_no else self._round
            # 阶段或轮次变了就发事件：首个阶段（round 0 → 1，同为 thinking）
            # 也要发，前端才知道「新一轮思考从这一行开始」。
            changed = (self._phase != EXECUTING) or (new_round != self._round)
            self._phase = EXECUTING
            self._round = new_round
            pending = self._take()
            round_now = self._round
        for ev in pending:
            self._emit(ev)
        if changed:
            self._emit({"type": "phase", "phase": EXECUTING,
                        "round": round_now})

    def enter_thinking(self, round_no: int = 0) -> None:
        """回到思考阶段：缓存先补发（保持原顺序），再宣布阶段切换。"""
        with self._lock:
            new_round = int(round_no) if round_no else self._round
            changed = (self._phase != THINKING) or (new_round != self._round)
            self._phase = THINKING
            self._round = new_round
            pending = self._take()
            round_now = self._round
        for ev in pending:
            self._emit(ev)
        if changed:
            self._emit({"type": "phase", "phase": THINKING,
                        "round": round_now})

    def flush(self) -> None:
        """把缓存里的模型事件补发出去（轮次收尾用，保证不丢内容）。"""
        with self._lock:
            pending = self._take()
        for ev in pending:
            self._emit(ev)

    def _take(self) -> List[Dict[str, Any]]:
        """取走缓存（调用方已持锁）。"""
        if not self._buf:
            return []
        buf, self._buf = self._buf, []
        return buf


def install(emit: Callable[[Dict[str, Any]], None]) -> PhaseGate:
    """给一个 emit 套上闸门（agent.start_turn 用）。"""
    return PhaseGate(emit)
