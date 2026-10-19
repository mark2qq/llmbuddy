# -*- coding: utf-8 -*-
"""
API 客户端（api_client.py）。

封装后端的两类接口：
    1) 管理接口 /api/v1/*   —— 统一响应 {code,message,data}（code==0 成功）
    2) OpenAI 兼容 /v1/*    —— /v1/models、/v1/chat/completions(SSE)

关键约定：
    - 所有请求 trust_env=False，绕过本机代理（本地服务不需要代理）
    - 若配置了 api_key，则附带 Authorization: Bearer <api_key>
    - chat_stream 逐块解析 SSE（data: 行），遇 [DONE] 结束，逐块回调 UI
"""
import json
from typing import Any, Callable, Dict, List, Optional

import httpx
from PySide6.QtCore import QObject, Signal


class ApiError(Exception):
    """API 错误：携带后端 code 与 message。"""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ApiClient(QObject):
    """后端 HTTP 客户端（REST + SSE）。"""

    log = Signal(str)                       # 调试日志

    def __init__(self, host: str = "127.0.0.1", port: int = 8000,
                 api_key: str = "", timeout: float = 30.0,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.host = host
        self.port = port
        self.api_key = api_key
        self.timeout = timeout

    # ------------------------------------------------------------------ #
    # 基础属性
    # ------------------------------------------------------------------ #
    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self) -> httpx.Client:
        # trust_env=False 关键：避免本地代理干扰 127.0.0.1 请求
        return httpx.Client(base_url=self.base_url, trust_env=False,
                             timeout=self.timeout, headers=self._headers())

    # ------------------------------------------------------------------ #
    # 响应解包
    # ------------------------------------------------------------------ #
    def _unwrap(self, resp: httpx.Response) -> Any:
        """解包响应：非 2xx 抛 ApiError；否则返回 data 字段（或原始 body）。"""
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, f"HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            body = resp.json()
        except Exception:
            return resp.text
        # 管理接口统一包：{code,message,data}
        if isinstance(body, dict) and "code" in body:
            if body.get("code", 0) != 0:
                raise ApiError(body.get("code", -1), body.get("message", "error"))
            return body.get("data")
        # OpenAI 兼容接口无 code 字段，直接返回
        return body

    # ------------------------------------------------------------------ #
    # 底层方法（对外也可直接使用）
    # ------------------------------------------------------------------ #
    def get(self, path: str, params: Optional[Dict] = None) -> Any:
        with self._client() as c:
            return self._unwrap(c.get(path, params=params))

    def post(self, path: str, json_body: Optional[Dict] = None) -> Any:
        with self._client() as c:
            return self._unwrap(c.post(path, json=json_body or {}))

    def put(self, path: str, json_body: Optional[Dict] = None) -> Any:
        with self._client() as c:
            return self._unwrap(c.put(path, json=json_body or {}))

    def delete(self, path: str) -> Any:
        with self._client() as c:
            return self._unwrap(c.delete(path))

    # ------------------------------------------------------------------ #
    # 管理接口便捷方法（M1~M16）
    # ------------------------------------------------------------------ #
    def health(self) -> Dict:
        """M16 健康检查（含 pool）。"""
        return self.get("/api/v1/health") or {}

    def system_info(self) -> Dict:
        """M1 系统资源信息。"""
        return self.get("/api/v1/system/info") or {}

    def scan_ports(self, count: int = 8, start: int = 7900) -> Dict:
        """M2 端口占用扫描。"""
        return self.get("/api/v1/system/ports",
                        {"count": count, "start": start}) or {"ports": [], "used": []}

    def catalog(self, force: bool = False, keyword: str = "") -> Dict:
        """M3 模型目录。"""
        return self.get("/api/v1/models/catalog",
                        {"force": str(bool(force)).lower(),
                         "keyword": keyword}) or {"items": [], "cached": False}

    def pull(self, name: str, auth: Optional[Dict] = None) -> Dict:
        """M4 触发下载。auth={user,token} 可选。"""
        body: Dict[str, Any] = {"name": name}
        if auth:
            body["auth"] = auth
        return self.post("/api/v1/models/pull", body) or {}

    def pull_status(self, name: str) -> Dict:
        """M5 查询下载进度。"""
        return self.get(f"/api/v1/models/pull/{name}") or {
            "status": "not_found", "progress": 0.0, "gguf_path": None, "error": None}

    def cancel_pull(self, name: str) -> bool:
        """M6 取消下载。"""
        data = self.delete(f"/api/v1/models/pull/{name}") or {}
        return bool(data.get("ok", False))

    def gen_key(self, model: Optional[str] = None) -> str:
        """M7 生成 API Key。传 model 则生成绑定该模型的 Key。"""
        body: Dict[str, Any] = {}
        if model:
            body["model"] = model
        data = self.post("/api/v1/keys/generate", body) or {}
        return data.get("api_key", "")

    def list_keys(self) -> List[Dict]:
        """列出全部 API Key（含 model 绑定信息）。"""
        data = self.get("/api/v1/keys") or {}
        if isinstance(data, dict):
            return data.get("keys", [])
        return []

    def pool(self) -> Dict:
        """M8 池总览。"""
        return self.get("/api/v1/pool") or {
            "max_concurrent": 0, "engine_base_port": 0, "loaded": []}

    def load_model(self, model: str, n_ctx: Optional[int] = None,
                   n_threads: Optional[int] = None,
                   n_gpu_layers: Optional[int] = None) -> Dict:
        """M9 加载模型。"""
        body: Dict[str, Any] = {"model": model}
        if n_ctx is not None:
            body["n_ctx"] = int(n_ctx)
        if n_threads is not None:
            body["n_threads"] = int(n_threads)
        if n_gpu_layers is not None:
            body["n_gpu_layers"] = int(n_gpu_layers)
        return self.post("/api/v1/pool/load", body) or {}

    def unload_model(self, model: str) -> Dict:
        """M11 卸载模型（取消加载：从池中移除）。"""
        return self.post(f"/api/v1/pool/{model}/unload") or {}

    def pause_model(self, model: str) -> Dict:
        """M17 暂停模型运行（停止运行但保留加载，可再次启动）。"""
        return self.post(f"/api/v1/pool/{model}/pause") or {}

    def resume_model(self, model: str) -> Dict:
        """M18 启动已加载模型（恢复运行）。"""
        return self.post(f"/api/v1/pool/{model}/resume") or {}

    def delete_model(self, model: str) -> Dict:
        """M19 删除本地模型（删除 gguf 文件并清注册）。"""
        return self.post(f"/api/v1/models/{model}/delete") or {}

    def model_status(self, model: str) -> Dict:
        """M10 查询某模型实例状态。"""
        return self.get(f"/api/v1/pool/{model}") or {}

    def test_model(self, model: str, prompt: str = "hi") -> Dict:
        """M12 测试模型（小推理，返回延迟与样例）。"""
        return self.get(f"/api/v1/pool/{model}/test",
                        {"prompt": prompt}) or {"ok": False}

    def concurrency(self) -> Dict:
        """M13 查询并发上限。"""
        return self.get("/api/v1/settings/concurrency") or {}

    def set_concurrency(self, max_concurrent: int) -> bool:
        """M14 设置并发上限。"""
        data = self.put("/api/v1/settings/concurrency",
                        {"max_concurrent": int(max_concurrent)}) or {}
        return bool(data.get("ok", False))

    def stop_service(self) -> bool:
        """M15 优雅停机。"""
        data = self.post("/api/v1/service/stop") or {}
        return bool(data.get("ok", False))

    # ------------------------------------------------------------------ #
    # OpenAI 兼容接口
    # ------------------------------------------------------------------ #
    def _gateway_key_candidates(self, model: str = "") -> List[str]:
        """401 后的网关 Key 候选（惰性查询，不影响开放态快路径）。

        用户通过 API 页生成过 Key 后，网关即对 /v1/* 启用鉴权；而客户端
        config.api_key 默认为空（生成的 Key 只存在于后端 keys.json），
        直接调用会 401（历史现象：API 页模型列表变空、对话发不出去）。
        这里从 keys.json 取已生成 Key 作为候选：全局 Key 优先，其次绑定
        该模型的 Key，最后其它 Key。
        """
        try:
            keys = self.list_keys() or []
        except Exception:
            return []
        global_keys: List[str] = []
        bound_keys: List[str] = []
        other_keys: List[str] = []
        for k in keys:
            key = (k or {}).get("key")
            if not key:
                continue
            bound = (k or {}).get("model") or ""
            if not bound:
                global_keys.append(key)
            elif model and bound == model:
                bound_keys.append(key)
            else:
                other_keys.append(key)
        return global_keys + bound_keys + other_keys

    def _get_models(self, api_key: str) -> List[Dict]:
        headers = self._headers()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        with httpx.Client(base_url=self.base_url, trust_env=False,
                          timeout=self.timeout, headers=headers) as c:
            data = self._unwrap(c.get("/v1/models")) or {}
        if isinstance(data, dict):
            return data.get("data", [])
        return []

    def list_models(self) -> List[Dict]:
        """GET /v1/models —— 已注册模型列表（含 loaded 标记）。

        401（网关已启用 Key 鉴权）时自动改用已生成 Key 重试，避免界面
        模型列表变空（红字：要默认显示已经加载的所有模型）。
        """
        try:
            return self._get_models("")     # _headers() 已带 config.api_key（若有）
        except ApiError as e:
            if e.code != 401:
                raise
            first_err = e
        candidates = self._gateway_key_candidates()
        if not candidates:
            return []                       # 无生成 Key 可用：按空处理（旧行为）
        last_err: ApiError = first_err
        for key in candidates:
            try:
                return self._get_models(key)
            except ApiError as e:
                last_err = e
                if e.code != 401:
                    raise
        raise last_err

    def _chat_stream_once(self, api_key: str, messages: List[Dict[str, str]],
                          model: str, on_chunk: Callable[[str], None],
                          max_tokens: int,
                          on_done: Optional[Callable[[Dict], None]],
                          cancel_check: Optional[Callable[[], bool]]) -> Dict:
        """单次尝试：带指定 Key 请求 /v1/chat/completions（stream:true）。"""
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "max_tokens": int(max_tokens),
        }
        url = f"{self.base_url}/v1/chat/completions"
        headers = self._headers()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        collected = ""
        finish_chunk: Dict[str, Any] = {}
        cancelled = False

        with httpx.Client(trust_env=False, timeout=600.0, headers=headers) as c:
            with c.stream("POST", url, json=payload) as resp:
                if resp.status_code >= 400:
                    # 流式响应必须先 read() 才能访问 text/json；否则 httpx 抛
                    # ResponseNotRead，会掩盖真实状态码（401 重试逻辑被绕过，
                    # 用户只看到晦涩的 httpx 内部错误——红字 bug）。
                    resp.read()
                    raise ApiError(resp.status_code, f"HTTP {resp.status_code}: {resp.text[:300]}")
                for line in resp.iter_lines():
                    if cancel_check and cancel_check():
                        cancelled = True
                        break
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str == "[DONE]":
                        break
                    if not data_str:
                        continue
                    try:
                        obj = json.loads(data_str)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            collected += piece
                            on_chunk(piece)
                        if obj.get("finish_reason"):
                            finish_chunk = obj

        result = {"text": collected, "finish": finish_chunk, "cancelled": cancelled}
        if on_done:
            on_done(result)
        return result

    def chat_stream(self, messages: List[Dict[str, str]], model: str,
                    on_chunk: Callable[[str], None],
                    max_tokens: int = 2048,
                    on_done: Optional[Callable[[Dict], None]] = None,
                    cancel_check: Optional[Callable[[], bool]] = None) -> Dict:
        """
        调用 /v1/chat/completions（stream:true），逐块解析 SSE 并回调。

        参数：
            messages    多轮对话消息列表 [{"role","content"}, ...]
            model       模型名
            on_chunk    每收到一段增量文本时的回调（用于打字机效果）
            max_tokens  最大生成 token
            on_done     结束回调，传入 {"text": 全文, "finish": 最后一条 chunk}
            cancel_check 可选：每轮询一行 SSE 时调用，返回 True 则终止读取
                        （供「停止生成」按钮使用）
        返回：
            {"text": 拼接后的完整文本, "cancelled": 是否被取消}

        401（网关已启用 Key 鉴权）时自动改用已生成 Key 重试；重试发生在
        流式请求建立之前，不会产生半截流（与 list_models 同一机制）。
        """
        def _attempt(api_key: str) -> Dict:
            return self._chat_stream_once(api_key, messages, model, on_chunk,
                                          max_tokens, on_done, cancel_check)

        try:
            return _attempt("")             # _headers() 已带 config.api_key（若有）
        except ApiError as e:
            if e.code != 401:
                raise
            first_err = e
        candidates = self._gateway_key_candidates(model)
        if not candidates:
            raise first_err
        last_err: ApiError = first_err
        for key in candidates:
            try:
                return _attempt(key)
            except ApiError as e:
                last_err = e
                if e.code != 401:
                    raise
        raise last_err
