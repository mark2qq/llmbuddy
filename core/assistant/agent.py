# -*- coding: utf-8 -*-
"""
会话级 Agent 循环（core/assistant/agent.py）。

一轮对话的完整链路：

    user message
        ↓ 组装 system(工具清单 + 工作区 + 审批策略) + 历史
    provider.chat(stream + tools)
        ↓ 原生 tool_calls 或文本内 ```tool_call``` 兜底协议
    审批（approval.py，按 policy 判定，必要时挂起等人工）
        ↓
    registry.call(...)  →  工具结果回填 messages  →  继续下一轮
        ↓ 无工具调用即完成
    done 事件 + 落盘会话

对外：
    Assistant.state()            运行态快照（提供商/工具/技能/MCP/策略）
    Assistant.start_turn(...)    阻塞执行一轮，通过 emit 回调推事件
    Assistant.stop(session_id)   取消正在跑的轮次
"""
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from core.assistant import mcp as mcp_mod
from core.assistant import profiles as profiles_mod
from core.assistant import skills as skills_mod
from core.assistant import tools as tools_mod
from core.assistant.approval import get_approval_manager
from core.assistant.providers import MAX_TOKENS_CAP, ProviderError, get_provider
from core.assistant.store import (audit, delete_session, ensure_dirs, get_provider as cfg_provider,
                                 list_sessions, load_config, load_session, norm_workspace,
                                 save_config, save_session, space_list as group_spaces,
                                 update_session as patch_session)
from core.assistant.tools import ToolRegistry

MAX_HISTORY_MESSAGES = 24
#: 单轮对话「模型→工具→模型」循环轮次上限。**0 = 不限制**（默认）。
#: 本地小模型跑多步任务（如从 GitHub 列目录→取 SKILL.md→取附属文件→安装→
#: 验证）轻松超过十几轮，写死小值会把任务中途掐断（历史上 12 次上限就掐过
#: 「下载安装 PPT Master 技能」）。不设上限后，真正的兜底是
#: MAX_TURN_SECONDS 墙钟 + 用户随时点「停止」。
DEFAULT_MAX_TOOL_ROUNDS = 0
#: 单轮自动挂载的技能上限（profile.skills + mounted_skills 合并后）
MAX_AUTO_SKILLS = 5
#: 未命中 profile 时写进 system 的可用技能条数上限
MAX_SKILL_HINTS = 20
# 单轮总时限（秒）：本地小模型单次调用就可能 30-60s，技能再放大若干倍，
# 不设上限时一次「卡住」会把会话挂十几分钟，这里兜底强制收尾。
MAX_TURN_SECONDS = 900

POLICY_TEXT = {
    "readonly_auto": "只读操作自动执行；写入、删除、执行命令、外发网络需用户批准",
    "ask_all": "任何工具调用都需要用户批准",
    "yolo": "全部自动执行（仅留审计记录）",
}

# 文本内工具调用兜底协议：```tool_call {...} ``` / <tool_call>{...}</tool_call>
_FENCE_RE = re.compile(r"```(?:tool_call|tool|json)\s*\n(.*?)```", re.S | re.I)
_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S | re.I)

SYSTEM_PROMPT = """你是 llmBuddy 的本地智能助理，直接运行在用户自己的 Windows 电脑上。
你可以操作本机文件、执行命令、抓取网页、调用 MCP 工具，还能运行技能包。

当前工作区：{workspace}
审批策略：{policy_text}
当前时间：{now}

## 可用工具
{tool_list}

## 行为准则
1. 需要事实（目录结构、文件内容、网页信息）必须先调用工具，禁止凭印象编造。
2. 写入/删除/执行命令类动作会触发人工审批，可能被拒绝；被拒后换方案，不要重复同一动作。
3. 一次可并行发起多个工具调用（建议不超过 8 个），拿到全部结果再决定下一步；任务完成就直接回答，不要再调工具。
4. 回答使用简体中文，结论先行，必要时用列表或表格；不要叙述工具调用过程。
5. 用户问「你能做什么」时，用一段话说明能力，并主动提议一个具体动作。

## 工具调用格式
优先使用原生 function calling。若你的运行时不支持，请只输出下面这种代码块（不要附其它文字）：
```tool_call
{{"name": "fs_list", "arguments": {{"path": "."}}}}
```
"""


class Assistant:
    """全局单例：持有 registry / provider / 会话 / 取消标记。"""

    def __init__(self) -> None:
        ensure_dirs()
        skills_mod.ensure_builtin_skills()
        profiles_mod.ensure_builtin_profiles()
        self._lock = threading.RLock()
        self._registry: Optional[ToolRegistry] = None
        self._registry_ws = ""
        self._cancel: Dict[str, bool] = {}
        self._running: Dict[str, bool] = {}
        self._mcp = mcp_mod.get_mcp_manager()
        self._approval = get_approval_manager()
        self._mcp_autostart_done = False

    # ------------------------------------------------------------------ #
    # 运行态
    # ------------------------------------------------------------------ #
    def registry(self) -> ToolRegistry:
        cfg = load_config()
        ws = cfg.get("workspace") or ""
        with self._lock:
            if self._registry is None or self._registry_ws != ws:
                self._registry = ToolRegistry(ws)
                self._registry_ws = ws
                # 工作区变了：把已连接的 MCP 工具重新注册到新 registry
                self._mcp.reregister(self._registry)
            return self._registry

    def state(self) -> Dict[str, Any]:
        cfg = load_config()
        self._maybe_autostart_mcp()
        # 一次读盘，任务列表与空间分组共用（左栏要同时渲染「任务」和「空间」两区）
        rows = list_sessions(500)
        return {
            "version": "1.0.8",
            "policy": cfg.get("policy"),
            "policies": [{"id": k, "label": v} for k, v in POLICY_TEXT.items()],
            "workspace": cfg.get("workspace"),
            "workspace_error": cfg.get("workspace_error") or "",
            "active_provider": cfg.get("active_provider"),
            "providers": [{"id": p.get("id"), "label": p.get("label"),
                           "kind": p.get("kind"), "enabled": p.get("enabled"),
                           "base_url": p.get("base_url"), "model": p.get("model"),
                           # 密钥不下发，只告知是否已配置
                           "has_key": bool(p.get("api_key"))}
                          for p in cfg.get("providers") or []],
            "tools": self.registry().list(),
            "skills": skills_mod.list_skills(),
            "mcp": self._mcp.status(),
            "sessions": rows[:50],
            "spaces": group_spaces(rows, fallback=cfg.get("workspace") or ""),
            "pending_approvals": self._approval.list_pending(),
            "orchestrator": "langgraph" if _has_langgraph() else "sequential",
            "running": list(k for k, v in self._running.items() if v),
        }

    def _maybe_autostart_mcp(self) -> None:
        """首次状态查询时自动连接 enabled 的 MCP 服务器（失败不阻塞）。"""
        if self._mcp_autostart_done:
            return
        self._mcp_autostart_done = True
        for s in self._mcp.servers():
            if not s.get("enabled", True):
                continue
            try:
                self._mcp.connect(s.get("id"), self.registry())
            except Exception:
                continue

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def new_session(self, title: str = "", workspace: str = "") -> Dict[str, Any]:
        """新建任务。workspace 为空时归属当前工作区（空间 = 工作区目录）。"""
        sid = "s" + uuid.uuid4().hex[:12]
        now = time.time()
        cur_ws = norm_workspace(workspace) or norm_workspace(load_config().get("workspace"))
        session = {"id": sid, "title": title or "新任务", "created_at": now,
                   "updated_at": now, "messages": [], "provider": "", "model": "",
                   "workspace": cur_ws, "pinned": False, "archived": False}
        save_session(session)
        return session

    def get_session(self, sid: str) -> Optional[Dict[str, Any]]:
        return load_session(sid)

    def session_list(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        return list_sessions(50, include_archived)

    def space_list(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        """按空间（工作区）分组的任务列表。

        历史任务没有 workspace 字段（1.0.8 之前），统一归到当前工作区名下，
        否则左栏会多出一个「未归属」组，看着像数据丢了。
        """
        cfg = load_config()
        rows = list_sessions(500, include_archived)
        return group_spaces(rows, fallback=cfg.get("workspace") or "")

    def update_session(self, sid: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        s = patch_session(sid, patch or {})
        if s is not None and patch.get("archived"):
            self._approval.cancel_session(sid)
        return s

    def remove_session(self, sid: str) -> bool:
        self._approval.cancel_session(sid)
        return delete_session(sid)

    # ------------------------------------------------------------------ #
    # 轮次执行
    # ------------------------------------------------------------------ #
    def stop(self, session_id: str) -> None:
        with self._lock:
            self._cancel[session_id] = True
        self._approval.cancel_session(session_id)

    def _cancelled(self, sid: str) -> bool:
        with self._lock:
            return bool(self._cancel.get(sid))

    def start_turn(self, session_id: str, text: str, provider_id: str = "",
                   model: str = "", emit: Optional[Callable[[Dict], None]] = None,
                   mounted_skills: Optional[List[str]] = None) -> Dict[str, Any]:
        """执行一轮对话（阻塞）。事件通过 emit(dict) 实时推送。

        `mounted_skills` 是**尾参且带默认值**：既有位置参数调用（测试、路由的
        非流式/流式两条路径）完全不受影响（架构 §5 A4）。传的是「用户在底栏
        挂载的技能 slug」，落库的 message 仍是**原文**（不再拼「用 xx 技能：」）。
        """
        emit = emit or (lambda ev: None)
        cfg = load_config()
        registry = self.registry()
        provider_cfg = cfg_provider(cfg, provider_id)
        provider = get_provider(provider_cfg)

        session = load_session(session_id) or self.new_session()
        session["id"] = session.get("id") or session_id
        session.setdefault("messages", [])
        # 老任务没有所属空间：补记当前工作区，左栏「空间」区才能把它归位
        if not session.get("workspace"):
            session["workspace"] = norm_workspace(cfg.get("workspace"))
        with self._lock:
            self._cancel[session_id] = False
            self._running[session_id] = True

        if not text.strip():
            emit({"type": "error", "message": "消息不能为空"})
            with self._lock:
                self._running[session_id] = False
            return {"ok": False, "error": "empty"}

        mounted = [str(s) for s in (mounted_skills or []) if str(s).strip()]

        # ---- 按需求自动加载（架构 §5 A3 + A4，PRD §5.4）----
        profile = self._match_profile(cfg, text, registry)
        auto_loaded = self._auto_loaded_payload(profile, registry, cfg)

        # 记录用户消息（原文，无前缀）
        session["messages"].append({"role": "user", "content": text,
                                    "time": time.time()})
        # 占位标题（新建时还没说话）才自动用首句当标题；用户改过名的绝不动
        if session.get("title") in ("", "新会话", "新任务", "未命名任务"):
            session["title"] = text.strip().replace("\n", " ")[:24]
            emit({"type": "title", "title": session["title"]})
        emit({"type": "start", "session_id": session_id,
              "provider": provider_cfg.get("id"), "label": provider_cfg.get("label"),
              "auto_loaded": auto_loaded})

        # ---- 同源（A3）：schemas 只算一次，provider 与系统提示共用同一份 ----
        schemas = self._select_schemas(cfg, registry, profile)
        messages = self._build_messages(session, registry, cfg, schemas,
                                        mounted_skills=mounted, profile=profile)
        final_text = ""
        error = ""
        rounds = 0
        model_name = ""

        try:
            model_name = provider.resolve_model(model or provider_cfg.get("model") or "")
            emit({"type": "model", "model": model_name,
                  "provider": provider_cfg.get("id")})
        except ProviderError as e:
            emit({"type": "error", "message": str(e)})
            with self._lock:
                self._running[session_id] = False
            return {"ok": False, "error": str(e)}

        def on_delta(channel: str, piece: str) -> None:
            if channel == "reset":
                # provider 因空正文自动放大 max_tokens 重试：先让前端清空半截内容
                emit({"type": "reset"})
                return
            emit({"type": "reasoning" if channel == "reasoning" else "delta",
                  "text": piece})

        def exec_tool(name: str, arguments: Any, source: str = "agent",
                      index: Optional[int] = None):
            """审批 + 执行，返回 (ok, text)。供本模块与 orchestrator 共用。

            index 用于并行执行时给事件带上调用序号，前端据此把 tool_result
            精确回填到对应的工具卡片，避免同名/乱序时串卡。
            """
            args = arguments
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except Exception:
                    args = {"_raw": args}
            if not isinstance(args, dict):
                args = {"_value": args}
            risk = registry.risk_of(name, args)
            summary = registry.describe_call(name, args)
            emit({"type": "tool_call", "name": name, "args": args,
                  "risk": risk, "summary": summary, "source": source,
                  "index": index})
            # 审批（按 policy；readonly_auto 下 low 直接过）
            pending = self._approval.request(
                session_id, name, args, risk,
                reason=summary, policy=cfg.get("policy"),
                timeout=int(cfg.get("approval_timeout") or 300))
            if pending.status != "approved":
                emit({"type": "approval_result", "id": pending.id,
                      "approve": False, "status": pending.status, "index": index})
                return False, (f"用户未批准该操作（{pending.status}），"
                               f"请换一种方式或询问用户。")
            emit({"type": "approval_result", "id": pending.id, "approve": True,
                  "status": "approved", "index": index})
            res = registry.call(name, args, {**self._ctx(session_id, cfg, provider,
                                                         model_name, emit, exec_tool)})
            emit({"type": "tool_result", "name": name, "ok": res.ok,
                  "content": (res.content or res.error)[:4000], "error": res.error,
                  "index": index})
            audit("tool_called", session_id=session_id, tool=name, risk=risk,
                  ok=res.ok, args=args)
            return res.ok, (res.content if res.ok else f"错误：{res.error}")

        # 审批挂起时向前端推卡片（只推本会话的，避免并发会话串卡）
        def _on_approval_created(p) -> None:
            if p.session_id != session_id:
                return
            emit({"type": "approval_request", "id": p.id, "tool": p.tool,
                  "args": p.args, "risk": p.risk, "reason": p.reason})
        self._approval.on_created = _on_approval_created

        try:
            turn_started = time.time()
            # 工具轮次上限：config.max_tool_rounds，0/缺省 = 不限制
            # （仅受 MAX_TURN_SECONDS 墙钟与用户「停止」约束）。
            try:
                max_rounds = int(cfg.get("max_tool_rounds")
                                 or DEFAULT_MAX_TOOL_ROUNDS)
            except (TypeError, ValueError):
                max_rounds = DEFAULT_MAX_TOOL_ROUNDS
            max_rounds = max(0, max_rounds)
            while max_rounds <= 0 or rounds < max_rounds:
                if self._cancelled(session_id):
                    emit({"type": "stopped"})
                    break
                if time.time() - turn_started > MAX_TURN_SECONDS:
                    error = (f"本轮已超过 {MAX_TURN_SECONDS // 60} 分钟，已中止。"
                             "可换用更大的模型/云端模型，或把任务拆小一些。")
                    emit({"type": "error", "message": error})
                    break
                rounds += 1
                reply = provider.chat(
                    messages, tools=schemas, model=model_name,
                    temperature=float(cfg.get("temperature") or 0.3),
                    max_tokens=int(cfg.get("max_tokens") or 2048),
                    on_delta=on_delta, cancel_check=lambda: self._cancelled(session_id))
                if not reply.ok:
                    error = reply.error
                    emit({"type": "error", "message": reply.error})
                    break
                if getattr(reply, "note", ""):
                    # provider 自愈记录（如自动放大 max_tokens 重试）
                    emit({"type": "note", "message": str(reply.note)})
                if self._cancelled(session_id):
                    # 用户在流式过程中点了停止：provider 会以 cancelled 结束，
                    # 这里不要报「空回复」错误，按正常停止处理
                    emit({"type": "stopped"})
                    break

                calls = list(reply.tool_calls or [])
                if not calls:
                    parsed, cleaned = self._parse_text_calls(reply.content)
                    if parsed:
                        calls = parsed
                        if cleaned != reply.content:
                            emit({"type": "replace", "text": cleaned.strip()})

                if not calls:
                    final_text = reply.content.strip()
                    if not final_text:
                        # 思考型模型可能把 max_tokens 全用在思考上：provider 已自动
                        # 放大预算重试过一次（providers.chat），仍为空才报这里。
                        error = ("模型没有返回正文：本轮 max_tokens 已被自动放大到上限"
                                 f"{MAX_TOKENS_CAP} 重试仍为空"
                                 f"（finish_reason={reply.finish_reason or '未知'}）。"
                                 "请换用更大的模型，或把任务拆小一些。")
                        emit({"type": "error", "message": error})
                        break
                    if reply.fallback_reasoning:
                        emit({"type": "note", "message":
                              "模型本轮只输出了思考内容、没有产出正文，下面是思考原文；"
                              "建议调大 max_tokens 或换用更大的模型。"})
                    emit({"type": "done", "content": final_text,
                          "stopped": reply.finish_reason == "cancelled"})
                    break

                # 有工具调用：把本轮 assistant 消息（含调用）加入上下文
                messages.append({"role": "assistant",
                                 "content": reply.content or "",
                                 "tool_calls": [
                                     {"id": c.get("id") or f"call_{i}",
                                      "type": "function",
                                      "function": {"name": c["name"],
                                                   "arguments": self._args_json(c)}}
                                     for i, c in enumerate(calls)]})

                # 并行执行多个工具（WorkBuddy 对标：单轮最多 8 个同时跑，超出分批）。
                # 结果按调用顺序回填 messages，保证上下文不乱序；取消语义保持
                # 「本轮完成后停止」：提交前查一次、全部完成后查一次。
                MAX_PARALLEL = 8
                if self._cancelled(session_id):
                    emit({"type": "stopped"})
                    break
                results: List[Optional[tuple]] = [None] * len(calls)
                aborted = False
                for start in range(0, len(calls), MAX_PARALLEL):
                    if self._cancelled(session_id):
                        aborted = True
                        break
                    batch = calls[start:start + MAX_PARALLEL]
                    with ThreadPoolExecutor(
                            max_workers=min(MAX_PARALLEL, len(batch))) as ex:
                        fut_to_idx = {
                            ex.submit(exec_tool, c["name"], c.get("arguments"),
                                      "agent", start + j): start + j
                            for j, c in enumerate(batch)}
                        for fut in fut_to_idx:
                            # 等本批全部完成；异常向上抛到外层 try（与串行一致）
                            results[fut_to_idx[fut]] = fut.result()
                    # 本批全部完成后再统一按调用顺序回填工具结果（保序）
                    for j, c in enumerate(batch):
                        idx = start + j
                        ok, result_text = results[idx] or (False, "（工具未返回结果）")
                        messages.append({"role": "tool",
                                         "tool_call_id": c.get("id") or f"call_{idx}",
                                         "content": result_text})
                    if self._cancelled(session_id):
                        aborted = True
                        break
                if aborted:
                    emit({"type": "stopped"})
                    break
            else:
                # 仅在设置了正数上限时才会走到这里（0 = 不限制，循环不会因条件退出）
                emit({"type": "error",
                      "message": f"已达到单轮工具调用上限（{max_rounds} 次），已中止"})
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            emit({"type": "error", "message": error})
        finally:
            with self._lock:
                self._running[session_id] = False
                self._cancel.pop(session_id, None)

        session["messages"].append({"role": "assistant", "content": final_text,
                                    "time": time.time(), "provider": provider_cfg.get("id"),
                                    "model": model_name})
        session["updated_at"] = time.time()
        session["provider"] = provider_cfg.get("id")
        session["model"] = model_name
        save_session(session)
        emit({"type": "end", "content": final_text, "error": error})
        return {"ok": bool(final_text) and not error, "content": final_text,
                "error": error, "model": model_name}

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #
    def _ctx(self, session_id: str, cfg: Dict[str, Any], provider,
             model: str, emit, exec_tool) -> Dict[str, Any]:
        # depth=0：Agent 直接调 skill_run 计为第 1 层（tools._t_skill_run 会 +1），
        # 技能内部再调技能到第 2 层即被拒绝，避免无限递归。
        return {"session_id": session_id, "registry": self.registry(),
                "provider": provider, "model": model, "emit": emit,
                "exec_tool": exec_tool, "policy": cfg.get("policy"),
                "temperature": cfg.get("temperature"),
                "max_tokens": cfg.get("max_tokens"),
                "max_steps": cfg.get("max_steps"),
                "skill_timeout": cfg.get("skill_timeout"),
                "depth": 0, "cancel_check": lambda: self._cancelled(session_id)}

    @staticmethod
    def _args_json(call: Dict[str, Any]) -> str:
        args = call.get("arguments")
        if isinstance(args, str):
            return args or "{}"
        try:
            return json.dumps(args or {}, ensure_ascii=False)
        except Exception:
            return "{}"

    def _parse_text_calls(self, content: str):
        """文本内工具调用兜底：返回 (calls, 清理后的正文)。"""
        text = content or ""
        found: List[Dict[str, Any]] = []
        spans: List[tuple] = []
        for rx in (_FENCE_RE, _TAG_RE):
            for m in rx.finditer(text):
                raw = (m.group(1) or "").strip()
                obj = _loads_jsonish(raw)
                if isinstance(obj, dict) and obj.get("name"):
                    found.append({"id": f"txt_{len(found)}", "name": obj["name"],
                                  "arguments": obj.get("arguments") or obj.get("args") or {}})
                    spans.append(m.span())
                elif isinstance(obj, list):
                    for it in obj:
                        if isinstance(it, dict) and it.get("name"):
                            found.append({"id": f"txt_{len(found)}", "name": it["name"],
                                          "arguments": it.get("arguments") or {}})
                    spans.append(m.span())
        cleaned = text
        for start, end in sorted(spans, reverse=True):
            cleaned = cleaned[:start] + cleaned[end:]
        return found[:8], cleaned

    # ------------------------------------------------------------------ #
    # 自动加载 / 同源（1.0.8 增量）
    # ------------------------------------------------------------------ #
    def _match_profile(self, cfg: Dict[str, Any], text: str,
                       registry: ToolRegistry) -> Optional[Dict[str, Any]]:
        """按需求匹配 profile（纯本地关键词，无网络）。

        `auto_load=false` 直接返回 None（用户关了自动加载，一票否决）。
        技能未安装时 `match_profile` 会返回 None（走 L3 降级：把已安装技能
        清单写进 system，模型自行决定 skill_run）。
        """
        if not bool(cfg.get("auto_load", True)):
            return None
        profiles = cfg.get("profiles")
        if not isinstance(profiles, list) or not profiles:
            profiles = profiles_mod.BUILTIN_PROFILES
        try:
            available = [str(s.get("slug") or "") for s in skills_mod.list_skills()
                         if s.get("slug")]
        except Exception:
            available = None
        _ = registry
        return profiles_mod.match_profile(text, profiles, available)

    @staticmethod
    def _auto_loaded_payload(profile: Optional[Dict[str, Any]],
                             registry: ToolRegistry,
                             cfg: Dict[str, Any]) -> Dict[str, Any]:
        """`start.auto_loaded` 的载荷；未命中 -> 空 dict。

        字段名是跨端接缝（任务分解 S3）：`profile / name / skills / tools`。
        `tools` 取**实际下发条数**，与同源裁剪一致，不重复计算。
        """
        if profile is None:
            return {}
        prefs = cfg.get("tool_prefs") if isinstance(cfg.get("tool_prefs"), dict) else {}
        allow = profile.get("tools") or None
        schemas = registry.select_schemas(
            disabled=set(prefs.get("disabled") or []),
            allow=allow,
            max_tools=int(prefs.get("max_tools") or tools_mod.DEFAULT_MAX_TOOLS))
        return {"profile": str(profile.get("id") or ""),
                "name": str(profile.get("name") or ""),
                "skills": [str(s) for s in (profile.get("skills") or [])][:MAX_AUTO_SKILLS],
                "tools": len(schemas)}

    def _select_schemas(self, cfg: Dict[str, Any], registry: ToolRegistry,
                        profile: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """下发的**唯一** schema 列表（A3：provider 与系统提示共用）。

        过滤条件来自 `tool_prefs`；命中 profile 时额外收窄白名单
        （`allow=profile.tools`）。顺带 `set_filter()`，让 `/state` 的
        `enabled` 字段与本次下发口径一致（时序图图 2 第 15 步）。
        """
        prefs = cfg.get("tool_prefs") if isinstance(cfg.get("tool_prefs"), dict) else {}
        disabled = {str(x) for x in (prefs.get("disabled") or [])}
        try:
            max_tools = int(prefs.get("max_tools") or tools_mod.DEFAULT_MAX_TOOLS)
        except (TypeError, ValueError):
            max_tools = tools_mod.DEFAULT_MAX_TOOLS
        allow = (profile.get("tools") or None) if profile else None
        registry.set_filter(disabled=disabled, allow=allow, max_tools=max_tools)
        return registry.select_schemas(disabled=disabled, allow=allow,
                                       max_tools=max_tools)

    def _build_messages(self, session: Dict[str, Any], registry: ToolRegistry,
                        cfg: Dict[str, Any],
                        schemas: Optional[List[Dict[str, Any]]] = None,
                        mounted_skills: Optional[List[str]] = None,
                        profile: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """组装 messages（system + 历史）。

        **工具清单从 `schemas` 渲染**（架构 §5 A3）：`registry.list()` 返回的是
        `Tool.to_dict()`（没有 `function` 键），直接拿它渲染会和下发给模型的
        function schema 不一致——模型会照着提示去调一个 schema 里不存在的工具，
        registry.call 返回「未知工具」，白烧一轮。

        `schemas` 为空时自己取一次 `select_schemas()`（向后兼容其他调用点），
        但 agent 主循环里始终传入同一份对象，保证同源。
        """
        if schemas is None:
            schemas = registry.select_schemas()
        risk_by_name = {str(t["name"]): str(t.get("risk") or "low")
                        for t in registry.list()}
        risk_text = {"low": "只读", "medium": "写入", "high": "高危"}
        tools_desc: List[str] = []
        for s in schemas:
            fn = s.get("function") if isinstance(s, dict) else None
            if not isinstance(fn, dict):
                continue
            name = str(fn.get("name") or "")
            if not name:
                continue
            risk = risk_text.get(risk_by_name.get(name, "low"), "低风险")
            tools_desc.append(f"- {name}（{risk}）：{fn.get('description') or ''}")

        system = SYSTEM_PROMPT.format(
            workspace=cfg.get("workspace"),
            policy_text=POLICY_TEXT.get(cfg.get("policy"), ""),
            now=time.strftime("%Y-%m-%d %H:%M:%S"),
            # 不再 [:80] 截断：max_tools 上限 64 < 80，天然收敛（A3 说明 2）
            tool_list="\n".join(tools_desc) or "（暂无工具）")
        system += self._skills_hint(cfg, profile, mounted_skills)

        msgs: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        history = [m for m in (session.get("messages") or [])
                   if m.get("role") in ("user", "assistant") and m.get("content")]
        for m in history[-MAX_HISTORY_MESSAGES:]:
            msgs.append({"role": m["role"], "content": m["content"]})
        return msgs

    def _skills_hint(self, cfg: Dict[str, Any],
                     profile: Optional[Dict[str, Any]],
                     mounted_skills: Optional[List[str]] = None) -> str:
        """system 尾部的技能段：命中 profile -> 指定技能；否则 -> 可用技能清单。

        只写进 system，**不进 message 正文**（架构 §5 A4：历史保持干净）。
        """
        mounted = [str(s) for s in (mounted_skills or []) if str(s).strip()]
        if profile is not None:
            merged: List[str] = []
            for slug in list(profile.get("skills") or []) + mounted:
                if slug not in merged:
                    merged.append(slug)
            merged = merged[:MAX_AUTO_SKILLS]
            if not merged:
                return ""
            return ("\n## 本轮指定技能\n用户明确指定使用技能："
                    + "、".join(merged)
                    + "。请优先调用 skill_run 执行它们；"
                      "技能内部的文件写入仍会逐个请求用户确认。\n")
        if mounted:
            return ("\n## 本轮指定技能\n用户明确指定使用技能："
                    + "、".join(mounted[:MAX_AUTO_SKILLS])
                    + "。请优先调用 skill_run 执行它们。\n")
        if not bool(cfg.get("auto_load", True)):
            return ""
        try:
            items = skills_mod.list_skills()[:MAX_SKILL_HINTS]
        except Exception:
            return ""
        if not items:
            return ""
        lines = []
        for s in items:
            desc = str(s.get("description") or "").strip()[:40]
            lines.append(f"- {s.get('name') or s.get('slug')}：{desc}")
        return ("\n## 可用技能\n"
                "以下技能已安装，用户需求与某个技能吻合时可调用 skill_run 执行：\n"
                + "\n".join(lines) + "\n")


def _loads_jsonish(raw: str) -> Any:
    """容错解析：先严格 JSON，再尝试去尾逗号/单引号替换。"""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z_]*\s*", "", raw)
        raw = re.sub(r"```$", "", raw).strip()
    for candidate in (raw, raw.replace("'", '"')):
        try:
            return json.loads(candidate)
        except Exception:
            continue
    return None


def _has_langgraph() -> bool:
    try:
        from core.assistant.orchestrator import HAS_LANGGRAPH
        return bool(HAS_LANGGRAPH)
    except Exception:
        return False


_ASSISTANT: Optional[Assistant] = None
_LOCK = threading.Lock()


def get_assistant() -> Assistant:
    global _ASSISTANT
    if _ASSISTANT is None:
        with _LOCK:
            if _ASSISTANT is None:
                _ASSISTANT = Assistant()
    return _ASSISTANT
