# -*- coding: utf-8 -*-
"""长期记忆（core/assistant/memory.py）。

对齐 WorkBuddy 的三层记忆，这里实现**本地两层**（没有云，第三层不需要）：

    memory/MEMORY.md                      全局记忆：跨空间的用户偏好 / 长期约定
    memory/spaces/<slug>/MEMORY.md        空间记忆：这个工作区的项目约定 / 结论
    memory/spaces/<slug>/YYYY-MM-DD.md    工作日志：按天追加，只增不改

三个使用面：

    ① 注入   —— 每轮把「全局记忆 + 当前空间记忆 + 最近 1~2 天日志摘要」写进
                system（`build_hint`），预算见下面的 MAX_* 常量；
    ② 工具   —— `memory_read` / `memory_append`（见 tools.py），模型可以自己读
                旧结论、自己记新结论，不需要用户手工维护文件；
    ③ 界面   —— `snapshot()` 给 `GET /memory` 下发，客户端「记忆…」对话框里
                直接编辑三个文件。

设计口径（与架构文档 §记忆 一致）：

    · **纯文本、纯本地**：Markdown 文件，用户随时能用记事本打开改；
    · **失败静默**：任何读写异常都落成空内容，绝不让一轮对话失败；
    · **有预算**：注入有字符上限，超了就截断并标注「已截断」，防止记忆把
      上下文窗口吃满（这是「不影响任务执行准确性」的硬约束）；
    · **模型可写、用户可改**：升级安装包不会碰这两个目录（不属于默认技能包）。
"""
import hashlib
import os
import re
import time
from typing import Any, Dict, List, Optional

from core.assistant.store import ASSISTANT_DIR, norm_workspace

#: 记忆根目录（与 skills/ sessions/ 同级，安装包升级不会覆盖）
MEMORY_DIR = os.path.join(ASSISTANT_DIR, "memory")
#: 全局记忆文件
GLOBAL_MD = os.path.join(MEMORY_DIR, "MEMORY.md")
#: 各空间的记忆目录
SPACES_DIR = os.path.join(MEMORY_DIR, "spaces")

#: 单次写入上限（字符）：防止模型一次把整篇日志塞进来
MAX_WRITE_CHARS = 20000
#: 注入 system 的字符预算（超了截断并标注）
MAX_GLOBAL_CHARS = 2400
MAX_SPACE_CHARS = 1600
MAX_LOG_CHARS = 1200
#: 注入时最多带几天的日志（新的在前）
MAX_LOG_DAYS = 2
#: 记忆段整体上限：兜底，避免三块加起来失控
MAX_HINT_CHARS = 6000

#: 记忆文件的「小节标题」格式（append 到 MEMORY.md 时用）
_ENTRY_FMT = "- {date} {text}"

_TEMPLATE_GLOBAL = """# 全局记忆

> 跨空间生效的长期事实与偏好。由用户在「记忆…」里维护，或助理用
> `memory_append` 追加。**不要把密码、密钥、身份证号之类的敏感信息写进来。**

## 用户偏好
<!-- 例如：回答用简体中文；先给结论再给依据；喜欢表格 -->

## 长期事实
<!-- 例如：在苏州工业园区工作；项目用 Python 3.11 -->
"""

_TEMPLATE_SPACE = """# 空间记忆：{name}

> 只在本空间生效的项目约定与已确认结论。助理进入本空间时会自动读到这里。

## 项目约定
<!-- 例如：后端 Python 3.11；提交信息用中文 -->

## 已确认结论
<!-- 例如：8000 端口是网关，不要占用 -->
"""


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
def _slug(ws: str) -> str:
    """空间目录名：可读前缀 + 路径哈希（同名目录不同路径也不会串）。"""
    name = (norm_workspace(ws).rsplit("/", 1)[-1] or "space").strip() or "space"
    name = re.sub(r"[^\w\-.]+", "-", name, flags=re.UNICODE).strip("-.") or "space"
    digest = hashlib.sha1(norm_workspace(ws).encode("utf-8")).hexdigest()[:6]
    return f"{name[:32]}-{digest}"


def space_dir(ws: str) -> str:
    return os.path.join(SPACES_DIR, _slug(ws))


def space_md(ws: str) -> str:
    return os.path.join(space_dir(ws), "MEMORY.md")


def log_path(ws: str, date: str = "") -> str:
    day = _norm_date(date) or time.strftime("%Y-%m-%d")
    return os.path.join(space_dir(ws), f"{day}.md")


def ensure_dirs(ws: str = "") -> None:
    """建目录（幂等）。给了空间就一并建它的目录。"""
    for d in (MEMORY_DIR, SPACES_DIR) + ((space_dir(ws),) if ws else ()):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #
def _norm_date(date: Any) -> str:
    """只接受 YYYY-MM-DD（模型可能传「今天」「2026/10/28」之类）。"""
    raw = str(date or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", raw)
    if not m:
        return ""
    y, mo, d = (int(x) for x in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d}"


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def _write(path: str, text: str) -> bool:
    """原子写（先写 .tmp 再 replace），失败返回 False。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def read_global() -> str:
    return _read(GLOBAL_MD)


def save_global(text: str) -> bool:
    return _write(GLOBAL_MD, _clip(text, MAX_WRITE_CHARS))


def read_space(ws: str) -> str:
    if not ws:
        return ""
    return _read(space_md(ws))


def save_space(ws: str, text: str) -> bool:
    if not ws:
        return False
    return _write(space_md(ws), _clip(text, MAX_WRITE_CHARS))


def read_log(ws: str, date: str = "") -> str:
    if not ws:
        return ""
    return _read(log_path(ws, date))


def save_log(ws: str, text: str, date: str = "") -> bool:
    if not ws:
        return False
    return _write(log_path(ws, date), _clip(text, MAX_WRITE_CHARS))


def _clip(text: Any, limit: int) -> str:
    raw = str(text or "")
    return raw[:limit]


# --------------------------------------------------------------------------- #
# 追加（模型用）
# --------------------------------------------------------------------------- #
def append_log(ws: str, text: str, date: str = "") -> str:
    """把一条记录追加到当天的工作日志，返回落盘路径（失败返回空串）。

    格式固定成 `- HH:MM 内容`：多行内容缩进两格，Markdown 列表才不会断。
    """
    body = str(text or "").strip()
    if not ws or not body:
        return ""
    body = _clip(body, 2000)
    path = log_path(ws, date)
    ensure_dirs(ws)
    head = f"# 工作日志 {os.path.basename(path)[:10]}\n\n"
    cur = _read(path)
    stamp = time.strftime("%H:%M")
    lines = body.splitlines() or [""]
    entry = f"- {stamp} {lines[0]}"
    for extra in lines[1:]:
        entry += "\n  " + extra
    new = (cur if cur.strip() else head) + entry + "\n"
    if not _write(path, _clip(new, MAX_WRITE_CHARS)):
        return ""
    return path


def append_memory(ws: str, text: str) -> str:
    """把一条记录追加到 **空间** MEMORY.md（长期结论，不是日志）。"""
    body = str(text or "").strip()
    if not ws or not body:
        return ""
    path = space_md(ws)
    cur = _read(path)
    if not cur.strip():
        cur = _TEMPLATE_SPACE.format(name=norm_workspace(ws)) + "\n"
    cur = cur.rstrip() + "\n" + _ENTRY_FMT.format(
        date=time.strftime("%Y-%m-%d"), text=_clip(body, 2000)) + "\n"
    return path if _write(path, _clip(cur, MAX_WRITE_CHARS)) else ""


def append_global(text: str) -> str:
    """把一条记录追加到全局 MEMORY.md。"""
    body = str(text or "").strip()
    if not body:
        return ""
    cur = read_global()
    if not cur.strip():
        cur = _TEMPLATE_GLOBAL
    cur = cur.rstrip() + "\n" + _ENTRY_FMT.format(
        date=time.strftime("%Y-%m-%d"), text=_clip(body, 2000)) + "\n"
    return GLOBAL_MD if _write(GLOBAL_MD, _clip(cur, MAX_WRITE_CHARS)) else ""


# --------------------------------------------------------------------------- #
# 清单与快照
# --------------------------------------------------------------------------- #
def list_logs(ws: str, limit: int = 7) -> List[Dict[str, Any]]:
    """最近的工作日志（新的在前）：[{date, path, chars, preview}]。"""
    if not ws:
        return []
    d = space_dir(ws)
    try:
        names = [n for n in os.listdir(d)
                 if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", n)]
    except OSError:
        return []
    out: List[Dict[str, Any]] = []
    for name in sorted(names, reverse=True)[:max(1, limit)]:
        path = os.path.join(d, name)
        text = _read(path)
        out.append({"date": name[:10], "path": path, "chars": len(text),
                    "preview": _first_line(text)})
    return out


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-").strip()
        if line and not line.startswith("#"):
            return line[:60]
    return ""


def stats(ws: str = "") -> Dict[str, Any]:
    """体量统计（界面显示 / 状态栏用）。"""
    logs = list_logs(ws, limit=999)
    return {
        "global_chars": len(read_global()),
        "space_chars": len(read_space(ws)),
        "logs": len(logs),
        "log_chars": sum(int(x.get("chars") or 0) for x in logs),
    }


def snapshot(ws: str = "", enabled: bool = True) -> Dict[str, Any]:
    """给 `GET /memory` / `state.memory` 下发的完整快照（含文件正文）。"""
    ws = norm_workspace(ws)
    return {
        "enabled": bool(enabled),
        "workspace": ws,
        "global": {"path": GLOBAL_MD, "text": read_global()},
        "space": {"path": space_md(ws) if ws else "", "text": read_space(ws)},
        "logs": [dict(x, text=read_log(ws, x.get("date") or ""))
                 for x in list_logs(ws, limit=14)],
        "stats": stats(ws),
    }


# --------------------------------------------------------------------------- #
# 注入
# --------------------------------------------------------------------------- #
def _trim(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + "\n…（记忆超出注入预算，已截断；完整内容用 memory_read 读）"


def _body_only(text: str) -> str:
    """去掉模板脚手架（H1 标题、> 说明行、HTML 注释），只留要注入的正文。

    模板里那些「例如：…」是给**人**看的提示，写进 system 只会白烧 token。
    """
    out: List[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("# ") or s.startswith(">"):
            continue
        if s.startswith("<!--") and s.endswith("-->"):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def build_hint(ws: str = "", enabled: bool = True) -> str:
    """拼 system 尾部的记忆段。没有任何记忆时返回空串（不占提示词）。"""
    if not enabled:
        return ""
    ws = norm_workspace(ws)
    parts: List[str] = []
    g = _trim(_body_only(read_global()), MAX_GLOBAL_CHARS)
    if g:
        parts.append("### 全局记忆（跨空间）\n" + g)
    s = _trim(_body_only(read_space(ws)), MAX_SPACE_CHARS) if ws else ""
    if s:
        name = ws.rsplit("/", 1)[-1] or ws
        parts.append(f"### 空间记忆（{name}）\n" + s)
    logs: List[str] = []
    for item in list_logs(ws, limit=MAX_LOG_DAYS) if ws else []:
        body = _trim(read_log(ws, item.get("date") or ""), MAX_LOG_CHARS // 2)
        if body:
            body = "\n".join(line for line in body.splitlines()
                             if not line.startswith("# "))
            logs.append(f"#### {item.get('date')}\n{body.strip()}")
    if logs:
        parts.append("### 最近工作日志\n" + "\n".join(logs))
    if not parts:
        return ""
    body = "\n\n".join(parts)[:MAX_HINT_CHARS]
    return (
        "\n## 记忆（本机长期记忆，跨轮次生效）\n"
        + body
        + "\n\n记忆只是**过往结论**，可能与当前事实不符：涉及文件、端口、"
          "版本这类会变的东西，先用工具核实再下结论。\n"
          "本次对话若产生了值得长期保留的偏好、约定或结论，用 `memory_append` "
          "记一条（默认写进本空间今天的日志；`to=memory` 写进空间记忆）。"
          "**不要记录密码、密钥等敏感信息，也不要记录一次性的临时内容。**\n"
    )


# --------------------------------------------------------------------------- #
# 首次初始化（幂等；不覆盖用户已改过的内容）
# --------------------------------------------------------------------------- #
def ensure_files(ws: str = "") -> None:
    ensure_dirs(ws)
    if not os.path.exists(GLOBAL_MD):
        _write(GLOBAL_MD, _TEMPLATE_GLOBAL)
    if ws and not os.path.exists(space_md(ws)):
        _write(space_md(ws), _TEMPLATE_SPACE.format(
            name=norm_workspace(ws).rsplit("/", 1)[-1] or ws))


def log_summary_line(ws: str = "") -> str:
    """状态栏一句话：记忆都为空时返回空串。"""
    st = stats(ws)
    if not (st["global_chars"] or st["space_chars"] or st["logs"]):
        return ""
    return (f"记忆：全局 {st['global_chars']} 字 · 空间 {st['space_chars']} 字 · "
            f"{st['logs']} 天日志")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def read_any(ws: str, scope: str = "space", date: str = "") -> Dict[str, Any]:
    """`memory_read` 工具的实现：按 scope 返回内容与路径。"""
    scope = (scope or "space").strip().lower()
    ws = norm_workspace(ws)
    if scope in ("global", "user", "全局"):
        return {"scope": "global", "path": GLOBAL_MD, "text": read_global()}
    if scope in ("log", "logs", "日志"):
        day = _norm_date(date)
        if not day:
            return {"scope": "log", "path": space_dir(ws) if ws else "",
                    "text": "", "logs": list_logs(ws, limit=14),
                    "note": "未指定日期；下面是可读的日志清单，"
                            "再用 date=YYYY-MM-DD 读取具体某天"}
        return {"scope": "log", "date": day, "path": log_path(ws, day),
                "text": read_log(ws, day)}
    return {"scope": "space", "path": space_md(ws) if ws else "",
            "text": read_space(ws)}


def write_any(ws: str, text: str, scope: str = "space", to: str = "log",
              date: str = "") -> Dict[str, Any]:
    """`memory_append` 工具的实现：写日志 / 空间记忆 / 全局记忆。"""
    scope = (scope or "space").strip().lower()
    target = (to or "log").strip().lower()
    ws = norm_workspace(ws)
    if scope in ("global", "user", "全局"):
        path = append_global(text)
    elif target in ("memory", "md", "记忆"):
        path = append_memory(ws, text)
    else:
        path = append_log(ws, text, date=date)
    return {"path": path, "ok": bool(path), "scope": scope, "to": target}


def enabled_from(cfg: Optional[Dict[str, Any]]) -> bool:
    """配置里 `memory_enabled` 缺省为 True（老 config 也能用上记忆）。"""
    cfg = cfg or {}
    val = cfg.get("memory_enabled", True)
    return bool(val)
