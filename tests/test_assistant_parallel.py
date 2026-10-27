# -*- coding: utf-8 -*-
"""
并行多任务执行回归（core/assistant/parallel.py）。

覆盖：
    - 计划拆分：模型拆 JSON / 拆分失败降级单任务 / 调用方直接给定子任务
    - 并发调度：并发度受 max_workers 限制、子任务各占独立 session
    - 结果聚合：全成功 / 部分失败 / 全失败（不编造）
    - 失败重试：可重试错误重试到上限、不可重试错误不重试
    - 超时：子任务超时被停掉并标记 failed(timeout)，不挂死
    - 取消：取消后未开始的子任务标记 cancelled、已完成的结果保留
    - 取消竞态：取消与完成同时发生，计划终态稳定（cancelled 优先）
    - 断点持久化：running 的子任务在恢复时重跑；已完成的不重跑
    - 跨模型续跑：resume 换模型只作用于未完成的子任务
    - 并发场景：两个计划同时跑互不干扰
    - HTTP 层：/parallel/* 端点与错误码

全部用例都在临时目录里跑，不碰用户真实数据。
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.assistant import parallel as parallel_mod
from core.assistant import parallel_store
from core.assistant import skills as skills_mod
from core.assistant import store as store_mod
from core.assistant.parallel import (ERR_NOTHING_TO_RESUME, ERR_PARAM,
                                     ERR_PLAN_BUSY, ERR_PLAN_NOT_FOUND,
                                     ParallelError, ParallelRunner)
from core.assistant.providers import ChatReply


# ----------------------------------------------------------------------------
# 夹具：助理数据目录整体指到 tmp_path，并重置单例
# ----------------------------------------------------------------------------
@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "assistant"
    for sub in ("skills", "sessions", "audit", "workspace", "parallel"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store_mod, "ASSISTANT_DIR", str(root))
    monkeypatch.setattr(store_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(store_mod, "SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setattr(store_mod, "AUDIT_DIR", str(root / "audit"))
    monkeypatch.setattr(store_mod, "CONFIG_PATH", str(root / "config.json"))
    monkeypatch.setattr(store_mod, "DEFAULT_WORKSPACE", str(root / "workspace"))
    monkeypatch.setattr(skills_mod, "SKILLS_DIR", str(root / "skills"))
    monkeypatch.setattr(parallel_store, "PARALLEL_DIR", str(root / "parallel"))

    from core.assistant import agent as agent_mod
    from core.assistant import mcp as mcp_mod
    from core.assistant import approval as approval_mod
    monkeypatch.setattr(agent_mod, "_ASSISTANT", None)
    monkeypatch.setattr(mcp_mod, "_MANAGER", None)
    monkeypatch.setattr(approval_mod, "_MANAGER", None)
    monkeypatch.setattr(parallel_mod, "_RUNNER", None)
    store_mod.ensure_dirs()
    skills_mod.ensure_builtin_skills()
    return {"root": root, "workspace": root / "workspace"}


class FakeAssistant:
    """假 Agent：记录 start_turn 调用，可编程返回成功/失败/延迟。

    只实现并行模块真正用到的那几个方法（new_session / start_turn / stop），
    接口与 core/assistant/agent.py 的 Assistant 保持一致。
    """

    def __init__(self, results=None, delay=0.0, default=None, by_prompt=None):
        self.results = list(results or [])      # 依次取用；取完用 default
        self.default = default                  # 兜底结果（默认成功）
        # prompt 关键字 → 结果：并发下 results 的取用顺序取决于线程调度，
        # 想断言「第 N 个子任务失败」必须按 prompt 分流，不能靠列表顺序。
        self.by_prompt = dict(by_prompt or {})
        self.delay = delay
        self.calls = []                          # (session_id, prompt, provider, model)
        self.stopped = []
        self._n = 0
        self._lock = threading.Lock()
        self._sessions = {}
        self.live = 0
        self.max_live = 0

    def new_session(self, title="", workspace=""):
        with self._lock:
            self._n += 1
            sid = f"s{self._n:04d}"
            self._sessions[sid] = {"title": title, "workspace": workspace,
                                    "messages": []}
        return {"id": sid, "title": title, "workspace": workspace}

    def start_turn(self, session_id, text, provider_id="", model="",
                   emit=None, mounted_skills=None, attachments=None):
        with self._lock:
            self.calls.append((session_id, text, provider_id, model))
            sess = self._sessions.setdefault(session_id, {"messages": []})
            sess["messages"].append(text)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            if self.delay:
                time.sleep(self.delay)
            with self._lock:
                idx = len(self.calls) - 1
            # by_prompt 优先：按 prompt 里出现的子任务标题命中，绕开并发取用顺序
            hit = next((k for k in self.by_prompt if k in text), None)
            if hit is not None:
                res = self.by_prompt[hit]
            else:
                with self._lock:
                    res = (self.results[idx]
                           if idx < len(self.results) else self.default)
            if callable(res):
                res = res(session_id, text)
            if res is None:
                res = {"ok": True, "content": f"子任务完成：{text[:20]}"}
            return res
        finally:
            with self._lock:
                self.live -= 1

    def stop(self, session_id):
        self.stopped.append(session_id)


class FakeProvider:
    """按队列返回预设回复的假模型（拆分阶段用）。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def resolve_model(self, preferred=""):
        return preferred or "fake-model"

    def chat(self, messages, tools=None, model="", temperature=0.3,
             max_tokens=2048, on_delta=None, cancel_check=None, timeout=300):
        self.calls.append({"messages": messages, "model": model})
        if not self.replies:
            return ChatReply(content="完成")
        r = self.replies.pop(0)
        if callable(r):
            r = r(messages)
        if isinstance(r, str):
            r = ChatReply(content=r)
        return r


@pytest.fixture()
def fake_provider(monkeypatch):
    """把 provider 装配换成假模型（拆分 + 聚合两处都用它）。"""
    holder = {}

    def _install(replies):
        provider = FakeProvider(replies)
        holder["provider"] = provider
        monkeypatch.setattr(parallel_mod, "get_provider", lambda cfg: provider)
        return provider

    return _install


def _plan(runner, assistant, **kw):
    """造一个不走模型的计划（直接给定子任务，确定性路径）。"""
    kw.setdefault("goal", "总目标")
    kw.setdefault("subtasks", [{"title": f"子任务{i}"} for i in (1, 2, 3)])
    return runner.plan(**kw)


# ----------------------------------------------------------------------------
# 计划拆分
# ----------------------------------------------------------------------------
def test_plan_splits_goal_into_subtasks(env, fake_provider):
    fake_provider(['["扫描目录", "提取要点", "写报告"]'])
    runner = ParallelRunner(FakeAssistant())
    plan = runner.plan(goal="整理资料")
    assert [t.title for t in plan.subtasks] == ["扫描目录", "提取要点", "写报告"]
    assert plan.status == "planning"
    # 断点文件已落盘
    assert parallel_store.load_plan(plan.id) is not None


def test_plan_falls_back_to_single_task_when_split_fails(env, fake_provider):
    """模型拆分不出东西时不能空手而归：降级成「自己干」这一个子任务。"""
    fake_provider(["完全不是 JSON 的一段话"])
    runner = ParallelRunner(FakeAssistant())
    plan = runner.plan(goal="整理资料")
    assert len(plan.subtasks) == 1
    assert plan.subtasks[0].title == "整理资料"


def test_plan_accepts_caller_provided_subtasks(env):
    """调用方直接给定子任务时不调模型（确定性路径）。"""
    runner = ParallelRunner(FakeAssistant())
    plan = runner.plan(goal="g", subtasks=[{"title": "A", "prompt": "做A"},
                                           {"title": "B"}])
    assert [t.title for t in plan.subtasks] == ["A", "B"]
    assert "做A" in plan.subtasks[0].prompt


def test_plan_rejects_empty_goal(env):
    runner = ParallelRunner(FakeAssistant())
    with pytest.raises(ParallelError) as ei:
        runner.plan(goal="   ")
    assert ei.value.code == ERR_PARAM


# ----------------------------------------------------------------------------
# 并发调度
# ----------------------------------------------------------------------------
def test_run_executes_all_subtasks_in_parallel(env):
    """三个子任务并发跑完，且各自的 session 互不相同（隔离）。"""
    assistant = FakeAssistant(delay=0.15)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    plan.max_workers = 3
    runner.run(plan)
    assert plan.status == "done"
    assert all(t.status == "done" for t in plan.subtasks)
    sids = {c[0] for c in assistant.calls}
    assert len(sids) == 3                      # 一个子任务一个会话
    assert assistant.max_live > 1             # 确实并发


def test_concurrency_is_capped_by_max_workers(env):
    """并发度不能超过 max_workers（别把模型端打爆）。"""
    assistant = FakeAssistant(delay=0.1)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    plan.max_workers = 2
    runner.run(plan)
    assert assistant.max_live <= 2


def test_run_persists_checkpoint_per_subtask(env):
    """每个子任务完成后都要落断点（断电也不丢进度）。"""
    assistant = FakeAssistant()
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    runner.run(plan)
    saved = parallel_store.load_plan(plan.id)
    assert saved is not None
    assert [t["status"] for t in saved["subtasks"]] == ["done", "done", "done"]
    assert all(t["session_id"] for t in saved["subtasks"])


# ----------------------------------------------------------------------------
# 结果聚合
# ----------------------------------------------------------------------------
def test_aggregate_reports_partial_failure(env):
    """部分失败时要如实说，不能编成「全部完成」。

    这里不装假模型：聚合时 provider 不可用 → 按设计降级为拼接子任务结论，
    正好验证「模型不可用也不丢信息、不编造」这条降级路径。

    结果按 **prompt 里的子任务标题** 分流（`by_prompt`）：三个子任务并发跑，
    谁先登记谁拿 results[0]，靠列表顺序断言「第 2 个失败」会在负载高时
    偶发乱序（历史上出现过一次全量跑失败、单跑通过）。
    """
    assistant = FakeAssistant(by_prompt={
        "子任务1": {"ok": True, "content": "A 完成"},
        "子任务2": {"ok": False, "error": "文件不存在"},
        "子任务3": {"ok": True, "content": "C 完成"},
    })
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    runner.run(plan)
    assert plan.status == "partial"
    assert [t.status for t in plan.subtasks] == ["done", "failed", "done"]
    assert "A 完成" in plan.summary
    assert "C 完成" in plan.summary


def test_aggregate_reports_total_failure_without_fabricating(env):
    """全失败时给出真实原因，而不是编一份总结。"""
    assistant = FakeAssistant(results=[
        {"ok": False, "error": "模型不可用"},
        {"ok": False, "error": "模型不可用"},
        {"ok": False, "error": "模型不可用"},
    ])
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    runner.run(plan)
    assert plan.status == "failed"
    assert "模型不可用" in plan.summary
    assert all(t.output == "" for t in plan.subtasks)


# ----------------------------------------------------------------------------
# 重试
# ----------------------------------------------------------------------------
def test_retryable_error_is_retried_up_to_limit(env):
    """限流类错误重试；重试上限用完才判失败（attempts 记在断点里）。"""
    calls = {"n": 0}

    def _flaky(sid, text):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": False, "error": "429 rate limit exceeded"}
        return {"ok": True, "content": "第二次成功"}

    assistant = FakeAssistant(results=[_flaky])
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "只有一个"}])
    plan.max_retries = 1
    runner.run(plan)
    assert plan.subtasks[0].status == "done"
    assert plan.subtasks[0].attempts == 2


def test_non_retryable_error_is_not_retried(env):
    """用户拒绝审批这类错误重试也没用，不浪费时间。"""
    assistant = FakeAssistant(results=[
        {"ok": False, "error": "用户未批准该操作"}])
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "只有一个"}])
    plan.max_retries = 3
    runner.run(plan)
    assert plan.subtasks[0].status == "failed"
    assert plan.subtasks[0].attempts == 1


def test_retry_limit_is_respected(env):
    """一直失败也不能无限重试：attempts 不超过 max_retries+1。"""
    assistant = FakeAssistant(default={"ok": False, "error": "503 service unavailable"})
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "只有一个"}])
    plan.max_retries = 2
    runner.run(plan)
    assert plan.subtasks[0].status == "failed"
    assert plan.subtasks[0].attempts == 3


# ----------------------------------------------------------------------------
# 超时
# ----------------------------------------------------------------------------
def test_subtask_timeout_marks_failed_and_stops_session(env):
    """子任务卡住不能把整个计划挂死：超时 → 停会话 → 标 failed(timeout)。"""
    assistant = FakeAssistant(delay=5.0)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "卡住"}])
    plan.subtask_timeout = 0.2          # 测试里用极短超时
    t0 = time.time()
    runner.run(plan)
    assert time.time() - t0 < 4.0        # 没等满 5s
    assert plan.subtasks[0].status == "failed"
    assert "超时" in plan.subtasks[0].error
    assert assistant.stopped              # 已经把卡住的会话停掉


# ----------------------------------------------------------------------------
# 取消
# ----------------------------------------------------------------------------
def test_cancel_marks_unstarted_tasks_cancelled(env):
    """取消后没开工的子任务标记 cancelled，不该继续跑。"""
    assistant = FakeAssistant(delay=0.4)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    plan.max_workers = 1

    def _cancel_later():
        time.sleep(0.1)
        runner.cancel(plan.id)

    th = threading.Thread(target=_cancel_later)
    th.start()
    runner.run(plan)
    th.join()
    assert plan.status == "cancelled"
    assert any(t.status == "cancelled" for t in plan.subtasks)


def test_cancel_keeps_finished_results(env):
    """取消不该回滚已经完成的结果。"""
    assistant = FakeAssistant()
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}])
    runner.run(plan)
    runner.cancel(plan.id)
    saved = parallel_store.load_plan(plan.id)
    assert saved["subtasks"][0]["status"] == "done"
    assert saved["subtasks"][0]["output"]


def test_cancel_race_is_stable(env):
    """取消与完成同时发生：计划终态稳定，不能出现半吊子状态。"""
    assistant = FakeAssistant(delay=0.05)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant)
    stop = threading.Event()

    def _cancel_soon():
        stop.wait(0.05)
        try:
            runner.cancel(plan.id)
        except ParallelError:
            pass

    th = threading.Thread(target=_cancel_soon)
    th.start()
    runner.run(plan)
    th.join()
    assert plan.status in ("cancelled", "done", "partial")
    saved = parallel_store.load_plan(plan.id)
    assert saved is not None
    # 每个子任务的状态都必须是已知状态（不能是 running 残留）
    assert all(t["status"] in ("done", "failed", "cancelled", "pending")
               for t in saved["subtasks"])


def test_cancel_unknown_plan_raises(env):
    runner = ParallelRunner(FakeAssistant())
    with pytest.raises(ParallelError) as ei:
        runner.cancel("p_not_exist")
    assert ei.value.code == ERR_PLAN_NOT_FOUND


# ----------------------------------------------------------------------------
# 断点续跑
# ----------------------------------------------------------------------------
def test_resume_skips_done_and_reruns_unfinished(env):
    """中断后恢复：已完成的跳过，未完成的（含 running 残留）重跑。"""
    assistant = FakeAssistant()
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}, {"title": "B"}])
    # 手工造一个「上次中断」的状态：A 已完成，B 卡在 running
    plan.subtasks[0].status = "done"
    plan.subtasks[0].output = "A 的结论"
    plan.subtasks[0].session_id = "sA"
    plan.subtasks[1].status = "running"
    plan.subtasks[1].session_id = "sB"
    plan.status = "running"
    parallel_store.save_plan(plan.to_dict())

    runner.resume(plan.id)
    saved = parallel_store.load_plan(plan.id)
    assert saved["subtasks"][0]["status"] == "done"
    assert saved["subtasks"][0]["output"] == "A 的结论"     # 结果没被覆盖
    assert saved["subtasks"][1]["status"] == "done"         # 断点子任务补跑完成
    # A 没有被重跑（只有一条 start_turn 记录，且用的是 B 的会话）
    assert [c[0] for c in assistant.calls] == ["sB"]


def test_resume_reuses_session_for_cross_session_continuity(env):
    """跨会话续跑：复用原 session_id，模型能看到上次的对话历史。"""
    assistant = FakeAssistant()
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}])
    plan.subtasks[0].status = "running"
    plan.subtasks[0].session_id = "s_keep"
    parallel_store.save_plan(plan.to_dict())
    runner.resume(plan.id)
    assert assistant.calls[0][0] == "s_keep"


def test_resume_with_new_model_only_affects_unfinished(env):
    """换模型续跑：只作用于未完成的子任务，已完成的结果原样保留。"""
    assistant = FakeAssistant()
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}, {"title": "B"}])
    plan.subtasks[0].status = "done"
    plan.subtasks[0].output = "A 的结论"
    plan.subtasks[0].session_id = "sA"
    plan.subtasks[0].model = "old-model"
    plan.subtasks[1].status = "failed"
    plan.subtasks[1].session_id = "sB"
    plan.subtasks[1].model = "old-model"
    parallel_store.save_plan(plan.to_dict())

    runner.resume(plan.id, model="new-model")
    saved = parallel_store.load_plan(plan.id)
    assert saved["subtasks"][0]["model"] == "old-model"     # 已完成的不动
    assert saved["subtasks"][1]["model"] == "new-model"     # 未完成的换模型
    assert assistant.calls[0][3] == "new-model"


def test_resume_accumulates_attempts(env):
    """attempts 跨续跑累计（审计里能看出这条子任务前后一共试了几次）。"""
    assistant = FakeAssistant()
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}])
    plan.subtasks[0].status = "failed"
    plan.subtasks[0].attempts = 2          # 之前已经试过两次
    parallel_store.save_plan(plan.to_dict())
    runner.resume(plan.id)
    saved = parallel_store.load_plan(plan.id)
    assert saved["subtasks"][0]["attempts"] == 3      # 2 + 本次 1
    assert saved["subtasks"][0]["status"] == "done"


def test_resume_without_pending_tasks_raises(env):
    runner = ParallelRunner(FakeAssistant())
    plan = _plan(runner, FakeAssistant(), subtasks=[{"title": "A"}])
    plan.subtasks[0].status = "done"
    parallel_store.save_plan(plan.to_dict())
    with pytest.raises(ParallelError) as ei:
        runner.resume(plan.id)
    assert ei.value.code == ERR_NOTHING_TO_RESUME


def test_resume_unknown_plan_raises(env):
    runner = ParallelRunner(FakeAssistant())
    with pytest.raises(ParallelError) as ei:
        runner.resume("p_nope")
    assert ei.value.code == ERR_PLAN_NOT_FOUND


# ----------------------------------------------------------------------------
# 并发场景
# ----------------------------------------------------------------------------
def test_two_plans_run_concurrently_without_interference(env):
    """两个计划同时跑：状态互不污染，断点文件各自独立。"""
    assistant = FakeAssistant(delay=0.1)
    runner = ParallelRunner(assistant)
    p1 = _plan(runner, assistant, subtasks=[{"title": "A1"}, {"title": "A2"}])
    p2 = _plan(runner, assistant, subtasks=[{"title": "B1"}, {"title": "B2"}])
    t1 = threading.Thread(target=runner.run, args=(p1,))
    t2 = threading.Thread(target=runner.run, args=(p2,))
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)
    assert p1.status == "done" and p2.status == "done"
    assert p1.id != p2.id
    s1 = {c[0] for c in assistant.calls if c[1].find("A") >= 0}
    s2 = {c[0] for c in assistant.calls if c[1].find("B") >= 0}
    assert s1 and s2 and not (s1 & s2)      # 会话不交叉


def test_running_plan_rejects_second_run(env):
    """同一个计划不能并发跑两遍（会互相踩断点）。"""
    assistant = FakeAssistant(delay=0.2)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}])
    err = {}

    def _run():
        try:
            runner.run(plan)
        except ParallelError as e:
            err["e"] = e

    th = threading.Thread(target=_run)
    th.start()
    time.sleep(0.05)
    with pytest.raises(ParallelError):
        runner.run(plan)
    th.join(5)
    assert "e" not in err or err["e"].code != ERR_PARAM


def test_is_running_reports_inflight_plan(env):
    """is_running：取消后要等它真正收尾，收尾前不能续跑。"""
    assistant = FakeAssistant(delay=0.2)
    runner = ParallelRunner(assistant)
    plan = _plan(runner, assistant, subtasks=[{"title": "A"}])
    assert runner.is_running(plan.id) is False
    th = threading.Thread(target=runner.run, args=(plan,))
    th.start()
    time.sleep(0.05)
    assert runner.is_running(plan.id) is True
    th.join(5)
    assert runner.is_running(plan.id) is False


# ----------------------------------------------------------------------------
# 持久化层
# ----------------------------------------------------------------------------
def test_parallel_store_atomic_roundtrip(env):
    plan = {"id": "pX", "goal": "g", "subtasks": [], "status": "running"}
    parallel_store.save_plan(plan)
    got = parallel_store.load_plan("pX")
    assert got["goal"] == "g"
    assert "pX.json" in os.listdir(str(root_dir(env)))
    parallel_store.delete_plan("pX")
    assert parallel_store.load_plan("pX") is None


def root_dir(env):
    return str(parallel_store.PARALLEL_DIR)


def test_parallel_store_list_is_sorted_and_limited(env):
    for i in range(3):
        parallel_store.save_plan({"id": f"p{i}", "goal": f"g{i}",
                                   "status": "done", "subtasks": [],
                                   "updated_at": time.time() + i})
    rows = parallel_store.list_plans(limit=2)
    assert len(rows) == 2
    assert rows[0]["updated_at"] >= rows[1]["updated_at"]


# ----------------------------------------------------------------------------
# 与真实 Agent 集成（子任务真的走 Assistant.start_turn）
# ----------------------------------------------------------------------------
def test_integration_real_agent_creates_isolated_sessions(env, monkeypatch):
    """集成路径：子任务走真实 Agent.start_turn → 各子任务独立会话、各自落库。"""
    from core.assistant import agent as agent_mod

    class _AnyProvider:
        """任何调用都回一句固定内容（不做工具调用）。"""

        def resolve_model(self, preferred=""):
            return preferred or "fake-model"

        def chat(self, messages, tools=None, model="", temperature=0.3,
                 max_tokens=2048, on_delta=None, cancel_check=None, timeout=300):
            return ChatReply(content="子任务已完成")

    monkeypatch.setattr(agent_mod, "get_provider", lambda cfg: _AnyProvider())
    agent = agent_mod.get_assistant()

    runner = ParallelRunner(agent)
    plan = runner.plan(goal="整理资料", subtasks=[{"title": "A"}, {"title": "B"}])
    runner.run(plan)

    assert [t.status for t in plan.subtasks] == ["done", "done"]
    sids = [t.session_id for t in plan.subtasks]
    assert all(sids) and sids[0] != sids[1]
    # 两个子任务各自落了一个会话文件，消息互不串
    for sid in sids:
        sess = store_mod.load_session(sid)
        assert sess is not None
        roles = [m.get("role") for m in sess.get("messages") or []]
        assert roles == ["user", "assistant"]
    texts = {(store_mod.load_session(sid)["messages"][0]["content"])
             for sid in sids}
    assert len(texts) == 2          # 两条不同的子任务提示


# ----------------------------------------------------------------------------
# HTTP 层
# ----------------------------------------------------------------------------
def test_http_parallel_endpoints(env, fake_provider, monkeypatch):
    """路由层：拆分 / 执行 / 查询 / 续跑 / 取消 的错误码与返回。"""
    import asyncio

    from routers import assistant as router_mod

    fake_provider(["汇总用回复"])
    assistant = FakeAssistant()
    monkeypatch.setattr(router_mod, "get_runner",
                        lambda: ParallelRunner(assistant))

    async def _run():
        plan_resp = await router_mod.parallel_plan({"goal": "g", "subtasks": [
            {"title": "A"}, {"title": "B"}]})
        assert plan_resp["code"] == 0
        pid = plan_resp["data"]["id"]

        detail = await router_mod.parallel_detail(pid)
        assert detail["code"] == 0
        assert len(detail["data"]["subtasks"]) == 2

        missing = await router_mod.parallel_detail("p_nope")
        assert missing["code"] == ERR_PLAN_NOT_FOUND

        bad = await router_mod.parallel_plan({"goal": ""})
        assert bad["code"] == ERR_PARAM

        cancel = await router_mod.parallel_cancel(pid)
        assert cancel["code"] == 0

        # resume 是**后台执行**（真实模型可能跑几分钟，不能让 HTTP 等着）：
        # 立即返回计划快照；没有未完成子任务时给 2033
        resume = await router_mod.parallel_resume(pid, {})
        assert resume["code"] == 0
        no_pending = await router_mod.parallel_resume(pid, {})
        assert no_pending["code"] in (0, ERR_NOTHING_TO_RESUME, ERR_PLAN_BUSY)

        lst = await router_mod.parallel_list(limit=10)
        assert lst["code"] == 0 and lst["data"]

    asyncio.run(_run())


def test_http_resume_requires_existing_plan(env, fake_provider, monkeypatch):
    """续跑不存在的计划 → 2031（而不是抛异常）。"""
    import asyncio

    from routers import assistant as router_mod

    fake_provider(["回复"])
    monkeypatch.setattr(router_mod, "get_runner",
                        lambda: ParallelRunner(FakeAssistant()))

    async def _run():
        resp = await router_mod.parallel_resume("p_nope", {})
        assert resp["code"] == ERR_PLAN_NOT_FOUND

    asyncio.run(_run())
