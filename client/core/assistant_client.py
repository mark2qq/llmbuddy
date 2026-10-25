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

    def settings(self) -> Dict:
        return self.get("/api/v1/assistant/settings") or {}

    def save_settings(self, patch: Dict) -> Dict:
        return self.put("/api/v1/assistant/settings", patch) or {}

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
