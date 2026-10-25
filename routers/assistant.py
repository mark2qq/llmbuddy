# -*- coding: utf-8 -*-
"""
智能助理 API（routers/assistant.py）。

统一前缀：/api/v1/assistant
鉴权：与管理接口一致（环境变量 LLM_API_KEY 非空时校验 X-API-Key / Bearer）

接口清单：
    GET    /state                  运行态快照（提供商/工具/技能/MCP/策略/任务/空间）
    GET    /sessions               任务列表（?include_archived=1 连归档一起给）
    POST   /sessions               新建任务（可指定 workspace）
    GET    /sessions/{sid}         任务详情（完整消息）
    PATCH  /sessions/{sid}         改任务（标题/所属空间/置顶/归档）
    DELETE /sessions/{sid}         删除任务
    GET    /spaces                 空间列表（任务按工作区目录分组）
    POST   /chat                   一轮对话（SSE 事件流；stream=false 时返回 JSON；
                                   mode=normal/queue/guided/resume，见方法 docstring）
    POST   /stop                   中止当前轮次（并置队列暂停，1.0.9）
    GET    /queue                  引导会话队列快照（?session_id=，1.0.9）
    PATCH  /queue/{id}             编辑排队消息（?session_id=，body {text}，1.0.9）
    DELETE /queue/{id}             删除排队消息（?session_id=，1.0.9）
    POST   /queue/{id}/move        排队消息排序（?session_id=，body {delta}，1.0.9）
    POST   /queue/clear            全部清除排队消息（?session_id=，不清引导，1.0.9）
    GET    /approvals              待审批列表
    POST   /approvals/{aid}        批准/拒绝
    GET    /tools                  工具清单
    GET    /skills                 技能列表
    POST   /skills                 新建/覆盖技能
    GET    /skills/{slug}          技能详情
    GET    /skills/{slug}/export   导出 zip
    POST   /skills/import          导入 zip（base64）
    DELETE /skills/{slug}          删除技能
    GET    /mcp                    MCP 服务器列表
    POST   /mcp                    新增服务器
    POST   /mcp/{sid}/connect      连接
    POST   /mcp/{sid}/disconnect   断开
    DELETE /mcp/{sid}              删除
    GET    /settings               助手设置
    PUT    /settings               保存设置
    GET    /audit                  审计记录
"""
import asyncio
import base64
import json
import os
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core.assistant import skills as skills_mod
from core.assistant.agent import get_assistant
from core.assistant.approval import get_approval_manager
from core.assistant.mcp import get_mcp_manager
from core.assistant.queue import (ERR_GUIDED_READONLY, ERR_ITEM_DISPATCHED,
                                  ERR_ITEM_NOT_FOUND, ERR_QUEUE_EMPTY,
                                  ERR_SESSION_BUSY, EV_DEQUEUED, EV_GUIDED,
                                  EV_QUEUE, EV_QUEUE_STATE, EV_QUEUED,
                                  QueueError, get_queue, hhmm)
from core.assistant.store import (load_config, read_audit, save_config)
from core.assistant.store import audit

API_KEY = os.environ.get("LLM_API_KEY", "")


def _auth(x_api_key: Optional[str] = Header(None),
          authorization: Optional[str] = Header(None)):
    if not API_KEY:
        return
    ok = (x_api_key == API_KEY) or (authorization in (f"Bearer {API_KEY}", API_KEY))
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def ok(data: Any = None, message: str = "ok") -> Dict[str, Any]:
    return {"code": 0, "message": message, "data": data}


def fail(code: int, message: str, data: Any = None) -> Dict[str, Any]:
    return {"code": code, "message": message, "data": data}


router = APIRouter(prefix="/api/v1/assistant", dependencies=[Depends(_auth)])


# ----------------------------------------------------------------------------
# 运行态 / 会话
# ----------------------------------------------------------------------------
@router.get("/state")
async def state():
    return ok(get_assistant().state())


@router.get("/sessions")
async def sessions(include_archived: bool = False):
    return ok(get_assistant().session_list(include_archived))


@router.post("/sessions")
async def create_session(body: Optional[Dict[str, Any]] = None):
    body = body or {}
    return ok(get_assistant().new_session(body.get("title") or "",
                                          body.get("workspace") or ""))


@router.get("/spaces")
async def spaces(include_archived: bool = False):
    """任务按空间（工作区目录）分组，左栏「空间」区直接用这个结果。"""
    return ok(get_assistant().space_list(include_archived))


@router.get("/sessions/{sid}")
async def session_detail(sid: str):
    s = get_assistant().get_session(sid)
    if s is None:
        return fail(2001, "会话不存在")
    return ok(s)


@router.patch("/sessions/{sid}")
async def session_update(sid: str, body: Dict[str, Any]):
    """改任务属性：标题（重命名）/ 所属空间 / 置顶 / 归档。"""
    if not isinstance(body, dict) or not body:
        return fail(2002, "没有要修改的字段")
    s = get_assistant().update_session(sid, body)
    if s is None:
        return fail(2001, "会话不存在")
    return ok({"id": s.get("id"), "title": s.get("title"),
               "workspace": s.get("workspace"),
               "pinned": bool(s.get("pinned")),
               "archived": bool(s.get("archived"))})


@router.delete("/sessions/{sid}")
async def session_delete(sid: str):
    # 引导会话队列（1.0.9）：会话删了，排队/暂停/已出队记忆一起清掉
    get_queue().drop_session(sid)
    return ok({"deleted": get_assistant().remove_session(sid)})


@router.get("/tools")
async def tools():
    ass = get_assistant()
    return ok(ass.registry().list())


# ----------------------------------------------------------------------------
# 对话（SSE）+ 引导会话队列（1.0.9 增量）
# ----------------------------------------------------------------------------
def _sse(event: Dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


#: 各会话活跃 SSE 流的事件队列。服务端「有活跃流」判据 = 是否持有该会话；
#: 后开的流覆盖先开的（P0 单用户场景够用，P2-3 完善多客户端同步）。
_STREAM_SINKS: Dict[str, "asyncio.Queue"] = {}


def _broadcast(session_id: str, event: Dict[str, Any]) -> None:
    """向该会话的活跃 SSE 流广播事件（无活跃流时静默丢弃）。

    REST 队列端点与 gen() 都运行在事件循环线程，put_nowait 无需额外锁。
    """
    sink = _STREAM_SINKS.get(session_id)
    if sink is not None:
        sink.put_nowait(event)


def _queue_snapshot_event(session_id: str) -> Dict[str, Any]:
    """队列全量快照事件：客户端以此为准重渲染面板（单一事实来源）。"""
    queue = get_queue()
    return {"type": EV_QUEUE, "items": queue.snapshot(session_id),
            "paused": queue.is_paused(session_id)}


@router.post("/chat")
async def chat(body: Dict[str, Any]):
    """一轮对话。

    mode（1.0.9 引导会话增量）：
        normal（默认）  直接开一轮：stream=true 走 SSE 事件流（现状），
                        stream=false 返回 JSON
        queue           忙碌态入队：有活跃流时只登记队列、不新开流、不打断
                        当前轮；无活跃流时退化为 normal（不报错、不丢消息）
        guided          忙碌态引导发送：立即以 guided 事件登记进对话流
                        （客户端渲染用户气泡 + HH:MM），同样不打断当前轮；
                        无活跃流时退化为 normal
        resume          停止后继续：解暂停并出队头条开一轮（SSE 流）；
                        空队列返回 HTTP 400 + fail(2013)；撞上活跃流 2017

    queue/guided 恒返回 JSON（REST 短请求，忽略 stream 字段）；
    本轮结束后 gen() 自动按 FIFO 消费队列续跑（同一连接），队列耗尽 /
    暂停才下发唯一一次 end + [DONE]。
    """
    body = body or {}
    text = str(body.get("message") or "").strip()
    mode = str(body.get("mode") or "normal").strip() or "normal"
    session_id = str(body.get("session_id") or "").strip()
    stream = bool(body.get("stream", True))
    ass = get_assistant()
    queue = get_queue()

    if mode in ("queue", "guided"):
        if not text:
            return fail(2002, "message 不能为空")
        if session_id and session_id in _STREAM_SINKS:
            # 有活跃流：入队 / 引导登记；当前轮 agent 线程完全不受影响
            try:
                item = queue.enqueue(session_id, text,
                                     guided=(mode == "guided"))
            except QueueError as e:
                return fail(e.code, e.message)
            _broadcast(session_id, {
                "type": EV_QUEUED, "id": item.id, "text": item.text,
                "time": hhmm(item.created_at),
                "index": queue.pending_count(session_id) - 1})
            if item.guided:
                _broadcast(session_id, {
                    "type": EV_GUIDED, "id": item.id, "text": item.text,
                    "time": hhmm(item.created_at)})
            _broadcast(session_id, _queue_snapshot_event(session_id))
            return ok({"queued": item.to_dict(),
                       "paused": queue.is_paused(session_id),
                       "session_id": session_id})
        # 无活跃流（含没有 session_id）：退化为普通发送，不报错、不丢消息
        mode = "normal"

    if mode == "resume":
        if not session_id:
            return fail(2002, "session_id 不能为空")
        if session_id in _STREAM_SINKS:
            return fail(ERR_SESSION_BUSY, "会话正在执行中，请稍后再试")
        if queue.pending_count(session_id) == 0:
            # 这条路径走 SSE 流，没法返回 JSON：用 HTTP 400 + fail(2013)，
            # 客户端经 AssistantStream.failed 提示
            return JSONResponse(
                status_code=400,
                content=fail(ERR_QUEUE_EMPTY, "队列是空的，没有要发送的消息"))
        queue.set_paused(session_id, False)      # 恢复：解暂停
        head = queue.pop_next(session_id)        # FIFO 出队头条
        first_dequeued: Optional[Dict[str, Any]] = {
            "type": EV_DEQUEUED, "id": head.id, "text": head.text,
            "mode": "guided" if head.guided else "queue",
            "time": hhmm(head.created_at)}
        first_text = head.text
    else:
        first_dequeued = None
        if not text:
            return fail(2002, "message 不能为空")
        if not session_id:
            session_id = ass.new_session()["id"]
        first_text = text

    provider_id = str(body.get("provider") or "")
    model = str(body.get("model") or "")

    if not stream:
        from fastapi.concurrency import run_in_threadpool
        events = []
        result = await run_in_threadpool(
            ass.start_turn, session_id, first_text, provider_id, model,
            events.append)
        return ok({"result": result, "events": events,
                   "session_id": session_id})

    async def gen():
        q: "asyncio.Queue" = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(ev: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(q.put_nowait, ev)

        _STREAM_SINKS[session_id] = q
        # agent 的单轮 end 被吞掉，只记录最后一轮的 content/error，
        # 由整条流统一在末尾下发一次
        last = {"content": "", "error": ""}
        try:
            yield _sse({"type": "session", "session_id": session_id})
            if mode == "resume":
                yield _sse({"type": EV_QUEUE_STATE, "paused": False})
                if first_dequeued is not None:
                    yield _sse(first_dequeued)
            yield _sse(_queue_snapshot_event(session_id))   # 起流即同步快照

            text_now = first_text
            while text_now:
                def run(t: str = text_now) -> None:
                    try:
                        ass.start_turn(session_id, t, provider_id, model,
                                       emit)
                    except Exception as e:  # noqa: BLE001
                        emit({"type": "error",
                              "message": f"{type(e).__name__}: {e}"})
                    finally:
                        loop.call_soon_threadsafe(q.put_nowait, None)

                threading.Thread(target=run, daemon=True,
                                 name=f"assistant-{session_id}").start()
                while True:
                    ev = await q.get()
                    if ev is None:
                        break
                    if ev.get("type") == "end":
                        # 吞掉单轮 end：单轮完成仍由 done 表达；中间轮次
                        # 不能再发 end，否则客户端会提前解除忙碌态（P0-2）
                        last = {"content": ev.get("content") or "",
                                "error": ev.get("error") or ""}
                        continue
                    yield _sse(ev)
                if get_queue().is_paused(session_id):
                    # 停止 = 取消 + 暂停：不出队下一条，收流
                    yield _sse({"type": EV_QUEUE_STATE, "paused": True,
                                "reason": "stopped"})
                    break
                nxt = get_queue().pop_next(session_id)   # FIFO 消费下一条
                if nxt is None:
                    break                               # 队列耗尽
                yield _sse({"type": EV_DEQUEUED, "id": nxt.id,
                            "text": nxt.text,
                            "mode": "guided" if nxt.guided else "queue",
                            "time": hhmm(nxt.created_at)})
                yield _sse(_queue_snapshot_event(session_id))
                text_now = nxt.text
            # 整条流唯一一次 end：仅在队列耗尽或暂停时下发
            yield _sse({"type": "end", "content": last["content"],
                        "error": last["error"],
                        "paused": get_queue().is_paused(session_id)})
            yield "data: [DONE]\n\n"
        finally:
            # 防双流互踩：sink 仍是自己才注销（resume 新流会覆盖旧槽位）
            if _STREAM_SINKS.get(session_id) is q:
                _STREAM_SINKS.pop(session_id, None)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/stop")
async def stop(body: Dict[str, Any]):
    """中止当前轮 + 队列置暂停（1.0.9：不出队、不清空，等「继续」）。

    理由：不清空避免丢消息，不自动跑避免「停了还自顾自执行」的惊吓。
    客户端在停止区 / 队列头部提供「继续」（POST /chat mode=resume）。
    """
    sid = str((body or {}).get("session_id") or "")
    if sid:
        get_assistant().stop(sid)
        get_queue().set_paused(sid, True)
        _broadcast(sid, {"type": EV_QUEUE_STATE, "paused": True,
                         "reason": "stopped"})
        _broadcast(sid, _queue_snapshot_event(sid))
    return ok({"stopped": bool(sid)})


# ----------------------------------------------------------------------------
# 引导会话队列（1.0.9 增量）：编辑 / 删除 / 排序 / 清除
#
# 所有业务错误一律 HTTP 200 + fail()；客户端以 queue 全量快照为准刷新，
# 「已出队（2015）」竞态由快照收敛。唯一 HTTP 非 200 是 chat 的 resume
# 空队列（400 + 2013），因为那条路径走 SSE 流。
# ----------------------------------------------------------------------------
@router.get("/queue")
async def queue_snapshot(session_id: str = ""):
    """队列快照（items + paused）：重连恢复 / 停止后同步用。"""
    sid = str(session_id or "").strip()
    if not sid:
        return fail(2002, "session_id 不能为空")
    queue = get_queue()
    return ok({"items": queue.snapshot(sid), "paused": queue.is_paused(sid)})


@router.patch("/queue/{item_id}")
async def queue_update(item_id: str, session_id: str = "",
                       body: Optional[Dict[str, Any]] = None):
    """编辑排队消息（仅排队中的普通消息；引导 2016 / 已出队 2015）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return fail(2002, "session_id 不能为空")
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return fail(2002, "text 不能为空")
    try:
        item = get_queue().update(sid, item_id, text)
    except QueueError as e:
        return fail(e.code, e.message)
    _broadcast(sid, _queue_snapshot_event(sid))
    return ok(item.to_dict())


@router.delete("/queue/{item_id}")
async def queue_delete(item_id: str, session_id: str = ""):
    """删除排队消息（引导消息不可删：已登记对话流，2016）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return fail(2002, "session_id 不能为空")
    try:
        deleted = get_queue().remove(sid, item_id)
    except QueueError as e:
        return fail(e.code, e.message)
    _broadcast(sid, _queue_snapshot_event(sid))
    return ok({"deleted": deleted})


@router.post("/queue/{item_id}/move")
async def queue_move(item_id: str, session_id: str = "",
                     body: Optional[Dict[str, Any]] = None):
    """排序：delta=-1 上移 / +1 下移（越界 2002 / 不存在 2014 / 引导 2016）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return fail(2002, "session_id 不能为空")
    try:
        delta = int((body or {}).get("delta") or 0)
    except (TypeError, ValueError):
        return fail(2002, "delta 只支持 -1 / +1")
    try:
        items = get_queue().move(sid, item_id, delta)
    except QueueError as e:
        return fail(e.code, e.message)
    _broadcast(sid, _queue_snapshot_event(sid))
    return ok({"items": items})


@router.post("/queue/clear")
async def queue_clear(session_id: str = ""):
    """全部清除：只清排队中的普通消息，引导消息不清（已登记对话流）。"""
    sid = str(session_id or "").strip()
    if not sid:
        return fail(2002, "session_id 不能为空")
    cleared = get_queue().clear(sid)
    _broadcast(sid, _queue_snapshot_event(sid))
    return ok({"cleared": cleared})


# ----------------------------------------------------------------------------
# 审批
# ----------------------------------------------------------------------------
@router.get("/approvals")
async def approvals(session_id: str = ""):
    return ok(get_approval_manager().list_pending(session_id))


@router.post("/approvals/{aid}")
async def resolve_approval(aid: str, body: Dict[str, Any]):
    p = get_approval_manager().resolve(
        aid, approve=bool(body.get("approve")),
        remember=bool(body.get("remember")), note=str(body.get("note") or ""))
    if p is None:
        return fail(2003, "审批不存在或已处理")
    return ok(p.to_dict())


# ----------------------------------------------------------------------------
# 技能
# ----------------------------------------------------------------------------
@router.get("/skills")
async def skill_list():
    return ok(skills_mod.list_skills())


@router.post("/skills")
async def skill_save(body: Dict[str, Any]):
    slug = str(body.get("slug") or "").strip()
    content = str(body.get("content") or "")
    if not slug or not content.strip():
        return fail(2004, "slug 与 content 不能为空")
    try:
        return ok(skills_mod.save_skill(slug, content,
                                        body.get("files") or {}))
    except skills_mod.SkillError as e:
        return fail(2005, str(e))


@router.get("/skills/{slug}")
async def skill_detail(slug: str):
    s = skills_mod.load_skill(slug)
    if s is None:
        return fail(2006, "技能不存在")
    return ok({"slug": s["slug"], "meta": s["meta"], "body": s["body"],
               "files": sorted(os.listdir(s["dir"]))})


@router.get("/skills/{slug}/export")
async def skill_export(slug: str):
    try:
        data = skills_mod.export_zip(slug)
    except skills_mod.SkillError as e:
        return fail(2006, str(e))
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{slug}.zip"'})


@router.post("/skills/import")
async def skill_import(body: Dict[str, Any]):
    b64 = str(body.get("zip_base64") or "")
    if not b64:
        return fail(2007, "缺少 zip_base64")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return fail(2007, "zip_base64 解码失败")
    try:
        return ok(skills_mod.import_zip(raw, overwrite=bool(body.get("overwrite", True))))
    except skills_mod.SkillError as e:
        return fail(2008, str(e))


@router.delete("/skills/{slug}")
async def skill_delete(slug: str):
    return ok({"deleted": skills_mod.delete_skill(slug)})


# ----------------------------------------------------------------------------
# MCP
# ----------------------------------------------------------------------------
@router.get("/mcp")
async def mcp_list():
    return ok(get_mcp_manager().status())


@router.post("/mcp")
async def mcp_add(body: Dict[str, Any]):
    mgr = get_mcp_manager()
    cfg = mgr.add_server(body or {})
    audit("mcp_added", server=cfg.get("id"))
    result: Dict[str, Any] = {"server": cfg, "connected": False, "error": ""}
    if body.get("connect", True) and cfg.get("enabled", True):
        try:
            mgr.connect(cfg["id"], get_assistant().registry())
            result["connected"] = True
        except Exception as e:
            result["error"] = str(e)
    return ok(result)


@router.post("/mcp/{sid}/connect")
async def mcp_connect(sid: str):
    try:
        st = get_mcp_manager().connect(sid, get_assistant().registry())
        return ok(st)
    except Exception as e:
        return fail(2009, str(e))


@router.post("/mcp/{sid}/disconnect")
async def mcp_disconnect(sid: str):
    get_mcp_manager().disconnect(sid)
    get_assistant().registry().unregister_prefix(f"mcp__{sid}__")
    return ok({"disconnected": True})


@router.delete("/mcp/{sid}")
async def mcp_delete(sid: str):
    get_mcp_manager().remove_server(sid)
    get_assistant().registry().unregister_prefix(f"mcp__{sid}__")
    return ok({"deleted": True})


# ----------------------------------------------------------------------------
# 设置 / 审计
# ----------------------------------------------------------------------------
@router.get("/settings")
async def get_settings():
    cfg = load_config()
    return ok({k: v for k, v in cfg.items() if k != "providers"} |
              {"providers": cfg.get("providers")})


@router.put("/settings")
async def put_settings(body: Dict[str, Any]):
    allowed = {"policy", "active_provider", "providers", "workspace",
               "max_steps", "approval_timeout", "temperature", "max_tokens",
               "skill_timeout"}
    patch = {k: v for k, v in (body or {}).items() if k in allowed}
    if "providers" in patch:
        # 按 id 合并：客户端拿到的 providers 不含 api_key（state 不下发密钥），
        # 直接整体覆盖会把已保存的 Key 抹掉，这里保留原值。
        cur = {p.get("id"): dict(p) for p in (load_config().get("providers") or [])}
        merged = []
        for p in (patch.get("providers") or []):
            if not isinstance(p, dict) or not p.get("id"):
                continue
            base = cur.get(p["id"], {"id": p["id"]})
            base.update({k: v for k, v in p.items() if v is not None})
            merged.append(base)
        patch["providers"] = merged
    if "workspace" in patch:
        ws = str(patch["workspace"] or "").strip()
        if ws:
            try:
                os.makedirs(ws, exist_ok=True)
            except OSError as e:
                return fail(2010, f"工作区不可用：{e}")
            if not os.path.isdir(ws):
                return fail(2011, f"工作区不是目录：{ws}")
            # 统一成绝对路径，避免 / 与 \ 混写导致客户端比对不上（判定「是不是
            # 上次那个工作区」时用的是 normcase 比较）
            patch["workspace"] = os.path.abspath(ws)
    cfg = save_config(patch)
    audit("settings_updated", keys=sorted(patch.keys()))
    return ok({k: cfg.get(k) for k in allowed})


@router.get("/audit")
async def get_audit(limit: int = 200):
    return ok(read_audit(max(1, min(int(limit), 1000))))
