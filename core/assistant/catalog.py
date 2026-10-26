# -*- coding: utf-8 -*-
"""
在线能力目录（core/assistant/catalog.py）。

职责边界（架构 §5 A1 裁决）：**只负责「发现 + 缓存 + 下载字节」**，绝不落盘到
`SKILLS_DIR`、绝不写 `config.json.mcp_servers`。安装动作分别归：

    - 技能落盘（原子 / 回滚 / 备份）  -> `skills.install_from_*`
    - MCP 配置持久化 + 连接           -> `mcp.MCPManager.add_server / connect`

三个数据源：
    MCP Registry   https://registry.modelcontextprotocol.io/v0/servers
    GitHub 仓库    https://api.github.com/search/repositories?q=topic:claude-skills
    GitHub tree    https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}
    技能文件       https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}

三份缓存（`<ASSISTANT_DIR>/catalog/`，均带 `fetched_at`，TTL 读 config）：

    mcp_servers.json   {fetched_at, cursor, items[]}
    gh_repos.json      {fetched_at, total_count, items[]}
    gh_skills.json     {fetched_at, repos:{"owner/repo@ref": {commit, items[]}}}

两个硬约束：

**代理（架构 §1.1 D4）**：本机 `registry.modelcontextprotocol.io` 直连返回 000，
必须走代理。所有出网客户端一律 `httpx.Client(trust_env=False, proxy=...)`；
代理值取 `config.json.catalog.proxy`，为空时依次回落到 `HTTPS_PROXY` /
`HTTP_PROXY` / `http://127.0.0.1:7897`，并**实际发一次请求验证**——探测失败就
抛 `CatalogError(2020)` 并带明确文案，绝不静默直连。

**离线退化（R-S1）**：缓存过期但拉取失败 -> 返回**过期缓存**并标 `stale=True`，
绝不返回空页。
"""
import concurrent.futures
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.assistant.store import CATALOG_DIR, ensure_dirs

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------
PROXY_DEFAULT = "http://127.0.0.1:7897"
TTL_MCP = 24 * 3600
TTL_GH_REPOS = 24 * 3600
TTL_GH_SKILLS = 7 * 24 * 3600

REGISTRY_BASE = "https://registry.modelcontextprotocol.io/v0/servers"
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
SKILL_TOPIC = "claude-skills"

CLIENT_TIMEOUT = 5.0
PROBE_TIMEOUT = 3.0
REFRESH_TOTAL_TIMEOUT = 12.0
#: refresh_all 预热 gh_skills 的仓库数上限（GitHub 配额不可控）
WARM_REPOS = 3

#: MCP 启动命令白名单（安全红线，任务分解 §4.7）。
#: 与 `mcp.COMMAND_WHITELIST` 同源——单一事实来源，避免在线安装与手动添加
#: 各维护一套规则而漂移（mcp.py 不再反向 import catalog，无循环依赖）。
from core.assistant.mcp import COMMAND_WHITELIST  # noqa: E402  (常量区导入)

#: 技能包只收文本类附件（脚本永不执行，但 .py 允许落盘供人工审阅）
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".toml", ".cfg")
MAX_SKILL_MD_BYTES = 200 * 1024          # SKILL.md 超过 200KB 拒收（R-S3）
DEFAULT_MAX_FILES = 20
DEFAULT_MAX_BYTES = 2 * 1024 * 1024

#: 大技能 / 二进制整包下载（tarball 子树）上限——远大于文本管线的 2MB / 20 文件。
#: 设计目标：PPT Master 这类 1.3 万文件 / 63MB 含 SVG+WAV 的技能能一整包装下，
#: 而非被文本管线静默丢 96% 二进制（R-S3 旧约束的已知缺陷，本次修复）。
ARCHIVE_MAX_FILES = 20000
ARCHIVE_MAX_BYTES = 256 * 1024 * 1024    # 抽取后落盘预算 256MB（防滥用，默认）
ARCHIVE_MAX_DOWNLOAD = 512 * 1024 * 1024  # 下载阶段硬上限（按 content-length 预检）
ARCHIVE_TIMEOUT = 120.0                  # tarball 下载超时（大体积需宽松）
CODELOAD_BASE = "https://codeload.github.com"

#: 错误码（架构 §8）
ERR_UNREACHABLE = 2020
ERR_UPSTREAM = 2021
ERR_BAD_BUNDLE = 2022
ERR_EXISTS = 2023
ERR_ENV_MISSING = 2024
ERR_CONNECT_FAILED = 2025
ERR_BAD_URL = 2026

CACHE_MCP = "mcp_servers.json"
CACHE_REPOS = "gh_repos.json"
CACHE_SKILLS = "gh_skills.json"


class CatalogError(RuntimeError):
    """目录层业务错误：带 code（2020-2026），由路由层转成 fail(code, message)。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)
        self.data = data

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "data": self.data}


# ----------------------------------------------------------------------------
# 基础设施：目录 / 代理 / 客户端 / 缓存
# ----------------------------------------------------------------------------
def catalog_dir() -> str:
    """缓存目录（绝对路径，内建 ensure_dirs）。

    每次**动态**读 `store.CATALOG_DIR`：测试会把 store 的目录常量 monkeypatch
    到 tmp_path，模块级 import 的快照会读到用户真实目录（污染本机数据）。
    """
    from core.assistant import store as store_mod
    ensure_dirs()
    path = str(store_mod.CATALOG_DIR)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _cfg_get(cfg: Optional[Dict[str, Any]], *keys: str,
             default: Any = None) -> Any:
    """安全读嵌套配置项（cfg 可能来自 load_config()，也可能是裸 dict）。"""
    cur: Any = cfg or {}
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


def _ttl_seconds(cfg: Optional[Dict[str, Any]], kind: str) -> int:
    """TTL 秒数：读 `catalog.ttl_hours.<kind>`，非法值回落到默认。"""
    base = {"mcp": TTL_MCP, "gh_repos": TTL_GH_REPOS,
            "gh_skills": TTL_GH_SKILLS}.get(kind, TTL_MCP)
    hours = _cfg_get(cfg, "catalog", "ttl_hours", kind, default=None)
    if hours is None:
        return int(base)
    try:
        val = float(hours)
    except (TypeError, ValueError):
        return int(base)
    if val <= 0:
        return int(base)
    return int(val * 3600)


def _env_proxy() -> str:
    """环境变量里的代理（HTTPS 优先，其次 HTTP）。"""
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = str(os.environ.get(key) or "").strip()
        if val:
            return val
    return ""


def _proxy_candidates(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    """代理尝试顺序：显式配置 > 环境变量 > 本机默认端口，去重保序。

    为什么要有「候选链」：很多机器上 `HTTPS_PROXY` 是别的工具（Clash / 加速器）
    写进去的**过期端口**（如 64422），进程一启动就带着它；只认第一个候选时，
    「明明有可用代理」也会一路报「无法连接」。这里按序降级，只在**传输层失败**
    （连不上 / 超时）时换下一个，业务错误（403/404）不换，避免把限流掩盖掉。
    """
    out: List[str] = []
    for val in (str(_cfg_get(cfg, "catalog", "proxy", default="") or "").strip(),
                _env_proxy(), PROXY_DEFAULT):
        if val and val not in out:
            out.append(val)
    return out or [PROXY_DEFAULT]


def _proxy(cfg: Optional[Dict[str, Any]] = None) -> str:
    """当前首选的代理地址（日志 / 报错文案用；实际请求走候选链）。

    **绝不做「直连」回落**：本机 registry 直连是 000，静默直连只会把
    "无网络" 的锅甩给用户。
    """
    return _proxy_candidates(cfg)[0]


def _client(cfg: Optional[Dict[str, Any]] = None,
            timeout: float = CLIENT_TIMEOUT,
            proxy: Optional[str] = None) -> httpx.Client:
    """唯一出网点：trust_env=False + 显式 proxy（架构 §1.1 D4）。

    `trust_env=False` 是硬约束：环境变量里残留的坏代理会让 httpx 在任何请求前
    就抛异常，且报错信息与真实原因无关；统一由 `_proxy()` / 候选链决定代理。
    """
    headers = {"User-Agent": "llmBuddy-assistant/1.0.9"}
    token = _github_token(cfg)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(verify=False, timeout=timeout, trust_env=False,
                        follow_redirects=True,
                        proxy=(proxy or _proxy(cfg)), headers=headers)


def _github_token(cfg: Optional[Dict[str, Any]] = None) -> str:
    """GitHub Token（可选）：`config.catalog.github_token`。

    未认证的 GitHub API 配额只有 **60 次/小时**，而「列仓库 → 枚举 SKILL.md →
    取正文」三步就要打十几次请求，正常浏览几个仓库就会 403。配了 token（任意
    classic PAT，无需任何 scope 勾选，只读公共仓库）配额升到 5000 次/小时。
    """
    return str(_cfg_get(cfg, "catalog", "github_token", default="") or "").strip()


def _probe(cfg: Optional[Dict[str, Any]] = None) -> None:
    """实际打一次请求验证代理可用；不可用抛 CatalogError(2020)。

    必须真发请求：`http://127.0.0.1:7897` 是常见默认值，用户机器上不一定有
    这个端口。只判断"配置非空"会把 000 错误延后到列表请求，报错位置离用户
    操作更远（架构 §4.3 / R-E4）。
    """
    proxy = _proxy(cfg)
    if not proxy:
        raise CatalogError(ERR_UNREACHABLE,
                           "未配置代理，无法访问在线目录（国内网络通常需要代理）")
    try:
        with _client(cfg, timeout=PROBE_TIMEOUT) as c:
            # 用 MCP Registry 本身做探针：代理通不通、上游通不通一次说清
            r = c.get(REGISTRY_BASE, params={"limit": "1"})
    except Exception as e:                       # noqa: BLE001
        raise CatalogError(
            ERR_UNREACHABLE,
            f"无法连接在线目录（代理：{proxy}）：{type(e).__name__}: {e}") from e
    if r.status_code >= 500:
        raise CatalogError(
            ERR_UPSTREAM,
            f"在线目录服务端异常（HTTP {r.status_code}，代理：{proxy}）")


def _cache_path(name: str) -> str:
    return os.path.join(catalog_dir(), name)


def _cache_read(name: str) -> Dict[str, Any]:
    """读缓存；文件不存在 / JSON 损坏 / 结构异常一律返回 `{}`（绝不抛）。"""
    path = _cache_path(name)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _cache_write(name: str, payload: Dict[str, Any]) -> None:
    """原子写缓存（.tmp -> os.replace）；失败静默（缓存坏了不影响主流程）。"""
    path = _cache_path(name)
    body = dict(payload or {})
    body.setdefault("fetched_at", time.time())
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


def _fresh(cache: Dict[str, Any], ttl: int) -> bool:
    """缓存是否在 TTL 内（`fetched_at` 缺失或为未来时间都按过期处理）。"""
    try:
        ts = float((cache or {}).get("fetched_at") or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    age = time.time() - ts
    return 0 <= age < max(0, int(ttl))


def _stale_payload(cache: Dict[str, Any], error: str, **extra: Any) -> Dict[str, Any]:
    """过期缓存的退化返回体：items 照旧、`stale=True`、带 error 文案。"""
    out: Dict[str, Any] = {
        "fetched_at": cache.get("fetched_at") or 0,
        "stale": True,
        "error": error,
    }
    out.update(extra)
    return out


# ----------------------------------------------------------------------------
# 上游请求小工具
# ----------------------------------------------------------------------------
#: 连接类/超时/TLS 抖动的最大重试次数（不含首次）。仅对传输层异常重试，
#: 4xx/5xx/解析失败不重试（那是确定性的上游语义，重试无益且拖时间）。
RETRY_MAX = 2
#: 退避间隔（秒）：首败 0.4s、二败 1.0s。总耗时上限见 RETRY_BUDGET_NOTE。
RETRY_BACKOFF = (0.4, 1.0)


def _is_retryable_transport(exc: BaseException) -> bool:
    """是否为「连接/超时/TLS 抖动」这类可重试的传输层异常。

    httpx 把网络层错误都归到 `httpx.TransportError`（含 ConnectError /
    ConnectTimeout / ReadTimeout / RemoteProtocolError / SSL 握手失败）。
    明确排除 4xx/5xx（`HTTPStatusError` 不在此路径——我们用的是 `c.get`
    非 `raise_for_status`，状态码在调用方判断）。
    """
    return isinstance(exc, httpx.TransportError)


def _do_get(cfg: Optional[Dict[str, Any]], url: str,
            params: Optional[Dict[str, Any]],
            timeout: float) -> httpx.Response:
    """发一次 GET（含传输层重试 + 代理候选降级）。

    重试策略（P2-4）：最多 `RETRY_MAX` 次，仅 `httpx.TransportError` 重试，
    退避 `RETRY_BACKOFF`。为避免把 `force=1` 的同步路径拖死：每次尝试沿用
    调用方 `timeout`，总最坏耗时 ≈ (1+RETRY_MAX)*timeout，故对 5s 预算
    实际最坏 3×5s——因此只在**单次 timeout ≤ 5s** 时启用满额重试；大超时
    （如 list_repo_skills 用的 2×5s）降为最多 1 次重试，防止用户等太久。

    代理降级：环境变量里的 `HTTPS_PROXY` 常是别的工具留下的过期端口
    （本机就见过 64422），连不上时按 `_proxy_candidates()` 顺序换下一个，
    全部失败才抛最后一次的异常。业务错误（403 限流 / 404）不触发换代理。
    """
    attempts = 1 + RETRY_MAX
    if timeout > CLIENT_TIMEOUT:          # 大超时请求少重试，别拖死同步路径
        attempts = 1 + 1
    candidates = _proxy_candidates(cfg)
    last: Optional[BaseException] = None
    for p_idx, proxy in enumerate(candidates):
        last_cand = p_idx == len(candidates) - 1
        # 非最后一个候选只给「短预算 + 一次尝试」：环境里那个过期代理若在
        # 半死状态（接受连接但不回），按 ARCHIVE_TIMEOUT=120s × 2 次去等，
        # 用户要先干等 4 分钟才轮到真能用的代理（实测过）。10s 足够判定
        # 「这个代理连不上」，真正的下载只在最后一个候选上用满预算。
        budget = timeout if last_cand else min(timeout, CLIENT_TIMEOUT * 2)
        tries = attempts if last_cand else 1
        for i in range(tries):
            try:
                with _client(cfg, timeout=budget, proxy=proxy) as c:
                    return c.get(url, params=params or None)
            except Exception as e:                    # noqa: BLE001
                last = e
                transport = _is_retryable_transport(e)
                if not transport:
                    raise
                if i >= tries - 1:
                    if not last_cand:
                        break                     # 换下一个代理
                    raise
                delay = RETRY_BACKOFF[min(i, len(RETRY_BACKOFF) - 1)]
                time.sleep(delay)
    assert last is not None
    raise last


def _get_json(cfg: Optional[Dict[str, Any]], url: str,
              params: Optional[Dict[str, Any]] = None,
              timeout: float = CLIENT_TIMEOUT) -> Tuple[Any, Dict[str, str]]:
    """发一次 GET 并解析 JSON。返回 (data, headers)。

    失败语义：
        连接异常 / 超时   -> CatalogError(2020)（传输层抖动已内部重试）
        非 200            -> CatalogError(2021)（含 GitHub 403 限流）
    """
    proxy = "、".join(_proxy_candidates(cfg))
    try:
        resp = _do_get(cfg, url, params, timeout)
    except Exception as e:                       # noqa: BLE001
        raise CatalogError(
            ERR_UNREACHABLE,
            f"无法连接在线目录（已尝试代理：{proxy}）：{type(e).__name__}: {e}"
        ) from e
    if resp.status_code != 200:
        hint = ""
        if resp.status_code == 403 and "github" in url:
            left = resp.headers.get("x-ratelimit-reset") or ""
            wait = ""
            try:
                wait = str(max(0, int(left) - int(time.time())))
            except (TypeError, ValueError):
                wait = ""
            hint = (f"GitHub 接口限流，请 {wait or '若干'} 秒后重试，"
                    "或先使用已缓存的结果")
        raise CatalogError(ERR_UPSTREAM,
                           hint or f"上游返回 HTTP {resp.status_code}（{url}）",
                           {"status": resp.status_code, "url": url})
    try:
        return resp.json(), dict(resp.headers)
    except Exception as e:                       # noqa: BLE001
        raise CatalogError(ERR_UPSTREAM,
                           f"上游返回的不是合法 JSON（{url}）：{e}") from e


def _get_text(cfg: Optional[Dict[str, Any]], url: str,
              timeout: float = CLIENT_TIMEOUT) -> str:
    """取纯文本（raw.githubusercontent.com）。2020/2021 语义同上（含重试）。"""
    proxy = "、".join(_proxy_candidates(cfg))
    try:
        resp = _do_get(cfg, url, None, timeout)
    except Exception as e:                       # noqa: BLE001
        raise CatalogError(
            ERR_UNREACHABLE,
            f"无法连接 {url}（已尝试代理：{proxy}）：{type(e).__name__}: {e}"
        ) from e
    if resp.status_code != 200:
        raise CatalogError(ERR_UPSTREAM,
                           f"下载失败 HTTP {resp.status_code}：{url}",
                           {"status": resp.status_code, "url": url})
    return resp.text or ""


# ----------------------------------------------------------------------------
# MCP Registry
# ----------------------------------------------------------------------------
def free_class_of(raw: Dict[str, Any]) -> str:
    """免费/易用分级：zero_config | need_key | need_register | unknown。

    zero_config  = 无必填项，且有可直接启动的包（stdio）或可直接连的远程
                   （streamable-http 且无必填 header）  -> 一键装
    need_key     = 有 isRequired 的 env（stdio 包）      -> 需用户填密钥
    need_register= 远程传输且 header 有必填项（先注册/授权拿 token）
    unknown      = 既无 packages 也无 remotes           -> 不给安装按钮

    规则对齐 PRD §5.3：`remotes[0].type == "streamable-http"` 且 headers 无
    必填项 → `zero_config`（真正免安装、免密钥，点一下就能连）。只有 header
    存在必填项才判 `need_register`——否则这类一键可连的服务器会被默认隐藏
    （客户端 CATALOG_HIDDEN_CLASSES 含 need_register），用户「抓全部免费 MCP」
    就只能拿到半张清单。
    """
    raw = raw or {}
    packages = [p for p in (raw.get("packages") or []) if isinstance(p, dict)]
    remotes = [r for r in (raw.get("remotes") or []) if isinstance(r, dict)]
    pkg_required = False
    for p in packages:
        for ev in (p.get("environmentVariables") or []):
            if isinstance(ev, dict) and (ev.get("isRequired")
                                         or ev.get("required")):
                pkg_required = True
                break
        if pkg_required:
            break
    remote_required = False
    if not pkg_required:
        for r in remotes:
            for hd in (r.get("headers") or []):
                if isinstance(hd, dict) and (hd.get("isRequired")
                                             or hd.get("required")):
                    remote_required = True
                    break
            if remote_required:
                break
    if pkg_required:
        return "need_key"
    if remote_required:
        # 远程 header 有必填项：需先注册/授权拿 token（PRD §5.3）
        return "need_register"
    if packages:
        return "zero_config"
    if remotes:
        # 无必填 header 的 streamable-http / sse 远程服务 = 零配置，一键可连
        return "zero_config"
    return "unknown"


def _pick_package(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 packages[] 里挑一个可用的（优先 npm/pypi，其次带 runtimeHint）。"""
    best: Optional[Dict[str, Any]] = None
    for p in (raw.get("packages") or []):
        if not isinstance(p, dict):
            continue
        rtype = str(p.get("registryType") or "").lower()
        if rtype in ("npm", "pypi"):
            if best is None or not str(best.get("registryType") or ""):
                best = p
    if best is not None:
        return best
    for p in (raw.get("packages") or []):
        if isinstance(p, dict) and p.get("identifier"):
            return p
    return None


def _pick_remote(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """从 remotes[] 里挑一个 streamable-http（再次 sse）。"""
    remotes = [r for r in (raw.get("remotes") or []) if isinstance(r, dict)]
    for want in ("streamable-http", "http", "sse"):
        for r in remotes:
            if str(r.get("type") or "").lower() == want:
                return r
    return remotes[0] if remotes else None


def _runtime_command(pkg: Dict[str, Any]) -> str:
    """把 registryType / runtimeHint 映射成白名单内的启动命令。"""
    hint = str(pkg.get("runtimeHint") or "").strip().lower()
    rtype = str(pkg.get("registryType") or "").strip().lower()
    if hint in COMMAND_WHITELIST:
        return hint
    if hint in ("npm", "npx"):
        return "npx"
    if hint in ("pypi", "uv", "uvx"):
        return "uvx"
    if rtype == "npm":
        return "npx"
    if rtype == "pypi":
        return "uvx"
    if rtype == "oci":
        return "docker"
    return "npx"


def _arg_values(pkg: Dict[str, Any]) -> List[str]:
    """runtimeArguments -> 字面量参数列表（variable 占位交调用方填）。"""
    out: List[str] = []
    for a in (pkg.get("runtimeArguments") or []):
        if not isinstance(a, dict):
            continue
        val = a.get("value")
        if val is None:
            continue
        out.append(str(val))
    return out


def _required_env(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """汇总必填环境变量（packages[].environmentVariables 里 isRequired 的）。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in (raw.get("packages") or []):
        if not isinstance(p, dict):
            continue
        for ev in (p.get("environmentVariables") or []):
            if not isinstance(ev, dict):
                continue
            if not (ev.get("isRequired") or ev.get("required")):
                continue
            name = str(ev.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name,
                        "description": str(ev.get("description") or ""),
                        "default": ev.get("default") or "",
                        "format": str(ev.get("format") or "")})
    return out


def _optional_env(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """非必填环境变量（前端展示"可选"用，安装时可不填）。"""
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in (raw.get("packages") or []):
        if not isinstance(p, dict):
            continue
        for ev in (p.get("environmentVariables") or []):
            if not isinstance(ev, dict):
                continue
            if ev.get("isRequired") or ev.get("required"):
                continue
            name = str(ev.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append({"name": name,
                        "description": str(ev.get("description") or "")})
    return out


def normalize_mcp_server(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把 Registry 原始条目归一化成前端可渲染 + 可安装的结构。

    `suggest` 是**直接可用的 mcp_servers 配置片段**（不含 id/name，由
    `build_mcp_config` 补齐），前端确认框也用它展示 command/args/url。
    """
    raw = raw or {}
    names = raw.get("name") or raw.get("registry_name") or ""
    registry_name = str(names).strip()
    if not registry_name:
        server = raw.get("server") if isinstance(raw.get("server"), dict) else {}
        registry_name = str(server.get("name") or "").strip()
        if server:
            raw = {**server, **raw}
    title = str(raw.get("title") or raw.get("description") or registry_name)
    description = str(raw.get("description") or "")
    version = str(raw.get("version") or "")

    suggest: Dict[str, Any] = {"env": {},
                               "required_env": _required_env(raw),
                               "optional_env": _optional_env(raw)}
    transport = "unknown"
    pkg = _pick_package(raw)
    remote = _pick_remote(raw)
    if remote is not None:
        url = str(remote.get("url") or "").strip()
        if url:
            transport = "http"
            suggest.update({"transport": "http", "url": url, "command": "",
                            "args": []})
    if transport == "unknown" and pkg is not None:
        ident = str(pkg.get("identifier") or "").strip()
        if ident:
            command = _runtime_command(pkg)
            args = _arg_values(pkg)
            pkg_version = str(pkg.get("version") or version or "").strip()
            spec = f"{ident}@{pkg_version}" if pkg_version else ident
            # uvx 的包名不需要 -y，npx 也不强制加；保持上游 runtimeArguments 原序
            transport = "stdio"
            suggest.update({"transport": "stdio", "command": command,
                            "args": args + [spec], "url": ""})
    suggest.setdefault("transport", "stdio")
    suggest.setdefault("command", "")
    suggest.setdefault("args", [])
    suggest.setdefault("url", "")

    return {
        "registry_name": registry_name,
        "title": title,
        "description": description,
        "version": version,
        "transport": transport,
        "free_class": free_class_of(raw),
        "suggest": suggest,
        "raw": raw,
    }


def _normalize_registry_list(payload: Any) -> Dict[str, Any]:
    """把 Registry 的 `{servers:[...], metadata:{nextCursor}}` 归一化。"""
    body = payload if isinstance(payload, dict) else {}
    servers = body.get("servers") or body.get("items") or []
    meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    items: List[Dict[str, Any]] = []
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        raw = entry
        # Registry v0 的形态：{"server": {...}, "_meta": {...}}
        if isinstance(entry.get("server"), dict):
            raw = {**entry["server"], "_meta": entry.get("_meta") or {}}
        item = normalize_mcp_server(raw)
        if item.get("registry_name"):
            items.append(item)
    cursor = meta.get("nextCursor")
    if cursor is None:
        cursor = body.get("nextCursor")
    return {"items": items, "cursor": cursor}


def list_mcp(cfg: Optional[Dict[str, Any]] = None, q: str = "", cursor: str = "",
             force: bool = False, limit: int = 50) -> Dict[str, Any]:
    """MCP 目录列表。

    `force=False` 且缓存新鲜 -> 直接返缓存（首屏 ≤ 2s）；
    拉取失败但有过期缓存 -> 返过期缓存 + `stale=True`（绝不返回空页）；
    都没有 -> 抛 CatalogError(2020/2021)，由路由层转 fail()。
    """
    cache = _cache_read(CACHE_MCP)
    ttl = _ttl_seconds(cfg, "mcp")
    top_page = not str(q or "").strip() and not str(cursor or "").strip()
    if not force and cache and _fresh(cache, ttl) and top_page:
        return {"items": cache.get("items") or [],
                "cursor": cache.get("cursor"),
                "fetched_at": cache.get("fetched_at") or 0,
                "stale": False, "error": ""}
    params = {"limit": str(max(1, min(int(limit or 50), 100)))}
    if str(q or "").strip():
        params["search"] = str(q).strip()
    if str(cursor or "").strip():
        params["cursor"] = str(cursor).strip()
    try:
        payload, _headers = _get_json(cfg, REGISTRY_BASE, params=params)
        norm = _normalize_registry_list(payload)
    except CatalogError as e:
        if cache:
            return _stale_payload(cache, e.message,
                                  items=cache.get("items") or [],
                                  cursor=cache.get("cursor"))
        raise
    out = {"items": norm["items"], "cursor": norm["cursor"],
           "fetched_at": time.time(), "stale": False, "error": ""}
    # 只在首页（无 q / 无 cursor）覆盖缓存：翻页结果不该覆盖首屏缓存
    if top_page:
        _cache_write(CACHE_MCP, {"fetched_at": out["fetched_at"],
                                 "cursor": norm["cursor"],
                                 "items": norm["items"]})
    return out


def _slugify_server(name: str) -> str:
    """MCP server id 归一化（架构 §5 A5：复用 skills._slugify 的规则）。"""
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(name or "").strip()).strip("-.")
    s = s.lower()[:64]
    return s or f"mcp-{int(time.time())}"


def _existing_server_ids() -> List[str]:
    """当前已配置的 MCP server id（懒 import，避免 catalog -> mcp 的硬依赖）。"""
    try:
        from core.assistant.mcp import get_mcp_manager
        return [str(s.get("id") or "") for s in get_mcp_manager().servers()]
    except Exception:
        return []


def _unique_server_id(base: str, existing: Optional[List[str]] = None) -> str:
    """id 冲突时追加 -2 / -3（架构 §5 A5）。

    `base` 若是完整的 registry_name（含 `/`），先 `_slugify` 成合法 id；
    已是合法 id 的（不含 `/`、非空）原样保留——否则
    `com.pulsemcp/remote-filesystem` 会被二次归一化成 `remote-filesystem`。
    """
    text = str(base or "").strip()
    cand = text if text and "/" not in text else (_slugify_server(text) or "mcp")
    taken = {str(x) for x in (existing if existing is not None
                              else _existing_server_ids())}
    if cand not in taken:
        return cand
    for i in range(2, 100):
        nxt = f"{cand}-{i}"
        if nxt not in taken:
            return nxt
    return f"{cand}-{int(time.time())}"


def build_mcp_config(item: Dict[str, Any], server_id: str = "", name: str = "",
                     env: Optional[Dict[str, Any]] = None,
                     risk: str = "high",
                     existing_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """`normalize_mcp_server` 的结果 -> 可直接 `MCPManager.add_server()` 的配置。

    Raises:
        CatalogError(2022): 条目既无 remote 也无可用 package（无法映射）
        CatalogError(2024): 必填环境变量缺失
    """
    item = item or {}
    suggest = item.get("suggest") if isinstance(item.get("suggest"), dict) else {}
    if not suggest:
        suggest = normalize_mcp_server(item.get("raw") or item).get("suggest") or {}
    transport = str(suggest.get("transport") or "stdio").lower()
    url = str(suggest.get("url") or "").strip()
    command = str(suggest.get("command") or "").strip()
    args = [str(a) for a in (suggest.get("args") or [])]
    if transport == "http" and not url:
        raise CatalogError(ERR_BAD_BUNDLE,
                           "该 MCP 条目缺少远程地址（url），无法安装",
                           {"registry_name": item.get("registry_name")})
    if transport != "http":
        if command.lower() not in COMMAND_WHITELIST:
            raise CatalogError(ERR_BAD_BUNDLE,
                               f"不支持的启动命令：{command or '（空）'}，"
                               f"只允许 {'/'.join(COMMAND_WHITELIST)}",
                               {"registry_name": item.get("registry_name")})
    required = [str(e.get("name") or "") for e in (suggest.get("required_env")
                                                   or []) if e.get("name")]
    provided = {str(k): str(v) for k, v in (env or {}).items() if v not in (None, "")}
    missing = [r for r in required if not provided.get(r)]
    if missing:
        raise CatalogError(ERR_ENV_MISSING,
                           "缺少必填环境变量：" + "、".join(missing),
                           {"missing": missing, "required_env": suggest.get("required_env")})

    # id 生成优先级（架构 §5 A5）：显式 server_id > registry_name 归一化 >
    # name > title。registry_name 排在 name/title 之前是刻意的：
    # `com.pulsemcp/remote-filesystem` 是全局唯一标识，而 title「Remote
    # Filesystem」几乎每个仓库都重名；冲突一律由 `_unique_server_id` 追加
    # -2 / -3 解决。
    explicit = str(server_id or "").strip()
    if explicit:
        sid = explicit
    else:
        base = (str(item.get("registry_name") or "").strip()
                or str(name or "").strip()
                or str(item.get("title") or "").strip()
                or "mcp")
        sid = _unique_server_id(base, existing_ids)
    config: Dict[str, Any] = {
        "id": sid,
        "name": str(name or item.get("title") or sid),
        "transport": transport,
        "enabled": True,
        "risk": str(risk or "high"),
        "registry_name": str(item.get("registry_name") or ""),
        "version": str(item.get("version") or ""),
        "env": provided,
    }
    if transport == "http":
        config["url"] = url
        config["command"] = ""
        config["args"] = []
    else:
        config["command"] = command
        config["args"] = args
        config["url"] = ""
    return config


# ----------------------------------------------------------------------------
# GitHub：技能仓库
# ----------------------------------------------------------------------------
def _normalize_repo(raw: Dict[str, Any]) -> Dict[str, Any]:
    raw = raw or {}
    return {
        "full_name": str(raw.get("full_name") or ""),
        "owner": str((raw.get("owner") or {}).get("login") or "") or
                 str(raw.get("full_name") or "").split("/")[0],
        "repo": str(raw.get("name") or "") or
                str(raw.get("full_name") or "/").split("/")[-1],
        "stars": int(raw.get("stargazers_count") or 0),
        "default_branch": str(raw.get("default_branch") or "main"),
        "description": str(raw.get("description") or ""),
        "topics": [str(t) for t in (raw.get("topics") or [])],
        "updated_at": str(raw.get("pushed_at") or raw.get("updated_at") or ""),
        "html_url": str(raw.get("html_url") or ""),
        "license": str(((raw.get("license") or {}) or {}).get("spdx_id") or ""),
    }


def list_repos(cfg: Optional[Dict[str, Any]] = None, q: str = "",
               force: bool = False, per_page: int = 60) -> Dict[str, Any]:
    """按 topic:claude-skills 搜 GitHub 技能仓库（star 降序）。"""
    cache = _cache_read(CACHE_REPOS)
    ttl = _ttl_seconds(cfg, "gh_repos")
    if not force and cache and _fresh(cache, ttl) and not str(q or "").strip():
        return {"items": cache.get("items") or [],
                "total_count": cache.get("total_count") or 0,
                "fetched_at": cache.get("fetched_at") or 0,
                "stale": False, "error": ""}
    query = str(q or "").strip()
    if query:
        term = f"{query} topic:{SKILL_TOPIC}"
    else:
        term = f"topic:{SKILL_TOPIC}"
    params = {"q": term, "sort": "stars", "order": "desc",
              "per_page": str(max(1, min(int(per_page or 60), 100)))}
    try:
        payload, _headers = _get_json(cfg, GITHUB_API + "/search/repositories",
                                      params=params)
    except CatalogError as e:
        if cache:
            return _stale_payload(cache, e.message,
                                  items=cache.get("items") or [],
                                  total_count=cache.get("total_count") or 0)
        raise
    body = payload if isinstance(payload, dict) else {}
    items = [_normalize_repo(r) for r in (body.get("items") or [])
             if isinstance(r, dict)]
    items = [i for i in items if i["full_name"]]
    out = {"items": items, "total_count": int(body.get("total_count") or len(items)),
           "fetched_at": time.time(), "stale": False, "error": ""}
    if not query:
        _cache_write(CACHE_REPOS, {"fetched_at": out["fetched_at"],
                                   "total_count": out["total_count"],
                                   "items": items})
    return out


# ----------------------------------------------------------------------------
# GitHub：单仓库技能枚举 + 技能包下载
# ----------------------------------------------------------------------------
def _split_full_name(full_name: str) -> Tuple[str, str]:
    """'owner/repo' -> (owner, repo)；非法抛 CatalogError(2022)。"""
    text = str(full_name or "").strip().strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) != 2:
        raise CatalogError(ERR_BAD_BUNDLE, f"仓库标识不合法：{full_name!r}")
    return parts[0], parts[1]


def _skill_dir_of(path: str) -> str:
    """'skills/pdf/SKILL.md' -> 'skills/pdf'；根目录 -> ''。"""
    norm = str(path or "").replace("\\", "/").strip("/")
    if "/" in norm:
        return norm.rsplit("/", 1)[0]
    return ""


def _is_text_file(path: str) -> bool:
    low = str(path or "").lower()
    return low.endswith(TEXT_SUFFIXES)


def _has_scripts(tree_paths: List[str], dirpath: str) -> bool:
    """技能目录下是否有可执行脚本（只提示，永不执行）。

    注意：带 prefix 时必须**跳过目录外的路径**。曾漏掉这步，导致任一技能只要
    兄弟目录里有 .py 就被标「含脚本」，界面上 20 个技能全挂 ⚠，警示形同虚设。
    """
    prefix = (dirpath.rstrip("/") + "/") if dirpath else ""
    for p in tree_paths:
        if prefix:
            if not p.startswith(prefix):
                continue
            rel = p[len(prefix):]
        else:
            rel = p
        low = rel.lower()
        if low.startswith("scripts/") or low.endswith((".py", ".sh", ".ps1",
                                                      ".bat", ".cmd", ".js")):
            return True
    return False


def _tree_cache_key(owner: str, repo: str, ref: str) -> str:
    return f"{owner}/{repo}@{ref}"


def _skills_cache_get(owner: str, repo: str, ref: str,
                      ttl: int) -> Optional[Dict[str, Any]]:
    cache = _cache_read(CACHE_SKILLS)
    if not cache or not _fresh(cache, ttl):
        return None
    repos = cache.get("repos") if isinstance(cache.get("repos"), dict) else {}
    entry = repos.get(_tree_cache_key(owner, repo, ref))
    return entry if isinstance(entry, dict) else None


def _skills_cache_put(owner: str, repo: str, ref: str,
                      entry: Dict[str, Any]) -> None:
    cache = _cache_read(CACHE_SKILLS)
    ttl = TTL_GH_SKILLS
    repos = cache.get("repos") if isinstance(cache.get("repos"), dict) else {}
    if not _fresh(cache, ttl):
        repos = {}                      # 整份过期：重置，避免无界增长
    repos[_tree_cache_key(owner, repo, ref)] = entry
    _cache_write(CACHE_SKILLS, {"fetched_at": time.time(), "repos": repos})


def _default_branch(cfg: Optional[Dict[str, Any]], owner: str, repo: str) -> str:
    """仓库默认分支：先查 gh_repos 缓存，未命中再打一次 /repos。"""
    for item in (_cache_read(CACHE_REPOS).get("items") or []):
        if str(item.get("full_name") or "").lower() == f"{owner}/{repo}".lower():
            branch = str(item.get("default_branch") or "").strip()
            if branch:
                return branch
    payload, _headers = _get_json(cfg, f"{GITHUB_API}/repos/{owner}/{repo}")
    body = payload if isinstance(payload, dict) else {}
    return str(body.get("default_branch") or "main")


def _collect_skill_items(repo: str, paths: List[str], all_paths: List[str],
                         read_text) -> List[Dict[str, Any]]:
    """从文件路径表里挑出 `**/SKILL.md`，读 frontmatter 生成条目。

    `read_text(path)` 由调用方提供（tree 路径走 raw.githubusercontent，tarball
    路径直接读内存成员），这样「tree API」与「tar 兜底」共用同一套解析逻辑，
    字段永远一致，不会出现两条路各错一处。
    """
    items: List[Dict[str, Any]] = []
    for path in paths:
        if not path.lower().endswith("skill.md"):
            continue
        dirpath = _skill_dir_of(path)
        text = ""
        try:
            text = read_text(path) or ""
        except Exception:                     # noqa: BLE001 - 单文件失败不毁整表
            text = ""
        meta: Dict[str, Any] = {}
        if text:
            try:
                from core.assistant.skills import parse_skill_md
                meta = parse_skill_md(text).get("meta") or {}
            except Exception:                 # noqa: BLE001
                meta = {}
        base = (dirpath.rsplit("/", 1)[-1] if dirpath else repo) or repo
        name = str(meta.get("name") or base)
        prefix = (dirpath.rstrip("/") + "/") if dirpath else ""
        files = [p[len(prefix):] if prefix else p for p in paths
                 if (not prefix or p.startswith(prefix)) and _is_text_file(p)]
        # SKILL.md 由 fetch_skill_bundle 必取，这里只列「附加文件」
        files = [f for f in files if f.lower() != "skill.md"]
        items.append({
            "slug": _slugify_server(name),
            "dir": dirpath,
            "name": name,
            "description": str(meta.get("description") or ""),
            "path": path,
            "files": files[:DEFAULT_MAX_FILES],
            "file_count": len(files),
            "has_scripts": _has_scripts(all_paths, dirpath),
        })
    return items


def _list_repo_skills_from_archive(cfg: Optional[Dict[str, Any]], owner: str,
                                   repo: str, ref: str) -> Dict[str, Any]:
    """兜底枚举：下一次 codeload tarball，在内存里扫 `**/SKILL.md`。

    为什么需要它：`/git/trees` 属 GitHub REST API，未认证时**每小时仅 60 次**。
    浏览两三个仓库（每个仓库还要逐文件取 frontmatter）就会 403，用户看到的就是
    「在线技能点不动、下载不了」。codeload 的 tar.gz 走独立通道，不受 API 配额
    限制，代价是多下一次整包（受 `ARCHIVE_MAX_DOWNLOAD` 约束）。
    """
    import io
    import tarfile

    url = f"{CODELOAD_BASE}/{owner}/{repo}/tar.gz/{ref}"
    try:
        resp = _do_get(cfg, url, None, timeout=ARCHIVE_TIMEOUT)
    except Exception as e:                    # noqa: BLE001
        raise CatalogError(
            ERR_UNREACHABLE,
            f"无法下载仓库快照（已尝试代理："
            f"{'、'.join(_proxy_candidates(cfg))}）：{type(e).__name__}: {e}"
        ) from e
    if resp.status_code != 200:
        raise CatalogError(ERR_UPSTREAM,
                           f"仓库快照下载失败 HTTP {resp.status_code}：{url}",
                           {"status": resp.status_code, "url": url})
    cl = str(resp.headers.get("content-length") or "").strip()
    if cl.isdigit() and int(cl) > ARCHIVE_MAX_DOWNLOAD:
        raise CatalogError(
            ERR_UPSTREAM,
            f"仓库快照过大（>{ARCHIVE_MAX_DOWNLOAD // 1024 // 1024}MB），"
            "无法在线枚举，请换用 GitHub Token 提高 API 配额",
            {"url": url})
    raw = resp.content
    if not raw:
        raise CatalogError(ERR_UPSTREAM, f"仓库快照为空：{url}")
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
        members = [m for m in tf.getmembers() if m.isfile()]
    except (tarfile.TarError, OSError) as e:
        raise CatalogError(ERR_UPSTREAM,
                           f"仓库快照解包失败：{type(e).__name__}: {e}")
    if not members:
        return {"commit": "", "ref": ref, "items": [], "fetched_at": time.time(),
                "stale": False, "error": "", "via": "archive"}
    top = members[0].name.split("/", 1)[0] + "/"
    rel_of = {m.name: m.name[len(top):] for m in members
              if m.name.startswith(top)}
    paths = [rel for rel in rel_of.values() if rel and not rel.endswith("/")]
    by_rel = {rel: name for name, rel in rel_of.items()}

    def _read(path: str) -> str:
        name = by_rel.get(path)
        if not name:
            return ""
        fh = tf.extractfile(name)
        if fh is None:
            return ""
        data = fh.read(MAX_SKILL_MD_BYTES + 1)
        if len(data) > MAX_SKILL_MD_BYTES:
            return ""
        return data.decode("utf-8", "replace")

    items = _collect_skill_items(repo, paths, paths, _read)
    return {"commit": "", "ref": ref, "items": items, "fetched_at": time.time(),
            "stale": False, "error": "", "via": "archive"}


def list_repo_skills(cfg: Optional[Dict[str, Any]] = None, owner: str = "",
                     repo: str = "", ref: str = "",
                     force: bool = False) -> Dict[str, Any]:
    """枚举一个仓库里的技能目录（`**/SKILL.md`），并读 frontmatter 校验。

    返回 `{commit, ref, items:[{slug, dir, name, description, files,
    has_scripts, path}], fetched_at, stale, via}`。

    只读 frontmatter（不读正文）：一个仓库可能有几十个 SKILL.md，正文下载
    交给 `fetch_skill_bundle`，避免"浏览列表"变成批量下载。

    三级降级：命中缓存 → tree API → **tarball 扫描**（API 限流时的兜底）。
    """
    owner = str(owner or "").strip()
    repo = str(repo or "").strip()
    if not owner or not repo:
        raise CatalogError(ERR_BAD_BUNDLE, "owner / repo 不能为空")
    branch = str(ref or "").strip()
    if not branch:
        try:
            branch = _default_branch(cfg, owner, repo)
        except CatalogError:
            branch = "main"                   # /repos 也可能被限流，别在这里卡死
    ttl = _ttl_seconds(cfg, "gh_skills")
    if not force:
        cached = _skills_cache_get(owner, repo, branch, ttl)
        if cached is not None:
            return {"commit": str(cached.get("commit") or ""), "ref": branch,
                    "items": cached.get("items") or [],
                    "fetched_at": cached.get("fetched_at") or 0,
                    "stale": False, "error": "",
                    "via": str(cached.get("via") or "api")}
    url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}"
    try:
        payload, _headers = _get_json(cfg, url, params={"recursive": "1"},
                                      timeout=CLIENT_TIMEOUT * 2)
    except CatalogError as e:
        cached = _skills_cache_get(owner, repo, branch, 10 ** 9)   # 不看 TTL
        if cached is not None:
            return {"commit": str(cached.get("commit") or ""), "ref": branch,
                    "items": cached.get("items") or [],
                    "fetched_at": cached.get("fetched_at") or 0,
                    "stale": True, "error": e.message,
                    "via": str(cached.get("via") or "api")}
        # 第二次机会：换 codeload tarball 扫（不受 API 配额），失败再抛原错
        try:
            out = _list_repo_skills_from_archive(cfg, owner, repo, branch)
        except CatalogError:
            raise e
        except Exception:                     # noqa: BLE001 - 兜底失败仍报原错
            raise e
        _skills_cache_put(owner, repo, branch,
                          {"commit": "", "items": out["items"],
                           "fetched_at": out["fetched_at"], "via": "archive"})
        return out
    body = payload if isinstance(payload, dict) else {}
    tree = [t for t in (body.get("tree") or []) if isinstance(t, dict)]
    paths = [str(t.get("path") or "") for t in tree
             if str(t.get("type") or "blob") == "blob"]
    all_paths = [str(t.get("path") or "") for t in tree]
    commit = str(body.get("sha") or body.get("commit") or "")[:40]

    def _read(path: str) -> str:
        return _get_text(cfg, f"{GITHUB_RAW}/{owner}/{repo}/{branch}/{path}")

    items = _collect_skill_items(repo, paths, all_paths, _read)
    out = {"commit": commit, "ref": branch, "items": items,
           "fetched_at": time.time(), "stale": False, "error": "",
           "via": "api"}
    _skills_cache_put(owner, repo, branch,
                      {"commit": commit, "items": items,
                       "fetched_at": out["fetched_at"], "via": "api"})
    return out


def fetch_skill_bundle(cfg: Optional[Dict[str, Any]] = None, owner: str = "",
                       repo: str = "", ref: str = "", dirpath: str = "",
                       max_files: int = DEFAULT_MAX_FILES,
                       max_bytes: int = DEFAULT_MAX_BYTES,
                       ) -> Dict[str, Any]:
    """把一个技能目录下的文本文件全下载到**内存**（不落盘，A1）。

    Returns:
        `{slug, files: {rel: text}, bytes, has_scripts, commit,
          skipped, total_files}`

    Raises:
        CatalogError(2022): 目录里没有 SKILL.md / frontmatter 缺 name /
                            SKILL.md 超过 200KB
    """
    owner = str(owner or "").strip()
    repo = str(repo or "").strip()
    if not owner or not repo:
        raise CatalogError(ERR_BAD_BUNDLE, "owner / repo 不能为空")
    dirpath = str(dirpath or "").strip().strip("/")
    ref = str(ref or "").strip() or _default_branch(cfg, owner, repo)

    listing = list_repo_skills(cfg, owner, repo, ref)
    hit = next((i for i in listing.get("items") or []
                if str(i.get("dir") or "") == dirpath), None)
    if hit is None:
        raise CatalogError(ERR_BAD_BUNDLE,
                           f"目录 {dirpath or '/'} 里没有 SKILL.md，不是技能包",
                           {"owner": owner, "repo": repo, "dir": dirpath})
    md_path = str(hit.get("path") or "").strip()
    if not md_path:
        md_path = f"{dirpath}/SKILL.md" if dirpath else "SKILL.md"

    files: Dict[str, str] = {}
    total = 0
    skipped = 0
    # SKILL.md 必取，且单独校验大小
    md_text = _get_text(cfg, f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{md_path}")
    if len(md_text.encode("utf-8", "replace")) > MAX_SKILL_MD_BYTES:
        raise CatalogError(ERR_BAD_BUNDLE,
                           f"SKILL.md 过大（>{MAX_SKILL_MD_BYTES // 1024}KB）")
    try:
        from core.assistant.skills import parse_skill_md
        meta = parse_skill_md(md_text).get("meta") or {}
    except Exception:
        meta = {}
    if not str(meta.get("name") or "").strip():
        raise CatalogError(ERR_BAD_BUNDLE,
                           "SKILL.md 的 frontmatter 缺少 name，不是有效技能包",
                           {"owner": owner, "repo": repo, "dir": dirpath})
    files["SKILL.md"] = md_text
    total += len(md_text.encode("utf-8", "replace"))
    name = str(meta.get("name") or "").strip()

    prefix = (dirpath.rstrip("/") + "/") if dirpath else ""
    candidates: List[str] = []
    for p in (hit.get("files") or []):
        rel = str(p)
        if rel.lower() == "skill.md":
            continue
        candidates.append(rel)
    for rel in candidates:
        if len(files) >= max(1, int(max_files or DEFAULT_MAX_FILES)):
            skipped += 1
            continue
        src = f"{prefix}{rel}" if prefix else rel
        try:
            text = _get_text(cfg, f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{src}")
        except CatalogError:
            skipped += 1
            continue
        size = len(text.encode("utf-8", "replace"))
        if total + size > max_bytes:
            skipped += 1
            continue
        files[rel] = text
        total += size
        _ = src

    return {
        "slug": _slugify_server(name),
        "name": name,
        "description": str(meta.get("description") or ""),
        "files": files,
        "bytes": total,
        "has_scripts": bool(hit.get("has_scripts")),
        "commit": str(listing.get("commit") or ""),
        "ref": ref,
        "repo": f"{owner}/{repo}",
        "dir": dirpath,
        "skipped": skipped,
        "total_files": len(candidates) + 1,
    }


def fetch_skill_archive(cfg: Optional[Dict[str, Any]] = None, owner: str = "",
                        repo: str = "", ref: str = "", dirpath: str = "",
                        max_files: int = ARCHIVE_MAX_FILES,
                        max_bytes: int = ARCHIVE_MAX_BYTES,
                        ) -> Dict[str, Any]:
    """下载技能目录的**整棵子树 tarball**（支持任意二进制 + 大体积）。

    走 `codeload.github.com/{owner}/{repo}/tar.gz/{ref}`（无 API 限流、整目录一次
    拿全），在内存里只抽取 `dirpath` 子树，返回
    `{slug, files:{rel:bytes}, bytes, has_scripts, commit, ref, repo, dir,
      skipped, total_files, binary:True}`。

    与文本管线（`fetch_skill_bundle`）互补：文本管线仍用于「粘贴单文件 / 预览」，
    整包安装一律走本函数，从而不再丢 SVG / 图标 / 音效等二进制资产。

    安全红线（与文本管线一致，架构 §8）：
      - 不执行任何文件；
      - 抽取路径做 `..` / 绝对 / 盘符 / 符号链接 / 硬链接逃逸校验，越界成员跳过
        并计入 `skipped`（不抛，不静默通过）；
      - 受 `max_files` / `max_bytes`（抽取预算）与 `ARCHIVE_MAX_DOWNLOAD`
        （下载预算，content-length 预检）约束，超出部分计入 `skipped`。
    """
    import io
    import tarfile

    owner = str(owner or "").strip()
    repo = str(repo or "").strip()
    if not owner or not repo:
        raise CatalogError(ERR_BAD_BUNDLE, "owner / repo 不能为空")
    dirpath = str(dirpath or "").strip().strip("/")
    ref = str(ref or "").strip() or _default_branch(cfg, owner, repo)

    # 1) 借 list_repo_skills 拿到 commit + 确认目录存在 + has_scripts（有缓存）
    listing = list_repo_skills(cfg, owner, repo, ref)
    hit = next((i for i in listing.get("items") or []
                if str(i.get("dir") or "") == dirpath), None)
    if hit is None:
        raise CatalogError(ERR_BAD_BUNDLE,
                           f"目录 {dirpath or '/'} 里没有 SKILL.md，不是技能包",
                           {"owner": owner, "repo": repo, "dir": dirpath})
    md_path = str(hit.get("path") or "").strip()
    if not md_path:
        md_path = f"{dirpath}/SKILL.md" if dirpath else "SKILL.md"
    commit = str(listing.get("commit") or "")
    has_scripts = bool(hit.get("has_scripts"))

    # 2) 下载整子树 tarball（codeload 无 API 限流，单次拿全）
    #    注意：SKILL.md 的校验放在**解包之后**从 tarball 里读，不再单独打
    #    raw.githubusercontent.com —— 少一个依赖点（国内该域名常被掐，
    #    掐了就等于「在线安装永远失败」，而 codeload 通着）。
    url = f"{CODELOAD_BASE}/{owner}/{repo}/tar.gz/{ref}"
    try:
        resp = _do_get(cfg, url, None, timeout=ARCHIVE_TIMEOUT)
    except Exception as e:                       # noqa: BLE001
        raise CatalogError(
            ERR_UNREACHABLE,
            f"无法下载技能包 tarball（已尝试代理："
            f"{'、'.join(_proxy_candidates(cfg))}）：{type(e).__name__}: {e}"
        ) from e
    if resp.status_code != 200:
        raise CatalogError(ERR_UPSTREAM,
                           f"tarball 下载失败 HTTP {resp.status_code}：{url}",
                           {"status": resp.status_code, "url": url})
    cl = resp.headers.get("content-length")
    if cl and str(cl).strip().isdigit() \
            and int(cl) > ARCHIVE_MAX_DOWNLOAD:
        raise CatalogError(
            ERR_UPSTREAM,
            f"技能包 tarball 过大（>{ARCHIVE_MAX_DOWNLOAD // 1024 // 1024}MB），已中止",
            {"url": url})
    raw = resp.content
    if not raw:
        raise CatalogError(ERR_UPSTREAM, f"tarball 为空：{url}")

    # 4) 抽取 dirpath 子树（内存，不落盘；路径严格校验）
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except (tarfile.TarError, OSError) as e:
        raise CatalogError(ERR_UPSTREAM,
                           f"tarball 解包失败：{type(e).__name__}: {e}")
    prefix = (dirpath + "/") if dirpath else ""
    files: Dict[str, bytes] = {}
    total = 0
    skipped = 0
    total_files = 0
    seen_top: Optional[str] = None
    try:
        members = list(tf)
        # 找公共顶层目录 top/（codeload 的 tarball 顶层是 repo-<ref>）
        for m in members:
            if m.isdir():
                first = m.name.rstrip("/")
                if "/" not in first:
                    seen_top = first + "/"
                    break
        if seen_top is None and members:
            seen_top = members[0].name.split("/", 1)[0] + "/"
        if not seen_top:
            raise CatalogError(ERR_BAD_BUNDLE, "tarball 结构异常（无顶层目录）")
        for m in members:
            name_in = str(m.name or "")
            if not name_in.startswith(seen_top):
                continue
            rel_all = name_in[len(seen_top):]
            if prefix:
                if not rel_all.startswith(prefix):
                    continue
                rel = rel_all[len(prefix):]
            else:
                rel = rel_all
            if not rel or rel.endswith("/"):
                continue
            # 路径逃逸校验（与 skills._safe_rel 同源）
            parts = rel.split("/")
            if ".." in parts or rel.startswith("/") \
                    or (len(rel) >= 2 and rel[1] == ":"):
                skipped += 1
                continue
            # 拒绝符号/硬链接（防逃逸 + 跨平台便携性）
            if m.issym() or m.islnk():
                skipped += 1
                continue
            if not m.isfile():
                continue
            total_files += 1
            if len(files) >= max(1, int(max_files or ARCHIVE_MAX_FILES)):
                skipped += 1
                continue
            try:
                ef = tf.extractfile(m)
                data = ef.read() if ef is not None else b""
            except Exception:
                skipped += 1
                continue
            if total + len(data) > max_bytes:
                skipped += 1
                continue
            files[rel] = data
            total += len(data)
    finally:
        tf.close()

    if "SKILL.md" not in files:
        raise CatalogError(ERR_BAD_BUNDLE,
                           "tarball 中未找到 SKILL.md（目录可能为空）",
                           {"owner": owner, "repo": repo, "dir": dirpath})

    # 5) 校验 frontmatter（数据来自刚解出来的 SKILL.md，零额外请求）
    md_bytes = files.get("SKILL.md") or b""
    if len(md_bytes) > MAX_SKILL_MD_BYTES:
        raise CatalogError(ERR_BAD_BUNDLE,
                           f"SKILL.md 过大（>{MAX_SKILL_MD_BYTES // 1024}KB）")
    md_text = md_bytes.decode("utf-8", "replace")
    try:
        from core.assistant.skills import parse_skill_md
        meta = parse_skill_md(md_text).get("meta") or {}
    except Exception:
        meta = {}
    name = str(meta.get("name") or "").strip() or str(hit.get("name") or "").strip()
    if not name:
        raise CatalogError(ERR_BAD_BUNDLE,
                           "SKILL.md 的 frontmatter 缺少 name，不是有效技能包",
                           {"owner": owner, "repo": repo, "dir": dirpath})

    return {
        "slug": _slugify_server(name),
        "name": name,
        "description": str(meta.get("description") or ""),
        "files": files,            # bytes 字典（二进制安全）
        "bytes": total,
        "has_scripts": has_scripts,
        "commit": commit,
        "ref": ref,
        "repo": f"{owner}/{repo}",
        "dir": dirpath,
        "skipped": skipped,
        "total_files": total_files + 1,
        "binary": True,
    }


# ----------------------------------------------------------------------------
# URL 归一
# ----------------------------------------------------------------------------
_GITHUB_RAW_RE = re.compile(
    r"^https?://raw\.githubusercontent\.com/([^/]+)/([^/]+)/([^/]+)/(.+)$", re.I)
_GITHUB_BLOB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([^/]+)/([^/]+)/(?:blob|tree|raw)/"
    r"([^/]+)/(.+)$", re.I)
_GITEE_BLOB_RE = re.compile(
    r"^https?://(?:www\.)?gitee\.com/([^/]+)/([^/]+)/(?:blob|raw)/([^/]+)/(.+)$",
    re.I)
_GITEE_RAW_RE = re.compile(
    r"^https?://gitee\.com/([^/]+)/([^/]+)/raw/([^/]+)/(.+)$", re.I)


def url_to_raw(url: str) -> Dict[str, Any]:
    """把用户粘贴的地址归一成 raw 直链 + 路径分段。

    支持：
        https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md
        https://github.com/o/r/blob/main/skills/pdf/SKILL.md
        https://github.com/o/r/tree/main/skills/pdf          （目录 -> SKILL.md）
        https://gitee.com/o/r/raw/main/skills/pdf/SKILL.md

    Raises:
        CatalogError(2026): 不是受支持的技能地址
    """
    text = str(url or "").strip()
    if not text:
        raise CatalogError(ERR_BAD_URL, "URL 不能为空")
    m = _GITHUB_RAW_RE.match(text)
    if m:
        owner, repo, ref, path = m.groups()
        raw_url, provider = text, "github"
    else:
        m = _GITHUB_BLOB_RE.match(text)
        if m:
            owner, repo, ref, path = m.groups()
            provider = "github"
            raw_url = f"{GITHUB_RAW}/{owner}/{repo}/{ref}/{path}"
        else:
            m = _GITEE_RAW_RE.match(text) or _GITEE_BLOB_RE.match(text)
            if not m:
                raise CatalogError(
                    ERR_BAD_URL,
                    "只支持 GitHub / Gitee 上的 SKILL.md 直链（raw 或 blob 形式）",
                    {"url": text})
            owner, repo, ref, path = m.groups()
            provider = "gitee"
            raw_url = text
    path = str(path or "").strip("/")
    if path.lower().endswith("skill.md"):
        dirpath = _skill_dir_of(path)
    else:
        dirpath = path
    return {"raw_url": raw_url, "owner": owner, "repo": repo, "ref": ref,
            "path": path, "dir": dirpath, "provider": provider}


def fetch_bundle_from_raw(cfg: Optional[Dict[str, Any]] = None,
                          url: str = "") -> Dict[str, Any]:
    """从**单个 SKILL.md 直链**取一个技能包（无树可用时只取 SKILL.md）。"""
    info = url_to_raw(url)
    md_text = _get_text(cfg, info["raw_url"])
    if len(md_text.encode("utf-8", "replace")) > MAX_SKILL_MD_BYTES:
        raise CatalogError(ERR_BAD_BUNDLE,
                           f"SKILL.md 过大（>{MAX_SKILL_MD_BYTES // 1024}KB）")
    try:
        from core.assistant.skills import parse_skill_md
        meta = parse_skill_md(md_text).get("meta") or {}
    except Exception:
        meta = {}
    if not str(meta.get("name") or "").strip():
        raise CatalogError(ERR_BAD_BUNDLE,
                           "SKILL.md 的 frontmatter 缺少 name，不是有效技能包")
    name = str(meta.get("name") or "").strip()
    return {"slug": _slugify_server(name), "name": name,
            "description": str(meta.get("description") or ""),
            "files": {"SKILL.md": md_text},
            "bytes": len(md_text.encode("utf-8", "replace")),
            "has_scripts": False, "commit": "", "ref": info["ref"],
            "repo": f"{info['owner']}/{info['repo']}", "dir": info["dir"],
            "skipped": 0, "total_files": 1}


# ----------------------------------------------------------------------------
# 全量刷新
# ----------------------------------------------------------------------------
def refresh_all(cfg: Optional[Dict[str, Any]] = None,
                force: bool = True) -> Dict[str, Any]:
    """并行刷新三源（ThreadPoolExecutor(3)），总时长上限 12s。

    任何单源失败只记进 `errors[]`，不影响其他源；本函数**永不抛**——
    路由层固定返回 ok()，错误在 data 里。
    """
    cfg = cfg or {}
    errors: Dict[str, str] = {}
    counts: Dict[str, int] = {"mcp": 0, "gh_repos": 0, "gh_skills": 0}

    def _do_mcp() -> Dict[str, Any]:
        return list_mcp(cfg, force=force)

    def _do_repos() -> Dict[str, Any]:
        return list_repos(cfg, force=force)

    def _do_skills() -> Dict[str, Any]:
        """预热 gh_skills 缓存：只对仓库列表前若干个仓库拉 tree。

        配额不可控（PRD P2-5 已砍全站扫描），所以固定上限 `WARM_REPOS=3`：
        首屏「在线技能」区能立刻有内容，又不至于刷爆 GitHub 限流。
        """
        repos = (_cache_read(CACHE_REPOS).get("items") or [])
        warm_targets = [r for r in repos if r.get("full_name")][:WARM_REPOS]
        warmed = 0
        last_error = ""
        for r in warm_targets:
            try:
                owner, name = _split_full_name(str(r.get("full_name") or ""))
            except CatalogError:
                continue
            try:
                listing = list_repo_skills(cfg, owner, name,
                                           str(r.get("default_branch") or ""),
                                           force=force)
            except CatalogError as e:
                last_error = e.message
                continue
            warmed += len(listing.get("items") or [])
        return {"items": [None] * warmed, "count": warmed, "fetched_at": time.time(),
                "stale": False, "error": last_error}

    jobs = {"mcp": _do_mcp, "gh_repos": _do_repos, "gh_skills": _do_skills}
    ok_any = False
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            futures = {name: ex.submit(fn) for name, fn in jobs.items()}
            for name, fut in futures.items():
                try:
                    res = fut.result(timeout=REFRESH_TOTAL_TIMEOUT)
                except concurrent.futures.TimeoutError:
                    errors[name] = f"超时（>{int(REFRESH_TOTAL_TIMEOUT)}s）"
                    continue
                except CatalogError as e:
                    errors[name] = e.message
                    continue
                except Exception as e:               # noqa: BLE001
                    errors[name] = f"{type(e).__name__}: {e}"
                    continue
                if res.get("error"):
                    errors[name] = str(res["error"])
                if name in counts:
                    if name == "gh_skills":
                        counts[name] = int(res.get("count") or 0)
                    else:
                        counts[name] = len(res.get("items") or [])
                if not res.get("error"):
                    ok_any = True
    except Exception as e:                           # noqa: BLE001
        errors["refresh"] = f"{type(e).__name__}: {e}"
    error_list = [f"{k}: {v}" for k, v in errors.items()]
    return {"ok": ok_any and not error_list,
            "counts": counts,
            "errors": error_list,
            "fetched_at": time.time()}
