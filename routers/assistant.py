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

from core.assistant import catalog as catalog_mod
from core.assistant import context as context_mod
from core.assistant import profiles as profiles_mod
from core.assistant import skills as skills_mod
from core.assistant import store as store_mod
from core.assistant import tools as tools_mod
from core.assistant.agent import get_assistant
from core.assistant.approval import get_approval_manager
from core.assistant.mcp import MCPError, get_mcp_manager
from core.assistant.parallel import (ERR_NOTHING_TO_RESUME, ERR_PARAM,
                                     ERR_PLAN_BUSY, ERR_PLAN_NOT_FOUND,
                                     MAX_TASKS, ParallelError, get_runner)
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
    """任务列表。多任务并行后每行带 `running`：同一工作空间里哪些任务在跑。

    `running` 来自后端的活跃轮次表（不是客户端推断），所以两个窗口 / 重启
    客户端后左栏的运行圆点也是准的。
    """
    ass = get_assistant()
    rows = ass.session_list(include_archived)
    try:
        running = set(ass.running_ids())
    except Exception:  # noqa: BLE001
        running = set()
    for r in rows:
        if isinstance(r, dict):
            r["running"] = str(r.get("id")) in running
    return ok(rows)


@router.post("/sessions")
async def create_session(body: Optional[Dict[str, Any]] = None):
    body = body or {}
    return ok(get_assistant().new_session(body.get("title") or "",
                                          body.get("workspace") or ""))


@router.get("/spaces")
async def spaces(include_archived: bool = False):
    """空间列表（1.0.11 起为真实具名实体，每个空间带全部任务）。"""
    return ok(get_assistant().space_list(include_archived))


#: 空间是一组具名、可切换的工作区目录。`key` 是归一化后的目录路径
#:（URL 里用 :path 转换器透传斜杠）。
@router.post("/spaces")
async def create_space(body: Optional[Dict[str, Any]] = None):
    """新建空间（具名、可切换的工作区目录）。"""
    body = body or {}
    return ok(get_assistant().create_space(body.get("name") or "",
                                            body.get("workspace") or ""))


@router.patch("/spaces/{key:path}")
async def update_space(key: str, body: Dict[str, Any]):
    """重命名空间 / 改目录（改目录会同时迁移该空间下的任务）。"""
    if not isinstance(body, dict) or not body:
        return fail(2034, "没有要修改的字段")
    return ok(get_assistant().rename_space(
        key, body.get("name") or "", body.get("workspace") or ""))


@router.delete("/spaces/{key:path}")
async def delete_space_endpoint(key: str):
    """注销空间（只删名字/排序登记，不动任务与目录）。"""
    return ok(get_assistant().delete_space(key))


@router.post("/spaces/{key:path}/activate")
async def activate_space(key: str):
    """把该空间设为当前（左栏高亮 + 新任务/文件工具默认落它）。"""
    return ok(get_assistant().activate_space(key))


#: 提示词库：全局可复用，跨空间调用。
@router.get("/prompts")
async def prompts():
    return ok(get_assistant().prompt_list())


@router.post("/prompts")
async def create_prompt(body: Optional[Dict[str, Any]] = None):
    body = body or {}
    if not (body.get("title") or "").strip() and not (body.get("content") or "").strip():
        return fail(2040, "提示词标题和内容不能都为空")
    return ok(get_assistant().prompt_save(body))


@router.get("/prompts/{pid}")
async def prompt_detail(pid: str):
    p = get_assistant().prompt_get(pid)
    if p is None:
        return fail(2041, "提示词不存在")
    return ok(p)


@router.patch("/prompts/{pid}")
async def update_prompt(pid: str, body: Dict[str, Any]):
    body = dict(body or {})
    body["id"] = pid
    return ok(get_assistant().prompt_save(body))


@router.delete("/prompts/{pid}")
async def delete_prompt_endpoint(pid: str):
    return ok({"deleted": get_assistant().prompt_delete(pid)})


@router.get("/sessions/{sid}")
async def session_detail(sid: str):
    s = get_assistant().get_session(sid)
    if s is None:
        return fail(2001, "会话不存在")
    # 附上上下文占用（2026-10-26）：客户端切到旧任务时也要能显示「上下文 xx%」，
    # 否则只有发过消息之后才知道用量，看着像没这个功能。
    try:
        s = dict(s)
        s["context_usage"] = context_mod.usage(s, load_config())
    except Exception:  # noqa: BLE001 —— 统计失败不影响会话本身
        pass
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
    # 挂载技能（1.0.8，架构 §5 A4）：客户端传列表，正文保持原文；
    # 老客户端不带这个字段 -> None，行为与今天完全一致。
    mounted = body.get("mounted_skills")
    mounted_skills = ([str(s) for s in mounted if str(s).strip()]
                      if isinstance(mounted, list) else None)
    # 粘贴的图片（2026-10-26）：客户端已落盘，这里只传绝对路径
    raw_att = body.get("attachments")
    attachments = ([str(p) for p in raw_att if str(p).strip()]
                   if isinstance(raw_att, list) else None)

    if not stream:
        from fastapi.concurrency import run_in_threadpool
        events = []
        result = await run_in_threadpool(
            ass.start_turn, session_id, first_text, provider_id, model,
            events.append, mounted_skills, attachments)
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
            first_round = True
            while text_now:
                # 附件只随第一轮下发：后续轮次是队列里的其它消息，不该重复带图
                atts = attachments if first_round else None
                first_round = False

                def run(t: str = text_now, atts=atts) -> None:
                    try:
                        ass.start_turn(session_id, t, provider_id, model,
                                       emit, mounted_skills, atts)
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
    try:
        cfg = mgr.add_server(body or {})
    except MCPError as e:
        # 安全红线：command 不在白名单（mcp.add_server 兜底校验）
        return fail(2022, str(e))
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
               "skill_timeout", "auto_load", "max_tool_rounds", "catalog",
               "parallel_max_workers", "parallel_max_retries",
               "parallel_subtask_timeout"}
    patch = {k: v for k, v in (body or {}).items() if k in allowed}
    if "catalog" in patch:
        # catalog 只允许改白名单字段，且**按字段合并**（save_config 是浅合并，
        # 整体覆盖会把没传的 proxy / ttl_hours 抹掉）
        raw = patch.get("catalog") if isinstance(patch.get("catalog"), dict) else {}
        cur_cat = dict(load_config().get("catalog") or {})
        for key in ("proxy", "github_token"):
            if key in raw:
                cur_cat[key] = str(raw.get(key) or "").strip()
        if isinstance(raw.get("ttl_hours"), dict):
            merged_ttl = dict(cur_cat.get("ttl_hours") or {})
            merged_ttl.update({k: v for k, v in raw["ttl_hours"].items()
                               if isinstance(v, (int, float))})
            cur_cat["ttl_hours"] = merged_ttl
        if "allow_nonfree" in raw:
            cur_cat["allow_nonfree"] = bool(raw.get("allow_nonfree"))
        patch["catalog"] = cur_cat
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


# ----------------------------------------------------------------------------
# 在线目录（1.0.8 增量，架构 §8 REST 接口表）
#
# 返回约定与既有接口一致：业务错误一律 HTTP 200 + fail(code, message, data)，
# 错误码 2020-2026（2020 不可达 / 2021 上游非 200 / 2022 结构非法 /
# 2023 已存在 / 2024 必填 env 缺失 / 2025 连接失败 / 2026 URL 不受支持）。
# ----------------------------------------------------------------------------
def _catalog_fail(e: Any) -> Dict[str, Any]:
    """CatalogError -> fail() 的统一转换（保留 data 供对话框逐项标红）。"""
    code = int(getattr(e, "code", catalog_mod.ERR_UNREACHABLE) or
               catalog_mod.ERR_UNREACHABLE)
    return fail(code, str(getattr(e, "message", e)), getattr(e, "data", None))


def _catalog_cfg() -> Dict[str, Any]:
    return load_config()


@router.get("/catalog/mcp")
async def catalog_mcp(q: str = "", cursor: str = "", force: bool = False):
    """在线 MCP 目录（先返缓存；force=1 才同步联网，超时 5s）。"""
    from fastapi.concurrency import run_in_threadpool
    try:
        data = await run_in_threadpool(catalog_mod.list_mcp, _catalog_cfg(),
                                       q, cursor, force)
    except catalog_mod.CatalogError as e:
        return _catalog_fail(e)
    return ok(data)


@router.get("/catalog/skills")
async def catalog_skills(q: str = "", force: bool = False):
    """在线技能仓库（GitHub `topic:claude-skills`，star 降序）。"""
    from fastapi.concurrency import run_in_threadpool
    try:
        data = await run_in_threadpool(catalog_mod.list_repos, _catalog_cfg(),
                                       q, force)
    except catalog_mod.CatalogError as e:
        return _catalog_fail(e)
    return ok(data)


@router.get("/catalog/skills/{owner}/{repo}")
async def catalog_repo_skills(owner: str, repo: str, ref: str = "",
                              force: bool = False):
    """枚举单仓库里的技能目录（`**/SKILL.md`）。

    `force=1` 跳过缓存强制刷新（客户端 `catalog_repo_skills(..., force=True)`）。
    """
    from fastapi.concurrency import run_in_threadpool
    try:
        data = await run_in_threadpool(catalog_mod.list_repo_skills,
                                       _catalog_cfg(), owner, repo, ref, force)
    except catalog_mod.CatalogError as e:
        return _catalog_fail(e)
    return ok(data)


@router.post("/catalog/skills/install")
async def catalog_install_skill(body: Dict[str, Any]):
    """在线安装技能：GitHub 整包 tarball（含二进制/大体积）-> 原子落盘 -> 记 skill_sources。

    走 `catalog.fetch_skill_archive`：下载技能目录的整棵子树 tarball，在内存里
    抽取后交 `skills.install_from_github` 二进制安全落盘（SVG / 图标 / 音效等
    不再被文本管线静默丢弃）。
    """
    from fastapi.concurrency import run_in_threadpool
    body = body or {}
    repo = str(body.get("repo") or "").strip()
    if "/" not in repo:
        return fail(2022, "repo 必须是 owner/repo 形式")
    owner, name = repo.split("/", 1)
    ref = str(body.get("ref") or "").strip()
    dirpath = str(body.get("dir") or "").strip()
    slug = str(body.get("slug") or "").strip()
    overwrite = bool(body.get("overwrite"))
    cfg = _catalog_cfg()
    try:
        bundle = await run_in_threadpool(catalog_mod.fetch_skill_archive,
                                         cfg, owner, name, ref, dirpath)
        result = await run_in_threadpool(
            skills_mod.install_from_github, bundle, slug, overwrite,
            {"repo": repo, "ref": bundle.get("ref") or ref,
             "dir": dirpath, "commit": bundle.get("commit") or "",
             "has_scripts": bool(bundle.get("has_scripts"))})
    except catalog_mod.CatalogError as e:
        audit("catalog_install_failed", kind="skill", target=repo, error=str(e))
        return _catalog_fail(e)
    except skills_mod.SkillError as e:
        code = (2023 if "已存在" in str(e) else 2022)
        audit("catalog_install_failed", kind="skill", target=repo, error=str(e))
        return fail(code, str(e))
    audit("skill_installed_online", slug=result.get("slug"), repo=repo,
          ref=result.get("ref") or ref, dir=dirpath,
          commit=result.get("commit") or "", bytes=result.get("bytes") or 0,
          has_scripts=bool(result.get("has_scripts")), overwrite=overwrite,
          skipped=int(result.get("skipped") or 0))
    return ok(result)


@router.post("/catalog/mcp/install")
async def catalog_install_mcp(body: Dict[str, Any]):
    """在线安装 MCP：Registry 条目 -> mcp_servers 配置 -> 可选立即连接。"""
    from fastapi.concurrency import run_in_threadpool
    body = body or {}
    registry_name = str(body.get("registry_name") or "").strip()
    if not registry_name:
        return fail(2022, "registry_name 不能为空")
    env = body.get("env") if isinstance(body.get("env"), dict) else {}
    server_id = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    risk = str(body.get("risk") or "high")
    if risk not in ("low", "medium", "high"):
        risk = "high"
    do_connect = bool(body.get("connect", True))
    item = body.get("item") if isinstance(body.get("item"), dict) else None
    mgr = get_mcp_manager()
    try:
        result = await run_in_threadpool(
            mgr.install_from_registry, registry_name, item, env, server_id,
            name, risk, do_connect, get_assistant().registry())
    except catalog_mod.CatalogError as e:
        if e.code == catalog_mod.ERR_CONNECT_FAILED:
            # 2025：配置已保留，客户端展示「已保存配置但连接失败」+ 卡片 ×
            audit("mcp_installed_online", server=(e.data or {}).get("server", {})
                  .get("id"), registry_name=registry_name, connected=False)
            return fail(e.code, e.message, e.data)
        audit("catalog_install_failed", kind="mcp", target=registry_name,
              error=str(e))
        return _catalog_fail(e)
    except Exception as e:                      # noqa: BLE001
        audit("catalog_install_failed", kind="mcp", target=registry_name,
              error=str(e))
        return fail(catalog_mod.ERR_CONNECT_FAILED, str(e))
    return ok(result)


@router.post("/catalog/refresh")
async def catalog_refresh():
    """并行刷新三源（总 12s 上限）；**总是 ok()**，错误在 data.errors 里。"""
    from fastapi.concurrency import run_in_threadpool
    data = await run_in_threadpool(catalog_mod.refresh_all, _catalog_cfg(), True)
    audit("catalog_refreshed", ok=bool(data.get("ok")),
          counts=data.get("counts") or {}, errors=data.get("errors") or [])
    return ok(data)


@router.post("/catalog/skills/from_dir")
async def catalog_skill_from_dir(body: Dict[str, Any]):
    """从本地文件夹导入技能（客户端经 `_pick_file(directory=True)` 选目录）。"""
    from fastapi.concurrency import run_in_threadpool
    body = body or {}
    path = str(body.get("path") or "").strip()
    if not path:
        return fail(2022, "path 不能为空")
    overwrite = bool(body.get("overwrite"))
    try:
        result = await run_in_threadpool(skills_mod.install_from_dir, path,
                                         overwrite, str(body.get("slug") or ""))
    except skills_mod.SkillError as e:
        code = (2023 if "已存在" in str(e) else 2022)
        return fail(code, str(e))
    audit("skill_installed_local", slug=result.get("slug"), path=path,
          overwrite=overwrite)
    return ok(result)


@router.post("/catalog/skills/from_url")
async def catalog_skill_from_url(body: Dict[str, Any]):
    """粘贴 URL 安装技能（只支持 GitHub / Gitee 的 SKILL.md 直链）。"""
    from fastapi.concurrency import run_in_threadpool
    body = body or {}
    url = str(body.get("url") or "").strip()
    if not url:
        return fail(catalog_mod.ERR_BAD_URL, "url 不能为空")
    overwrite = bool(body.get("overwrite"))
    cfg = _catalog_cfg()
    try:
        bundle = await run_in_threadpool(catalog_mod.fetch_bundle_from_raw,
                                         cfg, url)
        result = await run_in_threadpool(skills_mod.install_from_url, bundle,
                                         str(body.get("slug") or ""), overwrite)
    except catalog_mod.CatalogError as e:
        return _catalog_fail(e)
    except skills_mod.SkillError as e:
        code = (2023 if "已存在" in str(e) else 2022)
        return fail(code, str(e))
    audit("skill_installed_online", slug=result.get("slug"), url=url,
          overwrite=overwrite)
    return ok(result)


# ----------------------------------------------------------------------------
# 工具偏好 / 自动加载 profile（1.0.8 增量）
# ----------------------------------------------------------------------------
def _tool_prefs_payload(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """GET/PUT /tools/prefs 的响应体：偏好 + 计数 + 被上限裁掉的工具名。"""
    cfg = cfg or load_config()
    prefs = cfg.get("tool_prefs") if isinstance(cfg.get("tool_prefs"), dict) else {}
    disabled = [str(x) for x in (prefs.get("disabled") or [])]
    try:
        max_tools = int(prefs.get("max_tools") or tools_mod.DEFAULT_MAX_TOOLS)
    except (TypeError, ValueError):
        max_tools = tools_mod.DEFAULT_MAX_TOOLS
    registry = get_assistant().registry()
    total = len(registry.names())
    active = [t.name for t in registry.active(disabled=set(disabled), allow=None)]
    over = active[max_tools:] if max_tools > 0 else []
    return {"disabled": disabled, "max_tools": max_tools, "total": total,
            "active": len(active[:max_tools]) if max_tools > 0 else len(active),
            "over": over or None}


@router.get("/tools/prefs")
async def get_tool_prefs():
    return ok(_tool_prefs_payload())


@router.put("/tools/prefs")
async def put_tool_prefs(body: Dict[str, Any]):
    """保存工具偏好。`max_tools` 允许 8~64；非法参数 2002。"""
    body = body or {}
    patch: Dict[str, Any] = {}
    prefs_cur = load_config().get("tool_prefs")
    prefs = dict(prefs_cur) if isinstance(prefs_cur, dict) else {}
    if "disabled" in body:
        raw = body.get("disabled")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            return fail(2002, "disabled 必须是字符串数组")
        prefs["disabled"] = [str(x) for x in raw if str(x).strip()]
    if "max_tools" in body:
        try:
            val = int(body.get("max_tools"))
        except (TypeError, ValueError):
            return fail(2002, "max_tools 必须是整数（8~64）")
        if not (store_mod.MIN_MAX_TOOLS <= val <= store_mod.MAX_MAX_TOOLS):
            return fail(2002, f"max_tools 只允许 {store_mod.MIN_MAX_TOOLS}~"
                              f"{store_mod.MAX_MAX_TOOLS}")
        prefs["max_tools"] = val
    prefs.setdefault("disabled", [])
    prefs.setdefault("max_tools", tools_mod.DEFAULT_MAX_TOOLS)
    patch["tool_prefs"] = prefs
    cfg = save_config(patch)
    audit("tools_prefs_updated", disabled=prefs.get("disabled"),
          max_tools=prefs.get("max_tools"))
    registry = get_assistant().registry()
    registry.set_filter(disabled=set(prefs.get("disabled") or []), allow=None,
                        max_tools=int(prefs.get("max_tools")
                                      or tools_mod.DEFAULT_MAX_TOOLS))
    return ok(_tool_prefs_payload(cfg))


def _profiles_payload(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = cfg or load_config()
    profiles = cfg.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        profiles = profiles_mod.BUILTIN_PROFILES
    return {"auto_load": bool(cfg.get("auto_load", True)),
            "profiles": profiles}


@router.get("/profiles")
async def get_profiles():
    """自动加载 profile 列表 + 总开关（P0 只做查看/启停，不做自定义编辑）。"""
    return ok(_profiles_payload())


@router.put("/profiles")
async def put_profiles(body: Dict[str, Any]):
    """改 `auto_load` 与 profile 启停（只接受已知 id，避免脏数据写进 config）。"""
    body = body or {}
    patch: Dict[str, Any] = {}
    if "auto_load" in body:
        patch["auto_load"] = bool(body.get("auto_load"))
    if "profiles" in body:
        incoming = body.get("profiles")
        if not isinstance(incoming, list):
            return fail(2002, "profiles 必须是数组")
        cur_list = _profiles_payload().get("profiles") or []
        cur = {str(p.get("id") or ""): p for p in cur_list if isinstance(p, dict)}
        merged: List[Dict[str, Any]] = []
        for row in incoming:
            if not isinstance(row, dict) or not row.get("id"):
                return fail(2002, "每个 profile 必须有 id")
            pid = str(row.get("id"))
            if pid not in cur:
                return fail(2002, f"未知 profile：{pid}")
            base = dict(cur[pid])
            if "enabled" in row:
                base["enabled"] = bool(row.get("enabled"))
            if "triggers" in row:
                raw = row.get("triggers")
                if not isinstance(raw, list):
                    return fail(2002, "triggers 必须是字符串数组")
                base["triggers"] = [str(t) for t in raw if str(t).strip()]
            if "priority" in row:
                try:
                    base["priority"] = int(row.get("priority"))
                except (TypeError, ValueError):
                    return fail(2002, "priority 必须是整数")
            merged.append(base)
        patch["profiles"] = merged
    cfg = save_config(patch)
    if patch:
        audit("auto_load_updated", auto_load=patch.get("auto_load"),
              profiles=[p.get("id") for p in (patch.get("profiles") or [])])
    return ok(_profiles_payload(cfg))


# ----------------------------------------------------------------------------
# 并行多任务（core/assistant/parallel.py）
#
# 约定与既有接口一致：业务错误 HTTP 200 + fail(code, message, data)，
# 错误码 2030-2033（2030 参数非法 / 2031 计划不存在 / 2032 计划忙 /
# 2033 无可续跑子任务）。执行走后台线程，事件落在计划快照的 events 里，
# 用 GET /parallel/{plan_id} 拉取（与既有 SSE 单会话流不混用）。
# ----------------------------------------------------------------------------
@router.get("/parallel")
async def parallel_list(limit: int = 50):
    """并行计划列表（摘要，不含子任务明细）。"""
    return ok(get_runner().list(limit))


@router.post("/parallel/plan")
async def parallel_plan(body: Optional[Dict[str, Any]] = None):
    """把目标拆成子任务计划（只规划不执行）。

    body: {goal, provider, model, max_tasks, workspace, title, subtasks?}
    `subtasks` 给了就按它拆（不调模型，确定性路径）。
    """
    body = body or {}
    try:
        plan = get_runner().plan(
            goal=str(body.get("goal") or ""),
            provider_id=str(body.get("provider") or ""),
            model=str(body.get("model") or ""),
            max_tasks=int(body.get("max_tasks") or MAX_TASKS),
            workspace=str(body.get("workspace") or ""),
            title=str(body.get("title") or ""),
            subtasks=body.get("subtasks") or None)
    except ParallelError as e:
        return fail(e.code, e.message)
    except Exception as e:  # noqa: BLE001
        return fail(ERR_PARAM, f"拆分失败：{e}")
    return ok(plan.to_dict())


@router.post("/parallel/run")
async def parallel_run(body: Optional[Dict[str, Any]] = None):
    """拆分并在后台并行执行；立即返回计划快照（用 GET 轮询进度）。"""
    body = body or {}
    runner = get_runner()
    try:
        plan = runner.plan(
            goal=str(body.get("goal") or ""),
            provider_id=str(body.get("provider") or ""),
            model=str(body.get("model") or ""),
            max_tasks=int(body.get("max_tasks") or MAX_TASKS),
            workspace=str(body.get("workspace") or ""),
            title=str(body.get("title") or ""),
            subtasks=body.get("subtasks") or None)
    except ParallelError as e:
        return fail(e.code, e.message)
    except Exception as e:  # noqa: BLE001
        return fail(ERR_PARAM, f"拆分失败：{e}")

    def _work() -> None:
        try:
            runner.run(plan)
        except Exception as e:  # noqa: BLE001 —— 后台线程不外冒
            audit("parallel_run_failed", plan_id=plan.id, error=str(e))

    threading.Thread(target=_work, daemon=True,
                     name=f"parallel-plan-{plan.id}").start()
    return ok(plan.to_dict())


@router.get("/parallel/{plan_id}")
async def parallel_detail(plan_id: str):
    """计划详情（含子任务状态与事件）。"""
    plan = get_runner().get(plan_id)
    if plan is None:
        return fail(ERR_PLAN_NOT_FOUND, "计划不存在")
    return ok(plan.to_dict())


@router.post("/parallel/{plan_id}/cancel")
async def parallel_cancel(plan_id: str):
    """取消计划：停掉正在跑的子任务，未开始的标记为已取消。"""
    try:
        get_runner().cancel(plan_id)
    except ParallelError as e:
        return fail(e.code, e.message)
    except Exception as e:  # noqa: BLE001
        return fail(ERR_PARAM, f"取消失败：{e}")
    return ok({"cancelled": True, "id": plan_id})


@router.post("/parallel/{plan_id}/resume")
async def parallel_resume(plan_id: str, body: Optional[Dict[str, Any]] = None):
    """从断点续跑：跳过已完成的子任务，只跑未完成的（可换模型）。

    body: {provider?, model?} —— 换模型只作用于未完成的子任务。
    与 /parallel/run 一致：**后台执行**，立即返回计划快照，客户端轮询详情。
    """
    body = body or {}
    runner = get_runner()
    plan = runner.get(plan_id)
    if plan is None:
        return fail(ERR_PLAN_NOT_FOUND, "计划不存在")
    if not plan.pending_tasks():
        return fail(ERR_NOTHING_TO_RESUME, "没有未完成的子任务")
    if runner.is_running(plan_id):
        # 刚取消的计划还在收尾（子任务线程退出中），此时续跑会撞上「计划忙」
        return fail(ERR_PLAN_BUSY, "计划还在收尾，请稍后再续跑")

    def _work() -> None:
        try:
            runner.resume(plan_id,
                          provider_id=str(body.get("provider") or ""),
                          model=str(body.get("model") or ""))
        except Exception as e:  # noqa: BLE001 —— 后台线程不外冒
            audit("parallel_resume_failed", plan_id=plan_id, error=str(e))

    threading.Thread(target=_work, daemon=True,
                     name=f"parallel-resume-{plan_id}").start()
    return ok(plan.to_dict())
