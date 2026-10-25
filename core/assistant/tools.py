# -*- coding: utf-8 -*-
"""
工具注册表与内置工具（core/assistant/tools.py）。

内置工具覆盖「操作本机文件 + 执行命令 + 读取网页 + 调用技能/MCP」四类，
每个工具带 risk 等级（low/medium/high），由 approval.py 决定是否需要人工批准：

    low     只读、可逆、无外发      —— 放行
    medium  写入、可逆、有副作用    —— 默认需审批
    high    删除、执行命令、越界写  —— 默认需审批，且 UI 用红色卡片

沙箱约定：所有相对路径以「工作区」为根；绝对路径也允许，但一旦越出工作区，
风险自动升到 high（OpenClaw 的 workspace + permission 同构策略）。
"""
import fnmatch
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

MAX_OUTPUT = 20000          # 单个工具返回给模型的字符上限
TRASH_DIRNAME = ".trash"
#: 工具下发上限默认值（与 store.DEFAULT_MAX_TOOLS 保持一致，PRD §4.1 P0-5）
DEFAULT_MAX_TOOLS = 24
MAX_MAX_TOOLS = 64


@dataclass
class ToolResult:
    """工具执行结果。ok=False 时 error 必填（模型据此自我修正）。"""

    ok: bool
    content: str = ""
    data: Any = None
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "content": self.content,
                "data": self.data, "error": self.error}


@dataclass
class Tool:
    """一个可被模型调用的工具。"""

    name: str
    description: str
    parameters: Dict[str, Any]
    risk: str
    handler: Callable[[Dict[str, Any], Dict[str, Any]], ToolResult]
    source: str = "builtin"
    # 可选：按参数动态升级风险（如越界写入 -> high）
    escalate: Optional[Callable[[Dict[str, Any]], str]] = None
    # 高风险动作的中文摘要，给审批卡片展示（比裸参数可读）
    summarize: Optional[Callable[[Dict[str, Any]], str]] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def openai_schema(self) -> Dict[str, Any]:
        return {"type": "function",
                "function": {"name": self.name,
                             "description": self.description,
                             "parameters": self.parameters}}

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "risk": self.risk, "source": self.source,
                "parameters": self.parameters, "meta": self.meta}


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（已截断，原始长度 {len(text)} 字符）"


def _risk_rank(r: str) -> int:
    return {"low": 0, "medium": 1, "high": 2}.get((r or "low").lower(), 0)


class ToolRegistry:
    """工具集合。MCP / 技能会往这里动态注册。"""

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace or os.getcwd())
        self._tools: Dict[str, Tool] = {}
        self._lock = __import__("threading").RLock()
        # 下发过滤（架构 §5 A2：只存值、不做 IO；registry 仍是纯内存对象）
        self._filter: Dict[str, Any] = {"disabled": set(), "allow": None,
                                        "max_tools": DEFAULT_MAX_TOOLS}
        self._register_builtin()

    # ------------------------------------------------------------------ #
    # 注册与查询
    # ------------------------------------------------------------------ #
    def register(self, tool: Tool, replace: bool = True) -> None:
        with self._lock:
            if not replace and tool.name in self._tools:
                return
            self._tools[tool.name] = tool

    def unregister(self, name: str) -> bool:
        with self._lock:
            return self._tools.pop(name, None) is not None

    def unregister_prefix(self, prefix: str) -> None:
        with self._lock:
            for n in [n for n in self._tools if n.startswith(prefix)]:
                self._tools.pop(n, None)

    def get(self, name: str) -> Optional[Tool]:
        with self._lock:
            return self._tools.get(name)

    # ------------------------------------------------------------------ #
    # 下发过滤（1.0.8 增量：工具开关 + 上限裁剪 + 同源出口）
    # ------------------------------------------------------------------ #
    def set_filter(self, disabled: Optional[Any] = None,
                   allow: Optional[Any] = None,
                   max_tools: Optional[int] = None) -> None:
        """设置下发过滤条件（只存值，不读盘、不重算缓存）。

        Args:
            disabled: 被禁用的工具名集合（None 表示不改）。
            allow: 额外白名单（None = 不额外限制；空 list 会被当成
                「一个都不许」，这是 PRD 语义，不做特殊处理）。
            max_tools: 下发上限；`<=0` 视为「不裁剪」（供内部调用方要全量）。
        """
        with self._lock:
            if disabled is not None:
                self._filter["disabled"] = {str(x) for x in disabled}
            if allow is not None:
                self._filter["allow"] = ([str(x) for x in allow]
                                         if not isinstance(allow, str) else [allow])
            if max_tools is not None:
                try:
                    val = int(max_tools)
                except (TypeError, ValueError):
                    val = DEFAULT_MAX_TOOLS
                self._filter["max_tools"] = val if val > 0 else None

    def filter_state(self) -> Dict[str, Any]:
        """当前过滤条件的快照（UI / 测试用）。"""
        with self._lock:
            return {"disabled": sorted(self._filter.get("disabled") or set()),
                    "allow": (None if self._filter.get("allow") is None
                              else list(self._filter.get("allow") or [])),
                    "max_tools": self._filter.get("max_tools")}

    @staticmethod
    def _priority_key(tool: Tool) -> tuple:
        """裁剪优先级（架构 §6.2 / PRD §4.1 P0-5）。

        顺序：内置 low -> web_fetch -> skill_* -> 内置 medium 写入
              -> 内置 high -> MCP 工具（按 source 顺序，server 内按 name）。
        用元组排序而不是硬编码名单：新增内置工具按 risk 自动落位，
        漏配也只会退到「高危」档（安全方向）。
        """
        source = str(tool.source or "builtin")
        name = tool.name
        risk = (tool.risk or "low").lower()
        is_mcp = source.startswith("mcp:")
        if is_mcp:
            group = 6
        elif source == "skill" or name.startswith("skill_"):
            group = 3
        elif name == "web_fetch":
            group = 2
        elif risk == "low":
            group = 1
        elif risk == "high":
            group = 5
        else:                      # medium（写入类）
            group = 4
        return (group, source, name)

    def active(self, disabled: Optional[Any] = None,
               allow: Optional[Any] = None) -> List[Tool]:
        """未被禁用且（allow 为空 或 在 allow 内）的工具，按 §6.2 优先级排序。

        注意 `allow` 的「空」语义：`None` = 不限制；`[]` = 一个都不给。
        """
        with self._lock:
            disabled_set = ({str(x) for x in disabled} if disabled is not None
                            else set(self._filter.get("disabled") or set()))
            allow_list = (allow if allow is not None
                          else self._filter.get("allow"))
            allow_set = None if allow_list is None else {str(x) for x in allow_list}
            tools = list(self._tools.values())
        out = [t for t in tools
               if t.name not in disabled_set
               and (allow_set is None or t.name in allow_set)]
        out.sort(key=self._priority_key)
        return out

    def select_schemas(self, disabled: Optional[Any] = None,
                       allow: Optional[Any] = None,
                       max_tools: Optional[int] = None) -> List[Dict[str, Any]]:
        """**下发的唯一出口**（架构 §5 A3）：过滤 + 排序 + 裁到 max_tools -> schema。

        参数全 None 时用 `set_filter()` 存的值；显式传入时以传入值为准
        （profile 收窄白名单用）。`max_tools <= 0` 表示不裁剪。
        """
        if max_tools is None:
            max_tools = self._filter.get("max_tools")
        tools = self.active(disabled=disabled, allow=allow)
        try:
            limit = int(max_tools)
        except (TypeError, ValueError):
            limit = DEFAULT_MAX_TOOLS
        if limit > 0:
            tools = tools[:limit]
        return [t.openai_schema() for t in tools]

    def list(self) -> List[Dict[str, Any]]:
        """工具清单（含 `enabled` 字段：是否在当前过滤条件下会下发）。

        `enabled` 的判定只看 disabled / allow，**不看 max_tools 裁剪**——
        被上限裁掉的工具由前端 tooltip「超出上限未下发」表达，语义分开，
        避免用户关掉开关后仍看不到它。
        """
        with self._lock:
            disabled_set = set(self._filter.get("disabled") or set())
            allow_list = self._filter.get("allow")
        allow_set = None if allow_list is None else {str(x) for x in allow_list}
        with self._lock:
            tools = sorted(self._tools.values(), key=lambda x: (x.source, x.name))
        out: List[Dict[str, Any]] = []
        for t in tools:
            row = t.to_dict()
            row["enabled"] = (t.name not in disabled_set
                              and (allow_set is None or t.name in allow_set))
            out.append(row)
        return out

    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._tools.keys())

    def openai_schemas(self) -> List[Dict[str, Any]]:
        """兼容旧签名：内部走 `select_schemas()`（orchestrator 也调这里，
        所以技能内下发同样遵守禁用与上限——架构 §3.2 的期望行为）。
        """
        return self.select_schemas()

    def risk_of(self, name: str, args: Dict[str, Any]) -> str:
        tool = self.get(name)
        if tool is None:
            return "high"
        risk = tool.risk
        if tool.escalate:
            try:
                risk = max([risk, tool.escalate(args or {})], key=_risk_rank)
            except Exception:
                pass
        return risk

    def describe_call(self, name: str, args: Dict[str, Any]) -> str:
        tool = self.get(name)
        if tool and tool.summarize:
            try:
                return tool.summarize(args or {})
            except Exception:
                pass
        return f"{name}({json.dumps(args or {}, ensure_ascii=False)[:200]})"

    # ------------------------------------------------------------------ #
    # 路径沙箱
    # ------------------------------------------------------------------ #
    def resolve(self, path: str) -> str:
        p = str(path or "").strip().strip('"').strip("'")
        if not p:
            p = "."
        p = os.path.expandvars(os.path.expanduser(p))
        if not os.path.isabs(p):
            p = os.path.join(self.workspace, p)
        return os.path.abspath(p)

    def inside(self, abspath: str) -> bool:
        try:
            root = os.path.normcase(self.workspace)
            target = os.path.normcase(abspath)
            return os.path.commonpath([root, target]) == root
        except Exception:
            return False

    def _outside_risk(self, args: Dict[str, Any], keys=("path", "dst", "src")) -> str:
        for k in keys:
            v = (args or {}).get(k)
            if v and not self.inside(self.resolve(v)):
                return "high"
        return "low"

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def call(self, name: str, args: Dict[str, Any],
             ctx: Optional[Dict[str, Any]] = None) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(False, error=f"未知工具：{name}")
        # 原生 function calling 的 arguments 是 JSON 字符串（流式增量拼起来的），
        # 编排器/直接调用方可能原样传进来；这里统一兜一次，避免 handler 拿到 str。
        if isinstance(args, str):
            text = args.strip() or "{}"
            try:
                args = json.loads(text)
            except Exception:
                return ToolResult(False, error=f"参数不是合法 JSON：{text[:200]}")
        if not isinstance(args, dict):
            args = {}
        t0 = time.time()
        try:
            res = tool.handler(args or {}, ctx or {})
            if not isinstance(res, ToolResult):
                res = ToolResult(True, content=str(res))
        except Exception as e:                       # 工具内部异常不炸 Agent
            res = ToolResult(False, error=f"{type(e).__name__}: {e}")
        res.content = _truncate(res.content or "")
        try:
            (ctx or {}).get("_on_result", lambda *_: None)(
                name, args, res, int((time.time() - t0) * 1000))
        except Exception:
            pass
        return res

    # ------------------------------------------------------------------ #
    # 内置工具注册
    # ------------------------------------------------------------------ #
    def _register_builtin(self) -> None:
        def reg(name, desc, params, risk, handler, **kw):
            self.register(Tool(name=name, description=desc, parameters=params,
                               risk=risk, handler=handler, **kw))

        # ---------------- 文件读取类（low） ----------------
        reg("fs_list", "列出目录内容（默认工作区）。返回文件名、大小、是否目录。",
            {"type": "object", "properties": {
                "path": {"type": "string", "description": "目录路径，默认当前工作区"},
                "recursive": {"type": "boolean", "description": "是否递归列出，默认 false"}},
             "required": []},
            "low", self._t_list, summarize=lambda a: f"列出目录 {a.get('path', '.')}")

        reg("fs_read", "读取文本文件内容，支持按行区间读取。",
            {"type": "object", "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "起始行（1 起），默认 1"},
                "limit": {"type": "integer", "description": "读取行数，默认 300"}},
             "required": ["path"]},
            "low", self._t_read, summarize=lambda a: f"读取 {a.get('path')}")

        reg("fs_glob", "按通配符查找文件（如 **/*.py）。",
            {"type": "object", "properties": {
                "pattern": {"type": "string"},
                "root": {"type": "string", "description": "起始目录，默认工作区"},
                "limit": {"type": "integer"}},
             "required": ["pattern"]},
            "low", self._t_glob, summarize=lambda a: f"查找 {a.get('pattern')}")

        reg("fs_grep", "在文件内容中正则搜索，返回 文件:行号:内容。",
            {"type": "object", "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "文件或目录，默认工作区"},
                "include": {"type": "string", "description": "文件名通配，如 *.py"},
                "limit": {"type": "integer"}},
             "required": ["pattern"]},
            "low", self._t_grep, summarize=lambda a: f"搜索 {a.get('pattern')}")

        reg("fs_info", "查看路径的详细信息（大小、修改时间、是否目录）。",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"]},
            "low", self._t_info, summarize=lambda a: f"查看信息 {a.get('path')}")

        # ---------------- 写入类（medium） ----------------
        reg("fs_write", "写入文本文件（覆盖或追加），自动创建父目录。",
            {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"]}},
             "required": ["path", "content"]},
            "medium", self._t_write, escalate=self._outside_risk,
            summarize=lambda a: f"写入 {a.get('path')}（{len(str(a.get('content') or ''))} 字符）")

        reg("fs_edit", "精确替换文件中的一段文本（首次出现），失败会说明原因。",
            {"type": "object", "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"}},
             "required": ["path", "old_text", "new_text"]},
            "medium", self._t_edit, escalate=self._outside_risk,
            summarize=lambda a: f"编辑 {a.get('path')}")

        reg("fs_mkdir", "创建目录（含多级）。",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"]},
            "medium", self._t_mkdir, escalate=self._outside_risk,
            summarize=lambda a: f"新建目录 {a.get('path')}")

        reg("fs_move", "移动 / 重命名文件或目录。",
            {"type": "object", "properties": {
                "src": {"type": "string"}, "dst": {"type": "string"}},
             "required": ["src", "dst"]},
            "medium", self._t_move, escalate=self._outside_risk,
            summarize=lambda a: f"移动 {a.get('src')} -> {a.get('dst')}")

        # ---------------- 高危类（high） ----------------
        reg("fs_delete", "删除文件或目录（回收站式删除，可从 .trash 恢复）。",
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"]},
            "high", self._t_delete,
            summarize=lambda a: f"删除 {a.get('path')}")

        reg("shell_run", "在本机执行命令行命令，返回 stdout/stderr 与退出码。",
            {"type": "object", "properties": {
                "command": {"type": "string"},
                "cwd": {"type": "string", "description": "工作目录，默认工作区"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"}},
             "required": ["command"]},
            "high", self._t_shell,
            summarize=lambda a: f"执行命令：{a.get('command')}")

        reg("web_fetch", "抓取网页或接口内容（GET），返回纯文本。",
            {"type": "object", "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer"}},
             "required": ["url"]},
            "medium", self._t_fetch, summarize=lambda a: f"访问网络 {a.get('url')}")

        # ---------------- 技能类 ----------------
        reg("skill_list", "列出本机已安装的技能包。",
            {"type": "object", "properties": {}}, "low", self._t_skill_list,
            summarize=lambda a: "列出技能")

        reg("skill_run", "运行一个技能包：由 LangGraph 编排其内部子任务。",
            {"type": "object", "properties": {
                "name": {"type": "string"},
                "task": {"type": "string", "description": "交给该技能的具体任务"}},
             "required": ["name", "task"]},
            "medium", self._t_skill_run,
            summarize=lambda a: f"运行技能 {a.get('name')}：{str(a.get('task'))[:80]}")

    # ------------------------------------------------------------------ #
    # 文件工具实现
    # ------------------------------------------------------------------ #
    def _t_list(self, args, ctx) -> ToolResult:
        root = self.resolve(args.get("path", "."))
        if not os.path.isdir(root):
            return ToolResult(False, error=f"不是目录：{root}")
        rows = []

        def walk(d, depth):
            if depth > 3:
                return
            try:
                entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name))
            except OSError as e:
                rows.append(f"[无法读取] {d}: {e}")
                return
            for e in entries[:300]:
                try:
                    size = e.stat().st_size if e.is_file() else 0
                except OSError:
                    size = 0
                rel = os.path.relpath(e.path, root)
                rows.append(f"{'[D]' if e.is_dir() else '[F]'} {rel}"
                            + ("" if e.is_dir() else f"  {size}B"))
                if args.get("recursive") and e.is_dir() and e.name != TRASH_DIRNAME:
                    walk(e.path, depth + 1)

        walk(root, 0)
        head = f"目录：{root}\n共 {len(rows)} 项\n"
        return ToolResult(True, content=head + "\n".join(rows[:400]), data={"path": root})

    def _t_read(self, args, ctx) -> ToolResult:
        path = self.resolve(args.get("path"))
        if not os.path.isfile(path):
            return ToolResult(False, error=f"文件不存在：{path}")
        try:
            size = os.path.getsize(path)
            if size > 8 * 1024 * 1024:
                return ToolResult(False, error=f"文件过大（{size} 字节），请用 fs_grep 定位后再读")
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return ToolResult(False, error=str(e))
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 300), 2000))
        chunk = lines[offset - 1: offset - 1 + limit]
        body = "".join(f"{i + offset:>5}| {ln}" for i, ln in enumerate(chunk))
        tail = "" if offset - 1 + limit >= len(lines) else \
            f"\n…（共 {len(lines)} 行，可用 offset 继续读取）"
        return ToolResult(True, content=f"文件：{path}\n{body}{tail}",
                          data={"path": path, "total_lines": len(lines)})

    def _t_glob(self, args, ctx) -> ToolResult:
        pattern = str(args.get("pattern") or "*")
        root = self.resolve(args.get("root", "."))
        limit = max(1, min(int(args.get("limit") or 200), 2000))
        hits: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in
                           (".git", "node_modules", "__pycache__", TRASH_DIRNAME)]
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fn, pattern):
                    hits.append(rel)
                    if len(hits) >= limit:
                        break
            if len(hits) >= limit:
                break
        return ToolResult(True, content="\n".join(hits) or "（无匹配）",
                          data={"root": root, "count": len(hits)})

    def _t_grep(self, args, ctx) -> ToolResult:
        import re
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return ToolResult(False, error="pattern 不能为空")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return ToolResult(False, error=f"正则错误：{e}")
        base = self.resolve(args.get("path", "."))
        include = str(args.get("include") or "*")
        limit = max(1, min(int(args.get("limit") or 100), 1000))
        files: List[str] = []
        if os.path.isfile(base):
            files = [base]
        else:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in
                               (".git", "node_modules", "__pycache__", TRASH_DIRNAME)]
                for fn in filenames:
                    if fnmatch.fnmatch(fn, include):
                        files.append(os.path.join(dirpath, fn))
        out: List[str] = []
        for fp in files:
            try:
                if os.path.getsize(fp) > 2 * 1024 * 1024:
                    continue
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if rx.search(line):
                            out.append(f"{os.path.relpath(fp, base)}:{i}: {line.rstrip()[:300]}")
                            if len(out) >= limit:
                                break
            except OSError:
                continue
            if len(out) >= limit:
                break
        return ToolResult(True, content="\n".join(out) or "（无匹配）",
                          data={"count": len(out), "scanned": len(files)})

    def _t_info(self, args, ctx) -> ToolResult:
        path = self.resolve(args.get("path"))
        if not os.path.exists(path):
            return ToolResult(False, error=f"路径不存在：{path}")
        st = os.stat(path)
        info = {"path": path, "is_dir": os.path.isdir(path),
                "size": st.st_size, "inside_workspace": self.inside(path),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))}
        return ToolResult(True, content=json.dumps(info, ensure_ascii=False, indent=2),
                          data=info)

    # ---------------- 写入 ----------------
    def _t_write(self, args, ctx) -> ToolResult:
        path = self.resolve(args.get("path"))
        content = str(args.get("content") or "")
        mode = "a" if (args.get("mode") == "append") else "w"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, mode, encoding="utf-8") as f:
            f.write(content)
        n = len(content)
        return ToolResult(True, content=f"已{'追加' if mode == 'a' else '写入'} {n} 字符到 {path}",
                          data={"path": path, "chars": n})

    def _t_edit(self, args, ctx) -> ToolResult:
        path = self.resolve(args.get("path"))
        if not os.path.isfile(path):
            return ToolResult(False, error=f"文件不存在：{path}")
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        old = str(args.get("old_text") or "")
        new = str(args.get("new_text") or "")
        if not old:
            return ToolResult(False, error="old_text 不能为空")
        cnt = text.count(old)
        if cnt == 0:
            return ToolResult(False, error="未找到待替换文本（请先用 fs_read 确认原文）")
        text = text.replace(old, new, 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return ToolResult(True, content=f"已替换 {path} 中的 1 处（共 {cnt} 处匹配）",
                          data={"path": path, "matched": cnt})

    def _t_mkdir(self, args, ctx) -> ToolResult:
        path = self.resolve(args.get("path"))
        os.makedirs(path, exist_ok=True)
        return ToolResult(True, content=f"已创建目录 {path}", data={"path": path})

    def _t_move(self, args, ctx) -> ToolResult:
        src = self.resolve(args.get("src"))
        dst = self.resolve(args.get("dst"))
        if not os.path.exists(src):
            return ToolResult(False, error=f"源路径不存在：{src}")
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
        shutil.move(src, dst)
        return ToolResult(True, content=f"已移动 {src} -> {dst}",
                          data={"src": src, "dst": dst})

    def _t_delete(self, args, ctx) -> ToolResult:
        path = self.resolve(args.get("path"))
        if not os.path.exists(path):
            return ToolResult(False, error=f"路径不存在：{path}")
        if os.path.normcase(path) in (os.path.normcase(self.workspace),
                                      os.path.normcase(os.path.expanduser("~"))):
            return ToolResult(False, error="拒绝删除工作区根目录或用户主目录")
        trash = os.path.join(self.workspace, TRASH_DIRNAME,
                             time.strftime("%Y%m%d-%H%M%S"))
        os.makedirs(trash, exist_ok=True)
        target = os.path.join(trash, os.path.basename(path.rstrip("\\/")))
        shutil.move(path, target)
        return ToolResult(True, content=f"已删除（可在 {target} 恢复）",
                          data={"trashed_to": target})

    # ---------------- 命令 ----------------
    _DENY_PATTERNS = (
        "format c:", "format /", "del /f /s /q c:\\", "rd /s /q c:\\",
        "rm -rf /", "rm -rf /*", "mkfs", "shutdown /s", ":(){", "bcdedit",
    )

    def _t_shell(self, args, ctx) -> ToolResult:
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            return ToolResult(False, error="command 不能为空")
        low = cmd.lower()
        for bad in self._DENY_PATTERNS:
            if bad in low:
                return ToolResult(False, error=f"命令被内置安全策略拒绝：命中 {bad!r}")
        cwd = self.resolve(args.get("cwd", "."))
        timeout = max(5, min(int(args.get("timeout") or 60), 600))
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return ToolResult(False, error=f"命令超时（>{timeout}s）")
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        return ToolResult(proc.returncode == 0, content=f"exit={proc.returncode}\n{out}",
                          data={"exit": proc.returncode, "cwd": cwd},
                          error="" if proc.returncode == 0 else f"退出码 {proc.returncode}")

    # ---------------- 网络 ----------------
    def _t_fetch(self, args, ctx) -> ToolResult:
        import httpx
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(False, error="仅支持 http/https URL")
        max_chars = max(500, min(int(args.get("max_chars") or 8000), 40000))
        try:
            with httpx.Client(follow_redirects=True, timeout=25.0,
                              trust_env=True,
                              headers={"User-Agent": "llmBuddy-Assistant/1.0"}) as c:
                r = c.get(url)
        except Exception as e:
            return ToolResult(False, error=f"请求失败：{e}")
        text = r.text or ""
        # 极简 HTML -> 文本（去掉 script/style/标签，压缩空行）
        import re
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return ToolResult(r.status_code < 400, content=text[:max_chars],
                          data={"status": r.status_code, "url": str(r.url)},
                          error="" if r.status_code < 400 else f"HTTP {r.status_code}")

    # ---------------- 技能 ----------------
    def _t_skill_list(self, args, ctx) -> ToolResult:
        from core.assistant import skills
        items = skills.list_skills()
        if not items:
            return ToolResult(True, content="（未安装任何技能）", data=[])
        lines = [f"- {s['name']}：{s.get('description', '')}" for s in items]
        return ToolResult(True, content="\n".join(lines), data=items)

    def _t_skill_run(self, args, ctx) -> ToolResult:
        from core.assistant import skills
        from core.assistant.orchestrator import run_skill
        name = str(args.get("name") or "").strip()
        task = str(args.get("task") or "").strip()
        skill = skills.load_skill(name)
        if skill is None:
            return ToolResult(False, error=f"技能不存在：{name}")
        sub_ctx = dict(ctx or {})
        sub_ctx["depth"] = int(sub_ctx.get("depth") or 0) + 1
        result = run_skill(skill, task, sub_ctx)
        return ToolResult(bool(result.get("ok")), content=result.get("answer", ""),
                          data=result, error=result.get("error", ""))


def build_registry(workspace: str) -> ToolRegistry:
    return ToolRegistry(workspace)
