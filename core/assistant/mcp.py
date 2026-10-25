# -*- coding: utf-8 -*-
"""
MCP 客户端与服务器管理（core/assistant/mcp.py）。

支持两种传输：
    stdio   本机子进程（npx / uvx / 任意可执行文件），JSON-RPC 按行分隔
    http    Streamable HTTP（POST JSON-RPC，可能回 SSE 流）

管理的服务器配置持久化在 assistant/config.json 的 mcp_servers 里：

    {"id": "filesystem", "name": "文件系统", "transport": "stdio",
     "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
     "env": {}, "enabled": true, "risk": "medium"}

连接成功后把每个 MCP 工具注册进 ToolRegistry，名字为 mcp__<server>__<tool>，
风险等级取服务器配置（默认 medium，写操作类服务器建议配 high）。
"""
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from core.assistant.store import audit, load_config, save_config

PROTOCOL_VERSION = "2024-11-05"
CALL_TIMEOUT = 90.0


class MCPError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 单个客户端
# ----------------------------------------------------------------------------
class MCPClient:
    """一个 MCP 服务器的连接（stdio 或 http）。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.server_id = str(self.cfg.get("id") or "mcp")
        self.transport = (self.cfg.get("transport") or "stdio").lower()
        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._buffer: List[Dict[str, Any]] = []
        self._stderr_tail: List[str] = []
        self._session_id = ""
        self._closed = False
        self.server_info: Dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # stdio 进程
    # ------------------------------------------------------------------ #
    def _resolve_command(self, command: str) -> List[str]:
        """Windows 下 npx/uvx 是 .cmd，需要经 cmd /c 启动。"""
        cmd = str(command or "").strip()
        if not cmd:
            raise MCPError("未配置 command")
        if os.name == "nt":
            hit = shutil.which(cmd) or shutil.which(cmd + ".cmd") or shutil.which(cmd + ".exe")
            if hit and hit.lower().endswith((".cmd", ".bat")):
                return ["cmd", "/c", hit]
            if hit:
                return [hit]
            return ["cmd", "/c", cmd]
        return [cmd]

    def start_stdio(self) -> None:
        argv = self._resolve_command(self.cfg.get("command"))
        argv += [str(a) for a in (self.cfg.get("args") or [])]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (self.cfg.get("env") or {}).items()})
        cwd = self.cfg.get("cwd") or None
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env, cwd=cwd,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if os.name == "nt" else 0))
        except Exception as e:
            raise MCPError(f"启动 MCP 服务器失败：{e}")
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"mcp-{self.server_id}")
        self._reader.start()
        threading.Thread(target=self._stderr_loop, daemon=True).start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = (line or "").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if isinstance(msg, dict) and msg.get("id") is not None:
                with self._lock:
                    slot = self._pending.get(int(msg["id"]))
                if slot is not None:
                    slot["resp"] = msg
                    slot["event"].set()
            elif isinstance(msg, dict) and msg.get("method"):
                with self._lock:
                    self._buffer.append(msg)

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail.append((line or "").rstrip())
            if len(self._stderr_tail) > 40:
                self._stderr_tail.pop(0)

    def _send_stdio(self, method: str, params: Dict[str, Any],
                    timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        if self._proc is None or self._proc.poll() is not None:
            raise MCPError(f"MCP 进程未运行（{self.server_id}）")
        with self._lock:
            self._next_id += 1
            rid = self._next_id
            slot = {"event": threading.Event(), "resp": None}
            self._pending[rid] = slot
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:
            with self._lock:
                self._pending.pop(rid, None)
            raise MCPError(f"写入 MCP 进程失败：{e}")
        got = slot["event"].wait(timeout)
        with self._lock:
            self._pending.pop(rid, None)
        if not got or slot["resp"] is None:
            tail = "\n".join(self._stderr_tail[-8:])
            raise MCPError(f"MCP 调用超时：{method}" + (f"\nstderr:\n{tail}" if tail else ""))
        resp = slot["resp"]
        if resp.get("error"):
            err = resp["error"]
            raise MCPError(f"MCP 错误：{err.get('message') if isinstance(err, dict) else err}")
        return resp.get("result") or {}

    def _notify_stdio(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "method": method, "params": params or {}},
                ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # http 传输
    # ------------------------------------------------------------------ #
    def _send_http(self, method: str, params: Dict[str, Any],
                   timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        url = str(self.cfg.get("url") or "").strip()
        if not url:
            raise MCPError("未配置 url")
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        headers.update({str(k): str(v) for k, v in (self.cfg.get("headers") or {}).items()})
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        with httpx.Client(trust_env=True, timeout=timeout) as c:
            resp = c.post(url, json=body, headers=headers)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code}：{resp.text[:300]}")
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        msg = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == rid:
                        if msg.get("error"):
                            raise MCPError(f"MCP 错误：{msg['error']}")
                        return msg.get("result") or {}
            raise MCPError("SSE 响应中没有匹配的 result")
        try:
            msg = resp.json()
        except Exception:
            return {}
        if isinstance(msg, dict) and msg.get("error"):
            raise MCPError(f"MCP 错误：{msg['error']}")
        return (msg or {}).get("result") or {}

    # ------------------------------------------------------------------ #
    # 协议
    # ------------------------------------------------------------------ #
    def _send(self, method: str, params: Dict[str, Any],
              timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        if self.transport == "http":
            return self._send_http(method, params, timeout)
        return self._send_stdio(method, params, timeout)

    def start(self) -> Dict[str, Any]:
        """启动传输并完成 initialize 握手。"""
        if self.transport != "http":
            self.start_stdio()
        res = self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "llmBuddy-assistant", "version": "1.0.8"},
        }, timeout=60)
        self.server_info = (res or {}).get("serverInfo") or {}
        if self.transport != "http":
            self._notify_stdio("notifications/initialized")
        return res or {}

    def list_tools(self) -> List[Dict[str, Any]]:
        res = self._send("tools/list", {}, timeout=60)
        items = (res or {}).get("tools") or []
        return [t for t in items if isinstance(t, dict) and t.get("name")]

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        res = self._send("tools/call", {"name": name, "arguments": arguments or {}})
        is_error = bool((res or {}).get("isError"))
        parts: List[str] = []
        for item in (res or {}).get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif item.get("type") == "resource":
                parts.append(json.dumps(item.get("resource") or {}, ensure_ascii=False)[:4000])
            else:
                parts.append(json.dumps(item, ensure_ascii=False)[:2000])
        text = "\n".join(p for p in parts if p).strip() or "（无输出）"
        if is_error:
            raise MCPError(text[:1500])
        return text

    def close(self) -> None:
        self._closed = True
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


# ----------------------------------------------------------------------------
# 服务器池
# ----------------------------------------------------------------------------
class MCPManager:
    """MCP 服务器生命周期 + 工具注册。"""

    def __init__(self) -> None:
        self._clients: Dict[str, MCPClient] = {}
        self._status: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ---------------- 配置 ----------------
    @staticmethod
    def servers() -> List[Dict[str, Any]]:
        return (load_config().get("mcp_servers") or [])

    @staticmethod
    def save_servers(items: List[Dict[str, Any]]) -> None:
        save_config({"mcp_servers": items})

    def add_server(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        cfg = dict(cfg or {})
        cfg.setdefault("id", f"mcp{int(time.time())}")
        cfg.setdefault("name", cfg["id"])
        cfg.setdefault("transport", "stdio")
        cfg.setdefault("enabled", True)
        cfg.setdefault("risk", "medium")
        items = [s for s in self.servers() if s.get("id") != cfg["id"]]
        items.append(cfg)
        self.save_servers(items)
        return cfg

    def remove_server(self, server_id: str) -> bool:
        self.disconnect(server_id)
        items = [s for s in self.servers() if s.get("id") != server_id]
        self.save_servers(items)
        with self._lock:
            self._status.pop(server_id, None)
        return True

    def install_from_registry(self, name: str, item: Optional[Dict[str, Any]] = None,
                              env: Optional[Dict[str, Any]] = None,
                              server_id: str = "", display_name: str = "",
                              risk: str = "high",
                              connect: bool = True, registry=None) -> Dict[str, Any]:
        """从在线 MCP Registry 条目安装一台服务器（1.0.8 增量）。

        两步刻意分开（架构 §8 时序图）：
            1. `add_server(cfg)` 先落配置（原子写）——即使 connect 失败，
               用户也能就地修 env/命令再连，不用重装；
            2. connect 失败 -> `CatalogError(2025)`，`data.server` 保留配置。

        Args:
            name: Registry 条目名（`registry_name`），用于在线查找条目。
            item: 已归一化的条目（`catalog.normalize_mcp_server()` 的结果）；
                为空时按 `name` 现拉一次目录（取缓存优先）。
            env: 用户填的环境变量（必填项由 catalog 校验）。
            server_id: 显式 id（为空则由 catalog 归一化 + 去冲突）。
            display_name: 展示名（为空取条目 title）。
            risk: 风险等级，默认 high（第三方在线来源）。
            connect: 安装后是否立即连接。
            registry: ToolRegistry（连接成功后注册工具用）。

        Raises:
            CatalogError(2020/2021): 目录不可达且无缓存
            CatalogError(2022): 条目无法映射成配置
            CatalogError(2024): 必填环境变量缺失
            CatalogError(2025): 配置已保存但连接失败
        """
        from core.assistant import catalog as catalog_mod

        if item is None:
            item = self._find_registry_item(name)
        config = catalog_mod.build_mcp_config(
            item, server_id=server_id, name=display_name, env=env, risk=risk,
            existing_ids=[str(s.get("id") or "") for s in self.servers()])
        cfg = self.add_server(config)
        audit("mcp_installed_online", server=cfg.get("id"),
              registry_name=cfg.get("registry_name") or name,
              version=cfg.get("version") or "", transport=cfg.get("transport"))
        result: Dict[str, Any] = {"server": cfg, "connected": False, "error": ""}
        if not connect:
            return result
        try:
            self.connect(cfg["id"], registry)
            result["connected"] = True
        except Exception as e:                      # noqa: BLE001
            err = str(e)
            result["error"] = err[:400]
            raise catalog_mod.CatalogError(
                catalog_mod.ERR_CONNECT_FAILED,
                f"已保存配置但连接失败：{err}",
                result) from e
        return result

    @staticmethod
    def _find_registry_item(name: str) -> Dict[str, Any]:
        """按 registry_name 在目录缓存/在线列表里找条目（找不到抛 2020/2022）。"""
        from core.assistant import catalog as catalog_mod
        target = str(name or "").strip()
        if not target:
            raise catalog_mod.CatalogError(catalog_mod.ERR_BAD_BUNDLE,
                                           "registry_name 不能为空")
        cfg = load_config()
        found: Optional[Dict[str, Any]] = None
        for force in (False, True):
            try:
                listing = catalog_mod.list_mcp(cfg, force=force)
            except catalog_mod.CatalogError:
                if force:
                    raise
                continue
            for row in listing.get("items") or []:
                if str(row.get("registry_name") or "") == target:
                    found = row
                    break
            if found is not None:
                break
            if listing.get("items"):
                break                     # 缓存里没有就看一次在线，别反复拉
        if found is None:
            raw = self._registry_raw_lookup(cfg, target)
            if raw is None:
                raise catalog_mod.CatalogError(
                    catalog_mod.ERR_BAD_BUNDLE,
                    f"在线目录里找不到该 MCP：{target}")
            found = catalog_mod.normalize_mcp_server(raw)
        return found

    @staticmethod
    def _registry_raw_lookup(cfg: Dict[str, Any],
                             target: str) -> Optional[Dict[str, Any]]:
        """按名字直接查 Registry 详情（服务端 search 可能不精确匹配）。"""
        from core.assistant import catalog as catalog_mod
        try:
            payload, _headers = catalog_mod._get_json(
                cfg, catalog_mod.REGISTRY_BASE,
                params={"search": target, "limit": "50"})
        except catalog_mod.CatalogError:
            return None
        norm = catalog_mod._normalize_registry_list(payload)
        for row in norm["items"]:
            if str(row.get("registry_name") or "") == target:
                return row.get("raw") or row
        return None

    # ---------------- 连接 ----------------
    def connect(self, server_id: str, registry=None) -> Dict[str, Any]:
        cfg = next((s for s in self.servers() if s.get("id") == server_id), None)
        if cfg is None:
            raise MCPError(f"未找到 MCP 服务器：{server_id}")
        self.disconnect(server_id)
        client = MCPClient(cfg)
        try:
            client.start()
            tools = client.list_tools()
        except Exception as e:
            client.close()
            info = {"id": server_id, "status": "error", "error": str(e), "tools": []}
            with self._lock:
                self._status[server_id] = info
            audit("mcp_connect_failed", server=server_id, error=str(e))
            raise MCPError(str(e))
        with self._lock:
            self._clients[server_id] = client
            self._status[server_id] = {
                "id": server_id, "status": "connected",
                "server_info": client.server_info, "tools": tools, "error": "",
            }
        audit("mcp_connected", server=server_id, tool_count=len(tools))
        if registry is not None:
            self._register_tools(server_id, client, tools, registry,
                                 risk=cfg.get("risk") or "medium")
        return self._status[server_id]

    def _register_tools(self, server_id: str, client: MCPClient,
                        tools: List[Dict[str, Any]], registry, risk: str) -> None:
        from core.assistant.tools import Tool, ToolResult
        prefix = f"mcp__{server_id}__"
        registry.unregister_prefix(prefix)
        for t in tools:
            raw_name = t["name"]
            tool_name = prefix + raw_name
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}

            def handler(args, ctx, _c=client, _n=raw_name):
                try:
                    return ToolResult(True, content=_c.call_tool(_n, args or {}))
                except Exception as e:
                    return ToolResult(False, error=str(e))

            registry.register(Tool(
                name=tool_name,
                description=(t.get("description") or f"MCP 工具 {raw_name}")[:800],
                parameters=schema, risk=risk, handler=handler,
                source=f"mcp:{server_id}",
                summarize=lambda a, _n=raw_name, _s=server_id: f"调用 MCP[{_s}] {_n}",
                meta={"mcp_tool": raw_name, "server": server_id}))

    def disconnect(self, server_id: str) -> None:
        with self._lock:
            client = self._clients.pop(server_id, None)
        if client is not None:
            client.close()
        with self._lock:
            if server_id in self._status:
                self._status[server_id]["status"] = "disconnected"
                self._status[server_id]["tools"] = []

    def reregister(self, registry) -> None:
        """把已连接服务器的工具重新注册进新的 registry（工作区切换后调用）。

        只重注册，不重启子进程——重建 registry 时重连 MCP 既慢又可能失败。
        """
        with self._lock:
            snapshot = [(sid, client, dict(self._status.get(sid) or {}))
                        for sid, client in self._clients.items()]
            risks = {s.get("id"): s.get("risk") or "medium" for s in self.servers()}
        for sid, client, st in snapshot:
            tools = st.get("tools") or []
            if tools:
                self._register_tools(sid, client, tools, registry,
                                     risk=risks.get(sid, "medium"))

    def call(self, server_id: str, tool: str, args: Dict[str, Any]) -> str:
        with self._lock:
            client = self._clients.get(server_id)
        if client is None:
            raise MCPError(f"MCP 服务器未连接：{server_id}")
        return client.call_tool(tool, args)

    def status(self) -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            statuses = dict(self._status)
            connected = set(self._clients.keys())
        for s in self.servers():
            sid = s.get("id")
            st = statuses.get(sid) or {}
            out.append({
                "id": sid, "name": s.get("name") or sid,
                "transport": s.get("transport"), "enabled": s.get("enabled", True),
                "risk": s.get("risk", "medium"),
                "status": "connected" if sid in connected else st.get("status", "disconnected"),
                "error": st.get("error", ""),
                "tool_count": len(st.get("tools") or []),
                "tools": [t.get("name") for t in (st.get("tools") or [])],
                "server_info": st.get("server_info") or {},
                "command": s.get("command") or s.get("url") or "",
            })
        return out

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._clients.keys())
        for sid in ids:
            self.disconnect(sid)


_MANAGER: Optional[MCPManager] = None
_LOCK = threading.Lock()


def get_mcp_manager() -> MCPManager:
    global _MANAGER
    if _MANAGER is None:
        with _LOCK:
            if _MANAGER is None:
                _MANAGER = MCPManager()
    return _MANAGER
