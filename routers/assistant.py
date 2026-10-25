# -*- coding: utf-8 -*-
"""
智能助理 API（routers/assistant.py）。

统一前缀：/api/v1/assistant
鉴权：与管理接口一致（环境变量 LLM_API_KEY 非空时校验 X-API-Key / Bearer）

接口清单：
    GET    /state                  运行态快照（提供商/工具/技能/MCP/策略/会话）
    GET    /sessions               会话列表
    POST   /sessions               新建会话
    GET    /sessions/{sid}         会话详情
    DELETE /sessions/{sid}         删除会话
    POST   /chat                   一轮对话（SSE 事件流；stream=false 时返回 JSON）
    POST   /stop                   中止当前轮次
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
from fastapi.responses import Response, StreamingResponse

from core.assistant import skills as skills_mod
from core.assistant.agent import get_assistant
from core.assistant.approval import get_approval_manager
from core.assistant.mcp import get_mcp_manager
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
async def sessions():
    return ok(get_assistant().session_list())


@router.post("/sessions")
async def create_session(body: Optional[Dict[str, Any]] = None):
    return ok(get_assistant().new_session((body or {}).get("title") or ""))


@router.get("/sessions/{sid}")
async def session_detail(sid: str):
    s = get_assistant().get_session(sid)
    if s is None:
        return fail(2001, "会话不存在")
    return ok(s)


@router.delete("/sessions/{sid}")
async def session_delete(sid: str):
    return ok({"deleted": get_assistant().remove_session(sid)})


@router.get("/tools")
async def tools():
    ass = get_assistant()
    return ok(ass.registry().list())


# ----------------------------------------------------------------------------
# 对话（SSE）
# ----------------------------------------------------------------------------
def _sse(event: Dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@router.post("/chat")
async def chat(body: Dict[str, Any]):
    """一轮对话。stream=true（默认）返回 SSE 事件流。"""
    text = str(body.get("message") or "").strip()
    if not text:
        return fail(2002, "message 不能为空")
    session_id = str(body.get("session_id") or "").strip()
    ass = get_assistant()
    if not session_id:
        session_id = ass.new_session()["id"]
    provider_id = str(body.get("provider") or "")
    model = str(body.get("model") or "")
    stream = bool(body.get("stream", True))

    if not stream:
        from fastapi.concurrency import run_in_threadpool
        events = []
        result = await run_in_threadpool(
            ass.start_turn, session_id, text, provider_id, model, events.append)
        return ok({"result": result, "events": events, "session_id": session_id})

    async def gen():
        q: "asyncio.Queue" = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def emit(ev: Dict[str, Any]) -> None:
            loop.call_soon_threadsafe(q.put_nowait, ev)

        def run() -> None:
            try:
                ass.start_turn(session_id, text, provider_id, model, emit)
            except Exception as e:
                emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
            finally:
                loop.call_soon_threadsafe(q.put_nowait, None)

        threading.Thread(target=run, daemon=True,
                         name=f"assistant-{session_id}").start()
        yield _sse({"type": "session", "session_id": session_id})
        while True:
            ev = await q.get()
            if ev is None:
                break
            yield _sse(ev)
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/stop")
async def stop(body: Dict[str, Any]):
    sid = str(body.get("session_id") or "")
    if sid:
        get_assistant().stop(sid)
    return ok({"stopped": bool(sid)})


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
