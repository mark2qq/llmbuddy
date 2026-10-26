# -*- coding: utf-8 -*-
"""
LangGraph 技能编排（core/assistant/orchestrator.py）。

定位：OpenClaw 负责「网关 / 工具 / 权限 / UI」，本模块只负责「技能内部复杂
子任务的编排」——把 SKILL.md 的指令拆成可验证的阶段：

    START → plan → act → review ─┬→ act（未完成且未超步数）
                                 └→ finish → END

    plan   让模型把任务拆成 3-6 个可执行步骤（JSON）
    act    逐步执行：模型可调用工具，最多 12 轮工具调用/步
    review 判定目标是否达成，未达成就回到 act 继续
    finish 汇总产出（Markdown）

设计约束：
    - LangGraph 是可选依赖：缺失时自动降级为等价顺序循环，行为一致
      （打包环境若剔除 langgraph 也不会 500）
    - 所有 LLM 调用与工具执行都通过 ctx 注入，本模块不直接持有 Provider
"""
import json
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, TypedDict

try:                                            # pragma: no cover
    from langgraph.graph import StateGraph, START, END
    HAS_LANGGRAPH = True
except Exception:                               # pragma: no cover
    StateGraph = None
    START = END = None
    HAS_LANGGRAPH = False

#: 计划步数上限。config.max_steps 默认 12，此前被 clamp 到 5（潜在 bug）；
#: PPT 生成这类任务的计划轻松超过 5 步，抬到与配置默认一致。
MAX_STEPS = 12
#: 单步内「模型→工具」轮次上限。3 轮对真实任务（读模板→生成→转换→校验）
#: 远远不够，抬到 12；整体仍受 skill_timeout（默认 240s）墙钟约束。
MAX_TOOL_ROUNDS_PER_STEP = 12
MAX_SKILL_DEPTH = 2

_PLAN_PROMPT = """你是任务规划器。请把用户任务拆成 3~12 个可执行的步骤（每步一句话，
动宾结构，可通过工具完成）。只输出 JSON 数组，不要任何解释：
["步骤1", "步骤2", ...]"""

_REVIEW_PROMPT = """你在检查任务是否已经完成。
用户任务：{task}
已执行步骤与观察结果：
{history}
如果任务目标已达成，输出 DONE；否则输出 NEXT: 下一步要做什么（一句话）。
只输出 DONE 或 NEXT: ..."""

_FINISH_PROMPT = """请基于以下过程，用中文 Markdown 输出最终结果，直接给用户看：
用户任务：{task}
技能：{skill}
过程与观察：
{history}
要求：结论先行，必要时用表格/列表；不要写「我调用了什么工具」这类过程叙述。"""


class SkillState(TypedDict, total=False):
    task: str
    skill_name: str
    instructions: str
    plan: List[str]
    step: int
    observations: List[Dict[str, Any]]
    answer: str
    done: bool
    error: str


# ----------------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------------
def _extract_json_array(text: str) -> List[str]:
    """从模型输出里抠出 JSON 字符串数组。"""
    if not text:
        return []
    m = re.search(r"\[[\s\S]*?\]", text)
    if not m:
        # 退化：按行拆步骤
        lines = [re.sub(r"^\s*[-*\d.、)）]+\s*", "", ln).strip()
                 for ln in text.splitlines()]
        return [ln for ln in lines if 4 <= len(ln) <= 160][:MAX_STEPS]
    try:
        data = json.loads(m.group(0))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()][:MAX_STEPS]
    except Exception:
        pass
    return []


def _history_text(obs: List[Dict[str, Any]], limit: int = 6000) -> str:
    lines = []
    for i, o in enumerate(obs, 1):
        step = o.get("step") or i
        kind = o.get("kind") or "note"
        text = str(o.get("text") or "")[:1200]
        lines.append(f"[{step}] <{kind}> {text}")
    out = "\n".join(lines)
    return out[-limit:]


def _filter_tools(registry, allowed: List[str]) -> List[str]:
    """技能工具白名单过滤；白名单为空表示不限制（除技能嵌套工具）。"""
    names = [n for n in registry.names() if n not in ("skill_run", "skill_list")]
    if not allowed:
        return names
    allow = set(allowed)
    return [n for n in names if n in allow or n.startswith("mcp__")]


# ----------------------------------------------------------------------------
# 节点实现（两个执行器共享）
# ----------------------------------------------------------------------------
class _Runner:
    """一次技能运行的可变上下文。"""

    def __init__(self, skill: Dict[str, Any], task: str, ctx: Dict[str, Any]):
        self.skill = skill
        self.task = task or ""
        self.ctx = ctx or {}
        self.registry = self.ctx.get("registry")
        self.provider = self.ctx.get("provider")
        self.model = self.ctx.get("model") or ""
        self.emit = self.ctx.get("emit") or (lambda *a, **k: None)
        self.allowed = _filter_tools(self.registry, skill["meta"].get("tools") or [])
        self.max_steps = min(int(self.ctx.get("max_steps") or MAX_STEPS), MAX_STEPS)
        # 技能整体时限：本地小模型单个 LLM 调用就要 30-60s，不设上限会一直转，
        # 超时后强制进入 finish 收尾（已收集的观察仍然会汇总）。
        try:
            timeout = float(self.ctx.get("skill_timeout") or 240)
        except (TypeError, ValueError):
            timeout = 240.0
        self.deadline = time.time() + max(30.0, timeout)

    # ---------------- 事件 ----------------
    def _ev(self, kind: str, fields: Dict[str, Any]) -> None:
        """统一事件格式：{"type": kind, ...fields}，emit 异常不影响执行。"""
        ev = {"type": kind}
        ev.update(fields or {})
        try:
            self.emit(ev)
        except Exception:
            pass

    # ---------------- LLM ----------------
    def llm(self, messages: List[Dict[str, Any]],
            tools: Optional[List[Dict[str, Any]]] = None,
            on_delta: Optional[Callable[[str, str], None]] = None):
        if self.provider is None:
            raise RuntimeError("未配置模型提供商")
        return self.provider.chat(
            messages, tools=tools, model=self.model,
            temperature=float(self.ctx.get("temperature") or 0.3),
            max_tokens=int(self.ctx.get("max_tokens") or 2048),
            on_delta=on_delta, cancel_check=self.ctx.get("cancel_check"))

    def tool_schemas(self) -> List[Dict[str, Any]]:
        all_schemas = self.registry.openai_schemas()
        return [s for s in all_schemas
                if s["function"]["name"] in set(self.allowed)]

    # ---------------- 节点：plan ----------------
    def node_plan(self, state: SkillState) -> Dict[str, Any]:
        self._ev("skill_step", {"phase": "plan", "skill": self.skill["slug"],
                                 "detail": "正在拆解任务…"})
        sys = (f"你在执行技能「{self.skill['meta'].get('name')}」。\n"
               f"技能说明：{self.skill['meta'].get('description', '')}\n"
               f"技能要求：\n{self.skill['body']}\n")
        msgs = [{"role": "system", "content": sys},
                {"role": "user", "content": _PLAN_PROMPT}]
        try:
            reply = self.llm(msgs)
            plan = _extract_json_array(reply.content)
        except Exception as e:
            return {"plan": [], "error": f"规划失败：{e}"}
        if not plan:
            plan = [self.task or "完成技能要求"]
        self._ev("skill_step", {"phase": "plan", "skill": self.skill["slug"],
                                 "detail": "计划：" + " / ".join(plan), "plan": plan})
        return {"plan": plan[: self.max_steps], "step": 0, "observations": []}

    def _expired(self) -> bool:
        return time.time() > self.deadline

    # ---------------- 节点：act ----------------
    def node_act(self, state: SkillState) -> Dict[str, Any]:
        if self._expired():
            obs = list(state.get("observations") or [])
            obs.append({"step": int(state.get("step") or 0) + 1, "kind": "note",
                        "text": "技能执行超时，提前收尾"})
            return {"done": True, "observations": obs}
        idx = int(state.get("step") or 0)
        plan = state.get("plan") or []
        if idx >= len(plan):
            return {"done": True}
        step_text = plan[idx]
        self._ev("skill_step", {"phase": "act", "skill": self.skill["slug"],
                                 "detail": f"第 {idx + 1} 步：{step_text}", "index": idx})
        obs = list(state.get("observations") or [])
        msgs: List[Dict[str, Any]] = [
            {"role": "system", "content": (
                f"你在执行技能「{self.skill['meta'].get('name')}」的第 {idx + 1} 步。\n"
                f"技能要求：\n{self.skill['body']}\n"
                "规则：需要真实信息时必须调用工具，不要臆造；每步尽量少而准。\n"
                "本步完成后用一两句话说明结果。")},
            {"role": "user", "content": (
                f"总任务：{self.task}\n完整计划：{json.dumps(plan, ensure_ascii=False)}\n"
                f"当前步骤：{step_text}\n此前的观察：\n{_history_text(obs, 3000) or '（无）'}")},
        ]
        tools = self.tool_schemas()
        for _round in range(MAX_TOOL_ROUNDS_PER_STEP):
            if self._expired():
                obs.append({"step": idx + 1, "kind": "note", "text": "本步超时，跳过剩余工具调用"})
                break
            if self.ctx.get("cancel_check") and self.ctx["cancel_check"]():
                return {"done": True, "error": "已取消"}
            reply = self.llm(msgs, tools=tools or None)
            if not reply.ok:
                obs.append({"step": idx + 1, "kind": "error", "text": reply.error})
                break
            if not reply.tool_calls:
                if reply.content.strip():
                    msgs.append({"role": "assistant", "content": reply.content})
                    obs.append({"step": idx + 1, "kind": "note", "text": reply.content})
                break
            # 执行本轮工具调用
            msgs.append({"role": "assistant", "content": reply.content or "",
                         "tool_calls": [
                             {"id": tc["id"], "type": "function",
                              "function": {"name": tc["name"],
                                           "arguments": tc["arguments"] or "{}"}}
                             for tc in reply.tool_calls]})
            for tc in reply.tool_calls:
                if tc["name"] not in self.allowed and not tc["name"].startswith("mcp__"):
                    result_text = f"工具 {tc['name']} 不在该技能白名单内"
                    ok = False
                else:
                    ok, result_text = self.ctx["exec_tool"](
                        tc["name"], tc["arguments"], source="skill")
                obs.append({"step": idx + 1, "kind": "tool",
                            "text": f"{tc['name']} → {result_text[:800]}", "ok": ok})
                msgs.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": result_text})
        return {"step": idx + 1, "observations": obs}

    # ---------------- 节点：review ----------------
    def node_review(self, state: SkillState) -> Dict[str, Any]:
        plan = state.get("plan") or []
        idx = int(state.get("step") or 0)
        if idx >= len(plan):
            return {"done": True}
        self._ev("skill_step", {"phase": "review", "skill": self.skill["slug"],
                                 "detail": "检查完成度…"})
        msgs = [{"role": "system", "content": _REVIEW_PROMPT.format(
            task=self.task, history=_history_text(state.get("observations") or []))}]
        try:
            reply = self.llm(msgs)
        except Exception:
            return {"done": idx >= len(plan)}
        text = (reply.content or "").strip()
        if text.upper().startswith("DONE"):
            return {"done": True}
        if idx + 1 >= self.max_steps:
            return {"done": True}
        # NEXT: 追加一个补充步骤
        m = re.match(r"NEXT\s*[:：]\s*(.+)", text, re.I)
        if m:
            extra = m.group(1).strip()[:160]
            plan = plan + [extra]
            self._ev("skill_step", {"phase": "review", "skill": self.skill["slug"],
                                     "detail": f"追加步骤：{extra}"})
            return {"plan": plan, "done": False}
        return {"done": False}

    # ---------------- 节点：finish ----------------
    def node_finish(self, state: SkillState) -> Dict[str, Any]:
        self._ev("skill_step", {"phase": "finish", "skill": self.skill["slug"],
                                "detail": "汇总结果…"})
        obs = state.get("observations") or []
        failures = [o for o in obs
                    if o.get("kind") == "error"
                    or (o.get("kind") == "tool" and not o.get("ok"))]
        has_data = any(o.get("kind") == "tool" and o.get("ok") for o in obs)
        if failures and not has_data:
            # 一次工具都没成功（限流/断网/审批被拒）时，禁止让模型「编」结果，
            # 直接如实汇报失败原因——比一张编造的归档清单安全得多。
            first = str(failures[0].get("text") or "")[:300]
            return {"answer": (f"技能「{self.skill['meta'].get('name')}」没有执行成功："
                               f"{first}\n\n可以稍后重试，或把任务拆小一些。"),
                    "done": True,
                    "error": f"技能未成功执行：{first}"}
        msgs = [{"role": "system", "content": _FINISH_PROMPT.format(
            task=self.task, skill=self.skill["meta"].get("name"),
            history=_history_text(obs))}]
        try:
            reply = self.llm(msgs)
            answer = reply.content.strip()
        except Exception as e:
            answer = ""
            err = str(e)
        else:
            err = ""
        if not answer:
            # 兜底：把观察压成清单
            answer = "技能执行完成，过程摘要：\n" + (
                _history_text(state.get("observations") or []) or "（无输出）")
        return {"answer": answer, "done": True, "error": err or state.get("error", "")}


# ----------------------------------------------------------------------------
# LangGraph 图（模块级构建一次）
# ----------------------------------------------------------------------------
_GRAPH = None
_GRAPH_LOCK = threading.Lock()


def _build_graph():
    """构建 plan → act → review ↺ / finish 图（每次运行换 Runner，故用闭包传入）。"""
    graph = StateGraph(SkillState)
    # 节点用占位实现，真正逻辑在 run_skill 里通过 config 注入 runner
    graph.add_node("plan", lambda s, config: config["configurable"]["runner"].node_plan(s))
    graph.add_node("act", lambda s, config: config["configurable"]["runner"].node_act(s))
    graph.add_node("review", lambda s, config: config["configurable"]["runner"].node_review(s))
    graph.add_node("finish", lambda s, config: config["configurable"]["runner"].node_finish(s))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "act")
    graph.add_edge("act", "review")

    def _next(state: SkillState) -> str:
        if state.get("done"):
            return "finish"
        if int(state.get("step") or 0) >= MAX_STEPS:
            return "finish"
        return "act"

    graph.add_conditional_edges("review", _next, {"act": "act", "finish": "finish"})
    graph.add_edge("finish", END)
    return graph.compile()


def get_graph():
    global _GRAPH
    if not HAS_LANGGRAPH:
        return None
    if _GRAPH is None:
        with _GRAPH_LOCK:
            if _GRAPH is None:
                _GRAPH = _build_graph()
    return _GRAPH


# ----------------------------------------------------------------------------
# 对外入口
# ----------------------------------------------------------------------------
def run_skill(skill: Dict[str, Any], task: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """执行一个技能，返回 {ok, answer, steps, observations, engine, error}。"""
    ctx = dict(ctx or {})
    depth = int(ctx.get("depth") or 0)
    if depth >= MAX_SKILL_DEPTH:
        return {"ok": False, "answer": "", "error": "技能嵌套层级过深（≥2）"}
    if ctx.get("registry") is None or ctx.get("provider") is None:
        return {"ok": False, "answer": "", "error": "技能运行缺少 registry/provider 上下文"}

    runner = _Runner(skill, task, ctx)
    t0 = time.time()
    init: SkillState = {"task": task, "skill_name": skill["slug"],
                        "instructions": skill["body"], "plan": [], "step": 0,
                        "observations": [], "answer": "", "done": False, "error": ""}
    engine = "sequential"
    final: Dict[str, Any] = dict(init)

    graph = get_graph()
    if graph is not None and skill["meta"].get("engine", "langgraph") != "sequential":
        engine = "langgraph"
        try:
            final = graph.invoke(init, config={"configurable": {"runner": runner},
                                              "recursion_limit": 60})
        except Exception as e:
            runner._ev("skill_step", {"phase": "error", "skill": skill["slug"],
                                       "detail": f"LangGraph 执行异常，降级顺序执行：{e}"})
            engine = "sequential-fallback"
            final = _run_sequential(runner, init)
    else:
        final = _run_sequential(runner, init)

    answer = str(final.get("answer") or "").strip()
    err = str(final.get("error") or "")
    return {
        "ok": bool(answer) and not err,
        "answer": answer,
        "steps": final.get("plan") or [],
        "observations": final.get("observations") or [],
        "engine": engine,
        "elapsed": round(time.time() - t0, 1),
        "error": err,
    }


def _run_sequential(runner: _Runner, state: SkillState) -> SkillState:
    """无 LangGraph 时的等价流程：plan → (act → review)* → finish。"""
    st = dict(state)
    st.update(runner.node_plan(st))
    guard = 0
    while guard < MAX_STEPS * 2:
        guard += 1
        if st.get("done"):
            break
        st.update(runner.node_act(st))
        st.update(runner.node_review(st))
    st.update(runner.node_finish(st))
    return st
