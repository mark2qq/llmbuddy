# -*- coding: utf-8 -*-
"""
并行多任务执行（core/assistant/parallel.py）。

定位：把「一个大任务」拆成若干**可独立执行**的子任务，并行跑完再聚合。
对齐 WorkBuddy 的多任务并行形态（主理人拆分 → 子任务并发 → 汇总），
但复用本仓库既有的抽象，不另起一套：

    - 子任务执行 **走 Agent.start_turn**（core/assistant/agent.py）：
      工具、审批、技能、上下文压缩、事件全部复用既有链路，
      每个子任务一个独立 session → 天然资源隔离（消息/审批互不串）
    - 计划拆分复用 orchestrator 的 JSON 解析器 extract_json_array
    - 断点落盘复用 parallel_store（原子写 + RLock，同 store.py 约定）
    - 取消复用 Agent.stop() 的协作式取消标志（provider 侧 cancel_check）
    - 审批 / 工具 / 技能的白名单与风险分级完全不动（安全红线）

状态机：

    plan:     planning → running → done / partial / failed / cancelled
    subtask:  pending → running → done / failed / cancelled

断点续跑（跨会话、跨模型）：
    1. 每个子任务开始前就把 session_id 写进断点文件 —— 中途断电也知道
       「这条子任务的对话落在哪个会话里」
    2. resume 时：done 的跳过；running（上次中断）与 failed 的重跑；
       重跑复用同一 session_id → 模型能看到上次的对话历史，从断点接着聊
    3. 换模型续跑：resume 可传新的 provider/model，只作用于**未完成**的
       子任务；已完成的结果原样保留（每个子任务各自记录当时的 provider/model）

并发与边界：
    - 并发上限 parallel_max_workers（默认 4，硬上限 8，与 agent.py 的
      MAX_PARALLEL=8 同量级；实际瓶颈通常在模型端，本地引擎并发由
      core/pool.py 的 max_concurrent 控制）
    - 单子任务超时 subtask_timeout（默认 300s）：超时 → Agent.stop()
      协作式取消 → 标 failed(timeout)，不会挂死线程
    - 重试上限 max_retries（默认 1）：只对可重试错误（限流/超时/网络）重试；
      用户拒绝审批、参数非法这类**重试也没用**的错误不重试
    - 取消竞态：cancel 与子任务完成同时发生时，已完成的结果保留，
      计划终态以 cancelled 优先（不把「已取消」改写成「已完成」）
"""
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from core.assistant import parallel_store
from core.assistant.orchestrator import extract_json_array
from core.assistant.providers import ProviderError, get_provider
#: store.get_provider 是「按 id 选提供商配置」，providers.get_provider 才是
#: 「按配置造客户端」——两个同名函数，按 agent.py 的既有别名方式区分
from core.assistant.store import audit, get_provider as cfg_provider, load_config

#: 默认并发子任务数（硬上限 8，与 agent.py 的 MAX_PARALLEL 同量级）
MAX_PARALLEL_WORKERS = 4
MAX_PARALLEL_WORKERS_LIMIT = 8
#: 单个计划最多拆多少个子任务
MAX_TASKS = 8
#: 单个子任务超时（秒）
DEFAULT_SUBTASK_TIMEOUT = 300.0
MAX_SUBTASK_TIMEOUT = 1800.0
#: 单个子任务最大重试次数（不含首次执行）
DEFAULT_MAX_RETRIES = 1
MAX_RETRIES_LIMIT = 3
#: 两次重试之间的退避基数（与 providers.py 的 2.0*attempt 同风格）
RETRY_BACKOFF = 2.0

# ---- 业务错误码（对齐 queue.py 的 2xxx 段） ----
ERR_PARAM = 2030              # 参数不合法（空目标 / 子任务过多 / 非法超时）
ERR_PLAN_NOT_FOUND = 2031     # 计划不存在
ERR_PLAN_BUSY = 2032          # 计划已在运行（重复 run）
ERR_NOTHING_TO_RESUME = 2033  # 没有未完成的子任务可续跑

# ---- SSE 事件名（与 agent.py 的 {"type": ...} 约定一致） ----
EV_PLAN = "parallel_plan"       # 计划已拆分（plan_id, tasks[]）
EV_TASK = "parallel_task"       # 子任务状态变化（id, title, status, attempt）
EV_DONE = "parallel_done"       # 计划结束（plan_id, status, summary）
EV_ERROR = "parallel_error"     # 计划级错误（message）

_SPLIT_PROMPT = """你是任务拆分器。请把用户的大任务拆成 2~{n} 个**可独立执行**的
子任务，每个子任务一句话（动宾结构），彼此不依赖、可以同时开工。
只输出 JSON 数组，不要任何解释：
["子任务1", "子任务2", ...]"""

_AGGREGATE_PROMPT = """请基于下面若干子任务的结论，用中文 Markdown 输出总报告，
结论先行，必要时用列表或表格。不要编造子任务里没有的信息。

总目标：{goal}

子任务结论：
{results}"""


class ParallelError(Exception):
    """并行任务业务错误（code 见模块顶部常量；消息已中文化，可直接透出）。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)


@dataclass
class SubTask:
    """一个子任务（= 一个独立 session 的一次对话）。"""

    id: str
    title: str
    prompt: str
    status: str = "pending"       # pending/running/done/failed/cancelled
    attempts: int = 0
    output: str = ""
    error: str = ""
    session_id: str = ""
    provider: str = ""
    model: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "title": self.title, "prompt": self.prompt,
            "status": self.status, "attempts": self.attempts,
            "output": self.output, "error": self.error,
            "session_id": self.session_id, "provider": self.provider,
            "model": self.model, "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SubTask":
        data = data or {}
        return cls(
            id=str(data.get("id") or "t"),
            title=str(data.get("title") or ""),
            prompt=str(data.get("prompt") or ""),
            status=str(data.get("status") or "pending"),
            attempts=int(data.get("attempts") or 0),
            output=str(data.get("output") or ""),
            error=str(data.get("error") or ""),
            session_id=str(data.get("session_id") or ""),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            started_at=float(data.get("started_at") or 0),
            finished_at=float(data.get("finished_at") or 0),
        )


@dataclass
class ParallelPlan:
    """一个并行计划（= 一次多任务执行 + 它的断点文件内容）。"""

    id: str
    goal: str
    title: str = ""
    workspace: str = ""
    provider: str = ""
    model: str = ""
    status: str = "planning"
    subtasks: List[SubTask] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    max_workers: int = MAX_PARALLEL_WORKERS
    max_retries: int = DEFAULT_MAX_RETRIES
    subtask_timeout: float = DEFAULT_SUBTASK_TIMEOUT
    created_at: float = 0.0
    updated_at: float = 0.0
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "goal": self.goal, "title": self.title,
            "workspace": self.workspace, "provider": self.provider,
            "model": self.model, "status": self.status,
            "subtasks": [t.to_dict() for t in self.subtasks],
            "summary": self.summary, "error": self.error,
            "max_workers": self.max_workers, "max_retries": self.max_retries,
            "subtask_timeout": self.subtask_timeout,
            "created_at": self.created_at, "updated_at": self.updated_at,
            "events": self.events[-parallel_store.MAX_EVENTS:],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ParallelPlan":
        data = data or {}
        return cls(
            id=str(data.get("id") or ""),
            goal=str(data.get("goal") or ""),
            title=str(data.get("title") or ""),
            workspace=str(data.get("workspace") or ""),
            provider=str(data.get("provider") or ""),
            model=str(data.get("model") or ""),
            status=str(data.get("status") or "planning"),
            subtasks=[SubTask.from_dict(t) for t in (data.get("subtasks") or [])],
            summary=str(data.get("summary") or ""),
            error=str(data.get("error") or ""),
            max_workers=int(data.get("max_workers") or MAX_PARALLEL_WORKERS),
            max_retries=int(data.get("max_retries") or DEFAULT_MAX_RETRIES),
            subtask_timeout=float(data.get("subtask_timeout")
                                 or DEFAULT_SUBTASK_TIMEOUT),
            created_at=float(data.get("created_at") or 0),
            updated_at=float(data.get("updated_at") or 0),
            events=list(data.get("events") or []),
        )

    # ---------------- 便捷视图 ----------------
    def task(self, task_id: str) -> Optional[SubTask]:
        for t in self.subtasks:
            if t.id == task_id:
                return t
        return None

    def pending_tasks(self) -> List[SubTask]:
        """未完成的子任务（pending / failed / running[上次中断]）。"""
        return [t for t in self.subtasks if t.status != "done"]


class ParallelRunner:
    """并行计划调度器：拆分 → 并发执行 → 聚合，支持取消与断点续跑。

    线程模型与仓库既有风格一致：
        - 子任务各跑在自己的 daemon 线程里（Agent.start_turn 是阻塞的，
          审批等待更是同步阻塞，只能在线程里跑）
        - 调度器用 ThreadPoolExecutor 控制并发度
        - 取消走 Agent.stop() 的协作式标志，不做硬中断
    """

    def __init__(self, assistant: Any = None) -> None:
        self._lock = threading.RLock()
        self._assistant = assistant
        self._cancel: Dict[str, bool] = {}
        self._running: Dict[str, bool] = {}

    # ------------------------------------------------------------------ #
    # 依赖
    # ------------------------------------------------------------------ #
    def assistant(self):
        """取 Agent 单例（延迟导入，避免模块级循环依赖）。"""
        if self._assistant is None:
            from core.assistant.agent import get_assistant
            self._assistant = get_assistant()
        return self._assistant

    @staticmethod
    def _emit(emit: Optional[Callable[[Dict[str, Any]], None]],
              event: Dict[str, Any]) -> None:
        """事件出口：emit 异常一律吞掉（不能让 UI 抖动拖垮执行）。"""
        if emit is None:
            return
        try:
            emit(event)
        except Exception:
            pass

    def _record(self, plan: ParallelPlan,
                emit: Optional[Callable[[Dict[str, Any]], None]],
                event: Dict[str, Any]) -> None:
        plan.events.append(dict(event))
        if len(plan.events) > parallel_store.MAX_EVENTS:
            plan.events = plan.events[-parallel_store.MAX_EVENTS:]
        self._emit(emit, event)

    def _persist(self, plan: ParallelPlan) -> None:
        """把计划快照落盘（断点）。"""
        plan.updated_at = time.time()
        parallel_store.save_plan(plan.to_dict())

    # ------------------------------------------------------------------ #
    # 计划拆分
    # ------------------------------------------------------------------ #
    def plan(self, goal: str, provider_id: str = "", model: str = "",
             max_tasks: int = MAX_TASKS, workspace: str = "",
             title: str = "", subtasks: Optional[List[Dict[str, Any]]] = None,
             emit: Optional[Callable[[Dict[str, Any]], None]] = None
             ) -> ParallelPlan:
        """拆分目标为子任务计划（只规划，不执行）。

        `subtasks` 非空时直接用它（确定性路径：不调模型，测试与
        「我已经想好怎么拆」的场景都走这里）；否则调模型拆分。
        """
        goal = (goal or "").strip()
        if not goal and not subtasks:
            raise ParallelError(ERR_PARAM, "任务目标不能为空")
        try:
            n = int(max_tasks or MAX_TASKS)
        except (TypeError, ValueError):
            n = MAX_TASKS
        n = max(1, min(n, MAX_TASKS))

        cfg = load_config()
        pcfg = cfg_provider(cfg, provider_id)
        titles: List[str] = []
        details: List[str] = []
        model_name = model or str(pcfg.get("model") or "")
        if subtasks:
            # 调用方已经想好怎么拆：不调模型，也不碰 provider（否则本地引擎
            # 没启动时 resolve_model 会直接失败，把「确定性的拆分」也拖挂）
            for item in subtasks:
                if isinstance(item, dict):
                    t = str(item.get("title") or item.get("prompt") or "").strip()
                    d = str(item.get("prompt") or "").strip()
                else:
                    t = str(item or "").strip()
                    d = ""
                if t:
                    titles.append(t[:200])
                    details.append(d[:600])
        else:
            provider = get_provider(pcfg)
            try:
                model_name = provider.resolve_model(
                    model or pcfg.get("model") or "")
            except ProviderError as e:
                raise ParallelError(ERR_PARAM, f"模型不可用：{e}")
            try:
                reply = provider.chat(
                    [{"role": "system", "content": _SPLIT_PROMPT.format(n=n)},
                     {"role": "user", "content": goal}],
                    tools=None, model=model_name, temperature=0.2,
                    max_tokens=1024)
                # 严格模式：只认真正的 JSON 数组。模型随口一句「今天天气不错」
                # 绝不能被当成一个子任务（那等于凭空造了个任务）。
                titles = extract_json_array(reply.content or "",
                                            allow_lines=False)[:n]
                details = [""] * len(titles)
            except Exception as e:  # noqa: BLE001 —— 拆分失败不致命，降级单任务
                titles = []
                details = []
                self._emit(emit, {"type": EV_ERROR,
                                  "message": f"任务拆分失败，降级为单任务：{e}"})
        if not titles:
            # 兜底：拆不了就自己干（与 orchestrator 的 node_plan 同思路）
            titles = [goal or "完成目标"]
            details = [""]

        now = time.time()
        plan = ParallelPlan(
            id="p" + uuid.uuid4().hex[:12], goal=goal,
            title=title or (goal.strip().replace("\n", " ")[:24] or "并行任务"),
            workspace=workspace or "", provider=pcfg.get("id") or "",
            model=model_name, status="planning", created_at=now,
            updated_at=now,
            max_workers=self._workers(cfg),
            max_retries=self._retries(cfg),
            subtask_timeout=self._timeout(cfg))
        for i, t in enumerate(titles, 1):
            detail = details[i - 1] if i - 1 < len(details) else ""
            body = (f"总目标：{goal}\n你的子任务：{t}\n"
                    + (f"补充说明：{detail}\n" if detail else "")
                    + "请独立完成这个子任务并给出结论"
                      "（不要重复其他子任务的工作）。")
            plan.subtasks.append(SubTask(
                id=f"t{i}", title=t, prompt=body,
                provider=plan.provider, model=plan.model))
        self._record(plan, emit, {"type": EV_PLAN, "plan_id": plan.id,
                                  "goal": plan.goal,
                                  "tasks": [t.to_dict() for t in plan.subtasks]})
        self._persist(plan)
        audit("parallel_plan_created", plan_id=plan.id, goal=plan.goal,
              tasks=len(plan.subtasks), provider=plan.provider, model=plan.model)
        return plan

    # ------------------------------------------------------------------ #
    # 配置读取（与 store 的 config 字段同源）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _cfg_int(cfg: Dict[str, Any], key: str, default: int,
                 limit: int) -> int:
        try:
            return max(1, min(int(cfg.get(key) or default), limit))
        except (TypeError, ValueError):
            return default

    def _workers(self, cfg: Dict[str, Any]) -> int:
        return self._cfg_int(cfg, "parallel_max_workers", MAX_PARALLEL_WORKERS,
                             MAX_PARALLEL_WORKERS_LIMIT)

    def _retries(self, cfg: Dict[str, Any]) -> int:
        return self._cfg_int(cfg, "parallel_max_retries", DEFAULT_MAX_RETRIES,
                             MAX_RETRIES_LIMIT)

    def _timeout(self, cfg: Dict[str, Any]) -> float:
        try:
            val = float(cfg.get("parallel_subtask_timeout")
                        or DEFAULT_SUBTASK_TIMEOUT)
        except (TypeError, ValueError):
            val = DEFAULT_SUBTASK_TIMEOUT
        return max(10.0, min(val, MAX_SUBTASK_TIMEOUT))

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def run(self, plan: ParallelPlan,
            emit: Optional[Callable[[Dict[str, Any]], None]] = None,
            provider_id: str = "", model: str = "",
            only: Optional[List[str]] = None) -> ParallelPlan:
        """并行执行计划里的子任务（阻塞直到全部结束）。

        `only` 指定只跑这些子任务 id（断点续跑用：只跑未完成的）。
        """
        with self._lock:
            if self._running.get(plan.id):
                raise ParallelError(ERR_PLAN_BUSY, "该计划正在运行中")
            self._running[plan.id] = True
            self._cancel[plan.id] = False
        try:
            plan.status = "running"
            plan.error = ""
            self._persist(plan)
            self._execute(plan, emit, provider_id, model, only)
            self._finish(plan, emit, provider_id, model)
        finally:
            with self._lock:
                self._running[plan.id] = False
        return plan

    def _execute(self, plan: ParallelPlan,
                 emit: Optional[Callable[[Dict[str, Any]], None]],
                 provider_id: str, model: str,
                 only: Optional[List[str]]) -> None:
        targets = [t for t in plan.subtasks
                   if (not only or t.id in only) and t.status != "done"]
        if not targets:
            return
        workers = max(1, min(plan.max_workers, len(targets)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="parallel") as ex:
            futures = {ex.submit(self._run_subtask, plan, t, provider_id,
                                 model): t for t in targets}
            for fut, task in futures.items():
                try:
                    fut.result()
                except Exception as e:  # noqa: BLE001 —— 单任务异常不拖垮其它
                    task.status = "failed"
                    task.error = f"{type(e).__name__}: {e}"
                    task.finished_at = time.time()
                    self._record(plan, emit, {"type": EV_TASK, "id": task.id,
                                              "title": task.title,
                                              "status": task.status,
                                              "error": task.error})
                self._persist(plan)

    def _run_subtask(self, plan: ParallelPlan, task: SubTask,
                     provider_id: str, model: str) -> None:
        """执行单个子任务：重试 → 超时 → 取消 → 落断点。

        `attempts` 是**累计**的（断点续跑时接着往上加），这样审计里能看出
        「这条子任务前后一共试了几次」；单轮执行内的重试次数仍受
        `plan.max_retries` 限制（续跑视为新一轮执行，给一份新的额度）。
        """
        assistant = self.assistant()
        pid = plan.id
        budget = max(1, 1 + int(plan.max_retries or 0))
        already = int(task.attempts or 0)

        for attempt in range(1, budget + 1):
            if self.is_cancelled(pid):
                task.status = "cancelled"
                task.error = "已取消"
                task.finished_at = time.time()
                return
            task.attempts = already + attempt
            task.status = "running"
            task.started_at = task.started_at or time.time()
            task.provider = provider_id or plan.provider
            task.model = model or plan.model
            # 会话先建好并立刻落断点：中途断电也知道这条子任务的对话在哪个会话
            if not task.session_id:
                try:
                    sess = assistant.new_session(task.title, plan.workspace)
                    task.session_id = str((sess or {}).get("id") or "")
                except Exception as e:  # noqa: BLE001
                    task.status = "failed"
                    task.error = f"创建子任务会话失败：{e}"
                    task.finished_at = time.time()
                    return
            self._persist(plan)

            box: Dict[str, Any] = {}

            def _target() -> None:
                try:
                    box["result"] = assistant.start_turn(
                        task.session_id, task.prompt,
                        provider_id=task.provider, model=task.model,
                        emit=None)
                except Exception as e:  # noqa: BLE001
                    box["error"] = f"{type(e).__name__}: {e}"

            th = threading.Thread(target=_target, daemon=True,
                                  name=f"parallel-{pid}-{task.id}")
            th.start()
            th.join(max(1.0, float(plan.subtask_timeout or
                                   DEFAULT_SUBTASK_TIMEOUT)))
            if th.is_alive():
                # 超时：协作式停掉这个子任务（Agent.stop 会置取消标志，
                # provider 侧 cancel_check 会在下一个分片退出）
                try:
                    assistant.stop(task.session_id)
                except Exception:
                    pass
                th.join(2.0)
                task.status = "failed"
                task.error = f"子任务超时（>{int(plan.subtask_timeout)}s）"
                task.finished_at = time.time()
                return
            if "error" in box:
                err = str(box["error"])
                if attempt < budget and self._retryable(err):
                    time.sleep(min(RETRY_BACKOFF * attempt, 6.0))
                    continue
                task.status = "failed"
                task.error = err
                task.finished_at = time.time()
                return
            result = box.get("result") or {}
            content = str(result.get("content") or "")
            if not result.get("ok") or not content.strip():
                err = str(result.get("error") or "子任务没有返回内容")
                if attempt < budget and self._retryable(err):
                    time.sleep(min(RETRY_BACKOFF * attempt, 6.0))
                    continue
                task.status = "failed"
                task.error = err
                task.finished_at = time.time()
                return
            task.status = "done"
            task.output = content
            task.error = ""
            task.finished_at = time.time()
            return

    @staticmethod
    def _retryable(error: str) -> bool:
        """可重试判定：只认限流/超时/网络这类瞬时错误。

        用户拒绝审批、参数非法、模型不存在这类错误重试也没用，直接失败。
        """
        e = (error or "").lower()
        markers = ("429", "rate limit", "too many requests", "timeout",
                   "timed out", "超时", "连接", "connection", "temporarily",
                   "502", "503", "504", "providererror")
        return any(m in e for m in markers)

    def _finish(self, plan: ParallelPlan,
                emit: Optional[Callable[[Dict[str, Any]], None]],
                provider_id: str, model: str) -> None:
        """聚合结果并落终态。"""
        if self.is_cancelled(plan.id):
            plan.status = "cancelled"
        else:
            done = [t for t in plan.subtasks if t.status == "done"]
            failed = [t for t in plan.subtasks
                      if t.status in ("failed", "cancelled")]
            if not failed and len(done) == len(plan.subtasks):
                plan.status = "done"
            elif done:
                plan.status = "partial"
            else:
                plan.status = "failed"
        plan.summary = self.aggregate(plan, provider_id, model)
        self._persist(plan)
        self._record(plan, emit, {"type": EV_DONE, "plan_id": plan.id,
                                  "status": plan.status,
                                  "summary": plan.summary})
        audit("parallel_plan_done", plan_id=plan.id, status=plan.status,
              done=sum(1 for t in plan.subtasks if t.status == "done"),
              failed=sum(1 for t in plan.subtasks if t.status == "failed"))

    # ------------------------------------------------------------------ #
    # 聚合
    # ------------------------------------------------------------------ #
    def aggregate(self, plan: ParallelPlan, provider_id: str = "",
                  model: str = "") -> str:
        """把各子任务结论汇总成一段总报告。

        模型汇总失败时**降级为拼接**（不编造），与 orchestrator「不许编」
        的红线一致。
        """
        done = [t for t in plan.subtasks if t.status == "done"]
        if not done:
            failed = [t for t in plan.subtasks if t.status == "failed"]
            first = failed[0].error if failed else "未知原因"
            return f"并行任务没有成功完成：{first}"
        results = "\n\n".join(f"【{t.title}】\n{t.output}" for t in done)
        try:
            cfg = load_config()
            pcfg = cfg_provider(cfg, provider_id or plan.provider)
            provider = get_provider(pcfg)
            model_name = provider.resolve_model(
                model or plan.model or pcfg.get("model") or "")
            reply = provider.chat(
                [{"role": "system",
                  "content": _AGGREGATE_PROMPT.format(goal=plan.goal,
                                                       results=results[:12000])},
                 {"role": "user", "content": "请输出总报告。"}],
                tools=None, model=model_name, temperature=0.2, max_tokens=2048)
            text = (reply.content or "").strip()
            if text:
                return text
        except Exception:
            pass
        # 降级：直接拼接（不丢信息）
        return results

    # ------------------------------------------------------------------ #
    # 取消 / 续跑
    # ------------------------------------------------------------------ #
    def is_cancelled(self, plan_id: str) -> bool:
        with self._lock:
            return bool(self._cancel.get(plan_id))

    def is_running(self, plan_id: str) -> bool:
        """计划是否还在执行中（取消后要等它真正收尾，此时不宜续跑）。"""
        with self._lock:
            return bool(self._running.get(plan_id))

    def cancel(self, plan_id: str) -> bool:
        """取消计划：置标志 + 停掉正在跑的子任务会话。"""
        plan = parallel_store.load_plan(plan_id)
        if not plan:
            raise ParallelError(ERR_PLAN_NOT_FOUND, "计划不存在")
        with self._lock:
            self._cancel[plan_id] = True
        assistant = self.assistant()
        for t in plan.get("subtasks") or []:
            if t.get("status") == "running" and t.get("session_id"):
                try:
                    assistant.stop(str(t["session_id"]))
                except Exception:
                    pass
        audit("parallel_plan_cancelled", plan_id=plan_id)
        return True

    def resume(self, plan_id: str, provider_id: str = "", model: str = "",
               emit: Optional[Callable[[Dict[str, Any]], None]] = None
               ) -> ParallelPlan:
        """从断点续跑：跳过已完成的，只跑未完成的（可换模型）。

        跨会话：子任务复用原 session_id，模型能看到上次的对话历史；
        跨模型：provider/model 只作用于未完成的子任务，已完成的结果原样保留。
        """
        data = parallel_store.load_plan(plan_id)
        if not data:
            raise ParallelError(ERR_PLAN_NOT_FOUND, "计划不存在")
        plan = ParallelPlan.from_dict(data)
        todo = [t for t in plan.subtasks if t.status != "done"]
        if not todo:
            raise ParallelError(ERR_NOTHING_TO_RESUME, "没有未完成的子任务")
        # 未完成的子任务继承上次记录的 provider/model，除非本次显式指定
        for t in todo:
            t.provider = provider_id or t.provider or plan.provider
            t.model = model or t.model or plan.model
        audit("parallel_plan_resume", plan_id=plan_id,
              pending=len(todo), provider=provider_id or plan.provider,
              model=model or plan.model)
        return self.run(plan, emit=emit, provider_id=provider_id, model=model)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    @staticmethod
    def get(plan_id: str) -> Optional[ParallelPlan]:
        data = parallel_store.load_plan(plan_id)
        return ParallelPlan.from_dict(data) if data else None

    @staticmethod
    def list(limit: int = 50) -> List[Dict[str, Any]]:
        return parallel_store.list_plans(limit)

    @staticmethod
    def delete(plan_id: str) -> bool:
        return parallel_store.delete_plan(plan_id)


_RUNNER: Optional[ParallelRunner] = None
_RUNNER_LOCK = threading.Lock()


def get_runner(assistant: Any = None) -> ParallelRunner:
    """模块级单例（与 agent.get_assistant / queue.get_queue 同模式）。"""
    global _RUNNER
    if _RUNNER is None:
        with _RUNNER_LOCK:
            if _RUNNER is None:
                _RUNNER = ParallelRunner(assistant)
    return _RUNNER
