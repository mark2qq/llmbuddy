# -*- coding: utf-8 -*-
"""
智能助理客户端（core/assistant_client.py）。

两部分：
    AssistantApi     同步 REST 调用（在 Worker 线程里用，不阻塞 UI）
    AssistantStream  一轮对话的 SSE 事件流（QThread，逐事件 emit 到主线程）

注意（项目踩过的坑）：QThread 的 Python 包装对象在 run() 期间必须被强引用持有，
否则 GC 会销毁仍在运行的底层线程 → 原生崩溃 0xC0000409。这里沿用 task_runner
的做法，用模块级 _ACTIVE 在 start() 时自持引用，结束后释放。
"""
import json
from typing import Any, Dict, List, Optional

import httpx
from PySide6.QtCore import QThread, Signal

_ACTIVE: list = []


def _client(base_url: str, api_key: str = "", timeout: float = 30.0) -> httpx.Client:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    return httpx.Client(base_url=base_url, trust_env=False,
                        timeout=timeout, headers=headers)


class AssistantApi:
    """后端 /api/v1/assistant/* 的同步封装（code!=0 抛 RuntimeError）。"""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    # ---------------- 底层 ----------------
    def _unwrap(self, resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if isinstance(body, dict) and "code" in body:
            if body.get("code", 0) != 0:
                raise RuntimeError(body.get("message") or f"错误 {body.get('code')}")
            return body.get("data")
        return body

    def get(self, path: str, params: Optional[Dict] = None) -> Any:
        with _client(self.base_url, self.api_key) as c:
            return self._unwrap(c.get(path, params=params))

    def post(self, path: str, body: Optional[Dict] = None,
             timeout: float = 60.0, params: Optional[Dict] = None) -> Any:
        with _client(self.base_url, self.api_key, timeout) as c:
            return self._unwrap(c.post(path, json=body or {}, params=params))

    def put(self, path: str, body: Optional[Dict] = None) -> Any:
        with _client(self.base_url, self.api_key) as c:
            return self._unwrap(c.put(path, json=body or {}))

    def patch(self, path: str, body: Optional[Dict] = None,
              params: Optional[Dict] = None) -> Any:
        with _client(self.base_url, self.api_key) as c:
            return self._unwrap(c.patch(path, json=body or {}, params=params))

    def delete(self, path: str, params: Optional[Dict] = None) -> Any:
        with _client(self.base_url, self.api_key) as c:
            return self._unwrap(c.delete(path, params=params))

    def get_bytes(self, path: str) -> bytes:
        with _client(self.base_url, self.api_key, 60.0) as c:
            r = c.get(path)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code}")
        return r.content

    # ---------------- 业务 ----------------
    def state(self) -> Dict:
        return self.get("/api/v1/assistant/state") or {}

    def sessions(self) -> List[Dict]:
        return self.get("/api/v1/assistant/sessions") or []

    def spaces(self) -> List[Dict]:
        """任务按工作区目录分组（左栏「空间」区）。"""
        return self.get("/api/v1/assistant/spaces") or []

    def new_session(self, workspace: str = "") -> Dict:
        return self.post("/api/v1/assistant/sessions",
                         {"workspace": workspace} if workspace else {}) or {}

    def update_session(self, sid: str, patch: Dict) -> Dict:
        """改任务：标题 / 所属空间 / 置顶 / 归档。"""
        return self.patch(f"/api/v1/assistant/sessions/{sid}", patch) or {}

    def session(self, sid: str) -> Dict:
        return self.get(f"/api/v1/assistant/sessions/{sid}") or {}

    def delete_session(self, sid: str) -> Dict:
        return self.delete(f"/api/v1/assistant/sessions/{sid}") or {}

    def stop(self, sid: str) -> Dict:
        return self.post("/api/v1/assistant/stop", {"session_id": sid}) or {}

    # ---------------- 引导会话队列（1.0.9：排队插话 / 引导发送） ----------------
    def queue(self, sid: str) -> Dict:
        """队列快照（items + paused）：重连恢复 / 停止后同步用。"""
        return self.get("/api/v1/assistant/queue", {"session_id": sid}) or {}

    def enqueue(self, sid: str, text: str, guided: bool = False) -> Dict:
        """入队 / 引导发送（POST /chat mode=queue|guided，REST 短请求不新开流）。"""
        return self.post("/api/v1/assistant/chat",
                         {"session_id": sid, "message": text,
                          "mode": "guided" if guided else "queue",
                          "stream": False}) or {}

    def queue_update(self, sid: str, item_id: str, text: str) -> Dict:
        """编辑排队消息（仅排队中的普通消息；引导/已出队后端报错）。"""
        return self.patch(f"/api/v1/assistant/queue/{item_id}",
                          {"text": text}, params={"session_id": sid}) or {}

    def queue_delete(self, sid: str, item_id: str) -> Dict:
        """删除排队消息（引导消息不可删）。"""
        return self.delete(f"/api/v1/assistant/queue/{item_id}",
                           params={"session_id": sid}) or {}

    def queue_move(self, sid: str, item_id: str, delta: int) -> Dict:
        """排序：delta=-1 上移 / +1 下移。"""
        return self.post(f"/api/v1/assistant/queue/{item_id}/move",
                         {"delta": delta}, params={"session_id": sid}) or {}

    def queue_clear(self, sid: str) -> Dict:
        """全部清除（只清普通排队消息，引导消息不清）。"""
        return self.post("/api/v1/assistant/queue/clear", {},
                         params={"session_id": sid}) or {}

    def resolve_approval(self, aid: str, approve: bool, remember: bool = False) -> Dict:
        return self.post(f"/api/v1/assistant/approvals/{aid}",
                         {"approve": approve, "remember": remember}) or {}

    def skills(self) -> List[Dict]:
        return self.get("/api/v1/assistant/skills") or []

    def skill(self, slug: str) -> Dict:
        return self.get(f"/api/v1/assistant/skills/{slug}") or {}

    def save_skill(self, slug: str, content: str) -> Dict:
        return self.post("/api/v1/assistant/skills",
                         {"slug": slug, "content": content}) or {}

    def delete_skill(self, slug: str) -> Dict:
        return self.delete(f"/api/v1/assistant/skills/{slug}") or {}

    def export_skill(self, slug: str) -> bytes:
        return self.get_bytes(f"/api/v1/assistant/skills/{slug}/export")

    def import_skill(self, zip_bytes: bytes) -> Dict:
        import base64
        return self.post("/api/v1/assistant/skills/import",
                         {"zip_base64": base64.b64encode(zip_bytes).decode("ascii")},
                         timeout=120.0) or {}

    def mcp_servers(self) -> List[Dict]:
        return self.get("/api/v1/assistant/mcp") or []

    def add_mcp(self, cfg: Dict) -> Dict:
        return self.post("/api/v1/assistant/mcp", cfg, timeout=120.0) or {}

    def connect_mcp(self, sid: str) -> Dict:
        return self.post(f"/api/v1/assistant/mcp/{sid}/connect", {}, timeout=120.0) or {}

    def disconnect_mcp(self, sid: str) -> Dict:
        return self.post(f"/api/v1/assistant/mcp/{sid}/disconnect", {}) or {}

    def delete_mcp(self, sid: str) -> Dict:
        return self.delete(f"/api/v1/assistant/mcp/{sid}") or {}

    # ---------------- 在线能力目录 / 工具开关 / profile（1.0.8 清单中心） ----------------
    # 端点与返回结构见 docs/智能助理-技能工具MCP清单-架构设计.md §8（已定稿）。
    # 业务错误一律 HTTP 200 + fail(code, message)，由 _unwrap 抛 RuntimeError。
    def catalog_mcp(self, q: str = "", cursor: str = "",
                    force: bool = False) -> Dict:
        """GET /catalog/mcp：MCP Registry 目录（先返缓存，force=True 同步拉）。"""
        return self.get("/api/v1/assistant/catalog/mcp",
                        {"q": q or "", "cursor": cursor or "",
                         "force": 1 if force else 0}) or {}

    def catalog_repos(self, q: str = "", force: bool = False) -> Dict:
        """GET /catalog/skills：GitHub topic:claude-skills 仓库列表。"""
        return self.get("/api/v1/assistant/catalog/skills",
                        {"q": q or "", "force": 1 if force else 0}) or {}

    def catalog_repo_skills(self, owner: str, repo: str,
                            ref: str = "", force: bool = False) -> Dict:
        """GET /catalog/skills/{owner}/{repo}：单仓库技能枚举（tree → SKILL.md）。"""
        return self.get(f"/api/v1/assistant/catalog/skills/{owner}/{repo}",
                        {"ref": ref or "", "force": 1 if force else 0}) or {}

    def catalog_install_skill(self, repo: str, ref: str = "", dirpath: str = "",
                              slug: str = "", overwrite: bool = False) -> Dict:
        """POST /catalog/skills/install：从 GitHub 装技能（原子写入 + 回滚）。"""
        return self.post("/api/v1/assistant/catalog/skills/install",
                         {"repo": repo, "ref": ref or "", "dir": dirpath or "",
                          "slug": slug or "", "overwrite": bool(overwrite)},
                         timeout=120.0) or {}

    def catalog_install_mcp(self, registry_name: str, server_id: str = "",
                            name: str = "", env: Optional[Dict] = None,
                            risk: str = "", connect: bool = True) -> Dict:
        """POST /catalog/mcp/install：从 Registry 装 MCP（配置落盘 + 尝试连接）。"""
        return self.post("/api/v1/assistant/catalog/mcp/install",
                         {"registry_name": registry_name,
                          "id": server_id or "", "name": name or "",
                          "env": env or {}, "risk": risk or "",
                          "connect": bool(connect)},
                         timeout=120.0) or {}

    def catalog_refresh(self) -> Dict:
        """POST /catalog/refresh：三源并行刷新缓存（总超时 12s，部分成功）。"""
        return self.post("/api/v1/assistant/catalog/refresh", {},
                         timeout=120.0) or {}

    def catalog_from_dir(self, path: str, overwrite: bool = False) -> Dict:
        """POST /catalog/skills/from_dir：从本地文件夹装技能（须含 SKILL.md）。"""
        return self.post("/api/v1/assistant/catalog/skills/from_dir",
                         {"path": path, "overwrite": bool(overwrite)},
                         timeout=120.0) or {}

    def catalog_from_url(self, url: str, overwrite: bool = False) -> Dict:
        """POST /catalog/skills/from_url：从 SKILL.md 链接装技能（blob 自动转 raw）。"""
        return self.post("/api/v1/assistant/catalog/skills/from_url",
                         {"url": url, "overwrite": bool(overwrite)},
                         timeout=120.0) or {}

    def tool_prefs(self) -> Dict:
        """GET /tools/prefs：{disabled[], max_tools, total, active, over}。"""
        return self.get("/api/v1/assistant/tools/prefs") or {}

    def save_tool_prefs(self, disabled: Optional[List[str]] = None,
                        max_tools: Optional[int] = None) -> Dict:
        """PUT /tools/prefs：工具启用清单 + 下发上限（8~64）。"""
        body: Dict[str, Any] = {"disabled": list(disabled or [])}
        if max_tools is not None:
            body["max_tools"] = int(max_tools)
        return self.put("/api/v1/assistant/tools/prefs", body) or {}

    def profiles(self) -> Dict:
        """GET /profiles：{auto_load, profiles[]}。"""
        return self.get("/api/v1/assistant/profiles") or {}

    def save_profiles(self, auto_load: Optional[bool] = None,
                      profiles: Optional[List[Dict]] = None) -> Dict:
        """PUT /profiles：只传要改的字段（auto_load 开关 / profile 列表）。"""
        body: Dict[str, Any] = {}
        if auto_load is not None:
            body["auto_load"] = bool(auto_load)
        if profiles is not None:
            body["profiles"] = list(profiles)
        return self.put("/api/v1/assistant/profiles", body) or {}

    def settings(self) -> Dict:
        return self.get("/api/v1/assistant/settings") or {}

    def save_settings(self, patch: Dict) -> Dict:
        return self.put("/api/v1/assistant/settings", patch) or {}

    # ---------------- 并行多任务（core/assistant/parallel.py） ----------------
    # 端点见 docs/智能助理-并行多任务-架构设计.md §5。业务错误 HTTP 200 +
    # fail(code, message)，由 _unwrap 抛 RuntimeError。
    def parallel_list(self, limit: int = 50) -> List[Dict]:
        """GET /parallel：并行计划摘要列表。"""
        return self.get("/api/v1/assistant/parallel",
                        {"limit": int(limit)}) or []

    def parallel_plan(self, goal: str, provider: str = "", model: str = "",
                      max_tasks: int = 0, workspace: str = "",
                      title: str = "",
                      subtasks: Optional[List[Dict]] = None) -> Dict:
        """POST /parallel/plan：把目标拆成子任务计划（只规划不执行）。

        `subtasks` 给了就按它拆（不调模型，确定性路径）。
        """
        body: Dict[str, Any] = {"goal": goal or "", "provider": provider or "",
                                "model": model or "", "workspace": workspace or "",
                                "title": title or ""}
        if max_tasks:
            body["max_tasks"] = int(max_tasks)
        if subtasks:
            body["subtasks"] = list(subtasks)
        return self.post("/api/v1/assistant/parallel/plan", body) or {}

    def parallel_run(self, goal: str, provider: str = "", model: str = "",
                     max_tasks: int = 0, workspace: str = "",
                     title: str = "",
                     subtasks: Optional[List[Dict]] = None) -> Dict:
        """POST /parallel/run：拆分并在后台并行执行（立即返回计划快照）。"""
        body: Dict[str, Any] = {"goal": goal or "", "provider": provider or "",
                                "model": model or "", "workspace": workspace or "",
                                "title": title or ""}
        if max_tasks:
            body["max_tasks"] = int(max_tasks)
        if subtasks:
            body["subtasks"] = list(subtasks)
        return self.post("/api/v1/assistant/parallel/run", body,
                         timeout=120.0) or {}

    def parallel_detail(self, plan_id: str) -> Dict:
        """GET /parallel/{id}：计划详情（含子任务状态与事件）。"""
        return self.get(f"/api/v1/assistant/parallel/{plan_id}") or {}

    def parallel_cancel(self, plan_id: str) -> Dict:
        """POST /parallel/{id}/cancel：取消计划（停掉正在跑的子任务）。"""
        return self.post(f"/api/v1/assistant/parallel/{plan_id}/cancel", {}) or {}

    def parallel_resume(self, plan_id: str, provider: str = "",
                        model: str = "") -> Dict:
        """POST /parallel/{id}/resume：从断点续跑（可换模型）。"""
        body: Dict[str, Any] = {"provider": provider or "", "model": model or ""}
        return self.post(f"/api/v1/assistant/parallel/{plan_id}/resume", body,
                         timeout=120.0) or {}

    def audit(self, limit: int = 100) -> List[Dict]:
        return self.get("/api/v1/assistant/audit", {"limit": limit}) or []


class AssistantStream(QThread):
    """一轮对话的 SSE 事件流。每个事件是一个 dict，类型见后端 agent.py。"""

    event = Signal(dict)
    failed = Signal(str)
    closed = Signal()

    def __init__(self, base_url: str, api_key: str, payload: Dict[str, Any],
                 parent=None):
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._payload = payload
        self._cancelled = False
        # 线程真正结束后再释放引用并销毁（在 run() 里 deleteLater 会销毁运行中的
        # QThread，同样会触发原生崩溃）
        self.finished.connect(self._cleanup)

    def start(self):  # noqa: N802 - 覆盖 QThread.start，运行期自持强引用
        _ACTIVE.append(self)
        super().start()

    def cancel(self) -> None:
        self._cancelled = True

    def _cleanup(self) -> None:
        try:
            _ACTIVE.remove(self)
        except ValueError:
            pass
        self.deleteLater()

    def run(self) -> None:  # noqa: C901
        headers = {"Accept": "text/event-stream"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            with httpx.Client(trust_env=False,
                              timeout=httpx.Timeout(connect=15.0, read=None,
                                                    write=30.0, pool=15.0)) as c:
                with c.stream("POST", f"{self._base_url}/api/v1/assistant/chat",
                              json=self._payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        resp.read()
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
                    for line in resp.iter_lines():
                        if self._cancelled:
                            break
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            if raw == "[DONE]":
                                break
                            continue
                        try:
                            ev = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(ev, dict):
                            self.event.emit(ev)
        except Exception as e:  # noqa: BLE001
            if not self._cancelled:
                self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            self.closed.emit()
