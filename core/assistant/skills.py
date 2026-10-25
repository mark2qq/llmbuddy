# -*- coding: utf-8 -*-
"""
技能包管理（core/assistant/skills.py）。

技能 = 一个目录 + SKILL.md，与 OpenClaw / Claude Skill 同构：

    ---
    name: repo-digest
    description: 扫描代码库生成结构化摘要
    tools: [fs_glob, fs_read, fs_grep]
    version: 1.0.0
    ---
    # 执行要求
    1. 先用 fs_glob 列出 ...
    2. 再 ...

frontmatter 里的 tools 是「该技能允许调用的工具白名单」（留空=不限制），
orchestrator 会用 LangGraph 把 SKILL.md 的指令拆成 plan → act → review → finish
四个阶段执行，技能正文就是这几个阶段的系统提示词。

导入导出均为 zip：zip 根目录或一级子目录里包含 SKILL.md 即可，兼容从
OpenClaw / Skills 市场直接下载的包。
"""
import io
import os
import re
import shutil
import time
import uuid
import zipfile
from typing import Any, Dict, List, Optional

import yaml

from core.assistant.store import SKILLS_DIR, audit, ensure_dirs

MAX_ZIP_BYTES = 20 * 1024 * 1024
_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

#: 从本地文件夹安装时收取的文本类后缀（脚本永不执行，只落文件）
TEXT_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".py", ".toml", ".cfg",
                 ".ini", ".csv", ".html", ".js", ".ts", ".sh", ".ps1", ".bat",
                 ".cmd", ".env", ".gitignore", ".mod", ".sum")
#: 单个文本文件的体积上限（本地目录导入时跳过更大的文件）
MAX_SKILL_MD_BYTES = 200 * 1024


class SkillError(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------------
def parse_skill_md(text: str) -> Dict[str, Any]:
    """解析 SKILL.md：返回 {meta, body}。frontmatter 非法时 meta 为空。"""
    meta: Dict[str, Any] = {}
    body = text or ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", body, re.S)
    if m:
        raw, body = m.group(1), m.group(2)
        try:
            data = yaml.safe_load(raw)
            if isinstance(data, dict):
                meta = data
        except Exception:
            # YAML 异常时退化为 key: value 逐行解析
            for line in raw.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"').strip("'")
    name = str(meta.get("name") or "").strip()
    desc = str(meta.get("description") or "").strip()
    tools = meta.get("tools") or meta.get("allowed-tools") or []
    if isinstance(tools, str):
        tools = [t.strip() for t in re.split(r"[,\s]+", tools) if t.strip()]
    meta["name"] = name
    meta["description"] = desc
    meta["tools"] = [str(t) for t in tools] if isinstance(tools, list) else []
    return {"meta": meta, "body": body.strip()}


def _dir_size(path: str) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                continue
    return total


# ----------------------------------------------------------------------------
# 列表 / 读取
# ----------------------------------------------------------------------------
def list_skills() -> List[Dict[str, Any]]:
    ensure_dirs()
    out: List[Dict[str, Any]] = []
    for slug in sorted(os.listdir(SKILLS_DIR)):
        # 架构 §5 A8：安装临时目录 `.installing-<uuid>` 与备份 `.backup-<ts>`
        # 也可能含 SKILL.md，必须跳过，否则会被当成两个额外技能列出来。
        if slug.startswith("."):
            continue
        d = os.path.join(SKILLS_DIR, slug)
        md = os.path.join(d, "SKILL.md")
        if not (os.path.isdir(d) and os.path.isfile(md)):
            continue
        try:
            with open(md, "r", encoding="utf-8", errors="replace") as f:
                parsed = parse_skill_md(f.read())
        except OSError:
            continue
        meta = parsed["meta"]
        out.append({
            "slug": slug,
            "name": meta.get("name") or slug,
            "description": meta.get("description") or "",
            "tools": meta.get("tools") or [],
            "version": str(meta.get("version") or "1.0.0"),
            "builtin": bool(meta.get("builtin")),
            "files": sorted(os.listdir(d)),
            "size": _dir_size(d),
            "updated_at": os.path.getmtime(md),
        })
    return out


def load_skill(slug: str) -> Optional[Dict[str, Any]]:
    """按 slug 或 name 读技能，返回 {slug, meta, body, dir}。"""
    ensure_dirs()
    key = str(slug or "").strip()
    if not key:
        return None
    candidates = [key]
    for s in list_skills():
        if s["name"] == key and s["slug"] not in candidates:
            candidates.append(s["slug"])
    for cand in candidates:
        if not _SLUG_RE.match(cand):
            continue
        d = os.path.join(SKILLS_DIR, cand)
        md = os.path.join(d, "SKILL.md")
        if os.path.isfile(md):
            with open(md, "r", encoding="utf-8", errors="replace") as f:
                parsed = parse_skill_md(f.read())
            meta = parsed["meta"]
            meta.setdefault("name", cand)
            return {"slug": cand, "meta": meta, "body": parsed["body"], "dir": d}
    return None


def read_skill_file(slug: str, rel: str) -> str:
    """读取技能目录内的附属文件（防目录穿越）。"""
    skill = load_skill(slug)
    if skill is None:
        raise SkillError(f"技能不存在：{slug}")
    base = os.path.abspath(skill["dir"])
    target = os.path.abspath(os.path.join(base, rel))
    if os.path.commonpath([base, target]) != base:
        raise SkillError("非法路径")
    if not os.path.isfile(target):
        raise SkillError(f"文件不存在：{rel}")
    with open(target, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


# ----------------------------------------------------------------------------
# 增删改
# ----------------------------------------------------------------------------
def save_skill(slug: str, content: str, files: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """写入（覆盖）一个技能。files 为 {相对路径: 文本内容} 的附属文件。"""
    if not _SLUG_RE.match(slug or ""):
        raise SkillError("技能标识只能包含字母、数字、点、下划线、连字符")
    ensure_dirs()
    d = os.path.join(SKILLS_DIR, slug)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    for rel, text in (files or {}).items():
        rel = str(rel).replace("\\", "/").lstrip("/")
        if ".." in rel.split("/"):
            continue
        target = os.path.join(d, rel)
        os.makedirs(os.path.dirname(target) or d, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
    audit("skill_saved", slug=slug)
    return {"slug": slug, "path": d}


def delete_skill(slug: str) -> bool:
    if not _SLUG_RE.match(slug or ""):
        return False
    d = os.path.join(SKILLS_DIR, slug)
    if not os.path.isdir(d):
        return False
    shutil.rmtree(d, ignore_errors=True)
    audit("skill_deleted", slug=slug)
    return True


# ----------------------------------------------------------------------------
# 导入 / 导出
# ----------------------------------------------------------------------------
def import_zip(data: bytes, overwrite: bool = True) -> Dict[str, Any]:
    """从 zip 字节导入技能（一个 zip 可含多个技能）。"""
    if not data:
        raise SkillError("空文件")
    if len(data) > MAX_ZIP_BYTES:
        raise SkillError(f"压缩包过大（>{MAX_ZIP_BYTES // 1024 // 1024}MB）")
    ensure_dirs()
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillError("不是有效的 zip 文件（请把技能目录打包为 zip）")
    names = [n for n in zf.namelist() if not n.endswith("/")]
    md_names = [n for n in names if os.path.basename(n).lower() == "skill.md"]
    if not md_names:
        raise SkillError("压缩包内没有 SKILL.md")
    # 以 SKILL.md 所在目录为技能根
    roots = sorted({n[: -len("SKILL.md")].rstrip("/") for n in md_names})
    imported: List[str] = []
    for root in roots:
        if not root:
            with zf.open(next(n for n in md_names if n.lower() == "skill.md")) as f:
                parsed = parse_skill_md(f.read().decode("utf-8", "replace"))
            slug = _slugify(parsed["meta"].get("name") or
                            os.path.splitext(os.path.basename(md_names[0]))[0] or "skill")
        else:
            with zf.open(root + "/SKILL.md") as f:
                parsed = parse_skill_md(f.read().decode("utf-8", "replace"))
            slug = _slugify(parsed["meta"].get("name") or os.path.basename(root))
        target = os.path.join(SKILLS_DIR, slug)
        if os.path.isdir(target) and not overwrite:
            raise SkillError(f"技能已存在：{slug}")
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        os.makedirs(target, exist_ok=True)
        prefix = (root + "/") if root else ""
        for n in names:
            if prefix and not n.startswith(prefix):
                continue
            rel = n[len(prefix):] if prefix else n
            if not rel or ".." in rel.split("/"):
                continue
            dest = os.path.join(target, rel)
            os.makedirs(os.path.dirname(dest) or target, exist_ok=True)
            with zf.open(n) as src, open(dest, "wb") as out:
                out.write(src.read())
        imported.append(slug)
    zf.close()
    audit("skill_imported", slugs=imported)
    return {"imported": imported, "count": len(imported)}


def export_zip(slug: str) -> bytes:
    """把技能目录打成 zip 字节（根目录即 SKILL.md 所在层）。"""
    skill = load_skill(slug)
    if skill is None:
        raise SkillError(f"技能不存在：{slug}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        base = skill["dir"]
        for dirpath, _dirs, files in os.walk(base):
            for fn in files:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base).replace("\\", "/")
                zf.write(full, rel)
    audit("skill_exported", slug=skill["slug"])
    return buf.getvalue()


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(name or "").strip()).strip("-.")
    s = s.lower()[:64]
    return s or f"skill-{int(time.time())}"


# ----------------------------------------------------------------------------
# 在线 / 本地目录安装（1.0.8 增量，架构 §5 A1 / A8）
#
# 所有落盘路径收敛到 `_atomic_install()`：
#     .installing-<uuid> 写全 -> 校验 frontmatter -> [备份旧目录] ->
#     os.replace 到 <slug> -> prune_backups；任一步失败全量回滚，不留半成品。
# 临时目录名以 `.` 开头，天然不被 `list_skills()` 与 `_SLUG_RE` 接受。
# ----------------------------------------------------------------------------
INSTALLING_PREFIX = ".installing-"
BACKUP_PREFIX = ".backup-"
MAX_BACKUPS = 5
MAX_INSTALL_BYTES = 8 * 1024 * 1024


def _safe_rel(rel: str) -> str:
    """附属文件相对路径安全检查：拒绝 ..、盘符、绝对路径。"""
    text = str(rel or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.lstrip("/")
    if not text:
        raise SkillError("附属文件名不能为空")
    if ".." in text.split("/"):
        raise SkillError(f"非法文件路径（含 ..）：{rel}")
    if len(text) >= 2 and text[1] == ":":
        raise SkillError(f"非法文件路径（含盘符）：{rel}")
    return text


def _new_temp_dir() -> str:
    """创建 `.installing-<uuid>` 临时目录（不校验 slug，见架构 §5 A8 末尾说明）。"""
    ensure_dirs()
    path = os.path.join(SKILLS_DIR, INSTALLING_PREFIX + uuid.uuid4().hex)
    os.makedirs(path, exist_ok=False)
    return path


def _write_files(root: str, files: Dict[str, str]) -> int:
    """把 `{rel: text}` 写进 root（自动建父目录），返回写入字节数。

    只落文件，**永不执行任何脚本**（安全红线）。
    """
    total = 0
    for rel, text in (files or {}).items():
        safe = _safe_rel(rel)
        target = os.path.join(root, safe)
        os.makedirs(os.path.dirname(target) or root, exist_ok=True)
        body = "" if text is None else str(text)
        data = body.encode("utf-8", "replace")
        total += len(data)
        if total > MAX_INSTALL_BYTES:
            raise SkillError(f"技能包过大（>{MAX_INSTALL_BYTES // 1024 // 1024}MB）")
        with open(target, "w", encoding="utf-8") as f:
            f.write(body)
    return total


def _rollback(tmp_dir: str, backup_dir: str, slug: str) -> None:
    """回滚：删临时目录；若有备份则还原到 `<slug>`（失败静默）。"""
    shutil.rmtree(tmp_dir, ignore_errors=True)
    target = os.path.join(SKILLS_DIR, slug)
    if backup_dir and os.path.isdir(backup_dir):
        try:
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            os.replace(backup_dir, target)
        except OSError:
            pass


def prune_backups(keep: int = MAX_BACKUPS) -> int:
    """只保留最近 `keep` 份 `.backup-<ts>`，返回删除份数。"""
    ensure_dirs()
    try:
        names = [n for n in os.listdir(SKILLS_DIR) if n.startswith(BACKUP_PREFIX)]
    except OSError:
        return 0
    if len(names) <= max(0, int(keep)):
        return 0
    names.sort(key=lambda n: os.path.getmtime(os.path.join(SKILLS_DIR, n))
               if os.path.exists(os.path.join(SKILLS_DIR, n)) else 0)
    removed = 0
    for name in names[: len(names) - max(0, int(keep))]:
        shutil.rmtree(os.path.join(SKILLS_DIR, name), ignore_errors=True)
        removed += 1
    return removed


def _atomic_install(slug: str, files: Dict[str, str],
                    overwrite: bool = False) -> Dict[str, Any]:
    """原子安装一个技能包。

    Args:
        slug: 目标目录名，必须过 `_SLUG_RE`（首字符字母/数字，天然拒绝 `.` 开头）。
        files: `{相对路径: 文本}`，必须含 `SKILL.md` 且 frontmatter 有 name。
        overwrite: 目标已存在时是否覆盖（旧目录先搬到 `.backup-<ts>`）。

    Returns:
        `{slug, files, bytes, path, backup, overwritten}`

    Raises:
        SkillError: slug 非法 / 缺 SKILL.md / frontmatter 缺 name / 已存在且不覆盖。
    """
    if not _SLUG_RE.match(slug or ""):
        raise SkillError("技能标识只能包含字母、数字、点、下划线、连字符")
    payload = dict(files or {})
    md_key = next((k for k in payload
                   if str(k).replace("\\", "/").lower() == "skill.md"), None)
    if md_key is None:
        raise SkillError("技能包缺少 SKILL.md")
    parsed = parse_skill_md(str(payload.get(md_key) or ""))
    if not str(parsed["meta"].get("name") or "").strip():
        raise SkillError("SKILL.md 的 frontmatter 缺少 name，不是有效技能包")

    ensure_dirs()
    target = os.path.join(SKILLS_DIR, slug)
    if os.path.isdir(target) and not overwrite:
        raise SkillError(f"技能已存在：{slug}")

    tmp_dir = _new_temp_dir()
    backup_dir = ""
    try:
        written = _write_files(tmp_dir, payload)
        if os.path.isdir(target):
            backup_dir = os.path.join(SKILLS_DIR,
                                      BACKUP_PREFIX + time.strftime("%Y%m%d%H%M%S")
                                      + "-" + uuid.uuid4().hex[:6])
            os.replace(target, backup_dir)
        os.replace(tmp_dir, target)
    except Exception as e:                      # noqa: BLE001
        _rollback(tmp_dir, backup_dir, slug)
        if isinstance(e, SkillError):
            raise
        raise SkillError(f"安装失败（已回滚）：{type(e).__name__}: {e}") from e

    prune_backups()
    audit("skill_installed", slug=slug, files=len(payload), bytes=written,
          overwrite=bool(overwrite), backup=os.path.basename(backup_dir))
    return {"slug": slug, "files": sorted(payload.keys()), "bytes": written,
            "path": target, "backup": os.path.basename(backup_dir),
            "overwritten": bool(backup_dir), "name": parsed["meta"].get("name")}


def install_from_github(bundle: Dict[str, Any], slug: str = "",
                        overwrite: bool = False,
                        source: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """安装 `catalog.fetch_skill_bundle()` 的内存包（A1：catalog 只交字节）。

    Args:
        bundle: `{slug, files:{rel:text}, repo, ref, dir, commit, has_scripts}`
        slug: 覆盖目标 slug（为空则用 bundle 里的 slug）
        overwrite: 是否覆盖已存在技能
        source: 写进 `config.skill_sources` 的来源信息（可选）
    """
    bundle = bundle or {}
    files = bundle.get("files") if isinstance(bundle.get("files"), dict) else {}
    target_slug = _slugify(slug or bundle.get("slug") or bundle.get("name") or "skill")
    result = _atomic_install(target_slug, files, overwrite=overwrite)
    result.update({
        "commit": str(bundle.get("commit") or ""),
        "ref": str(bundle.get("ref") or ""),
        "repo": str(bundle.get("repo") or ""),
        "dir": str(bundle.get("dir") or ""),
        "has_scripts": bool(bundle.get("has_scripts")),
        "skipped": int(bundle.get("skipped") or 0),
    })
    if source:
        try:
            from core.assistant.store import load_config, save_config
            cur = dict(load_config().get("skill_sources") or {})
            rec = {"kind": "github", "installed_at": time.time()}
            rec.update({k: v for k, v in source.items() if v is not None})
            cur[target_slug] = rec
            save_config({"skill_sources": cur})
        except Exception:
            pass
    return result


def install_from_dir(src: str, overwrite: bool = False,
                     slug: str = "") -> Dict[str, Any]:
    """从本地文件夹安装（只读文本类文件；不跟随符号链接）。"""
    root = os.path.abspath(str(src or ""))
    if not os.path.isdir(root):
        raise SkillError(f"不是目录：{src}")
    md_path = os.path.join(root, "SKILL.md")
    if not os.path.isfile(md_path):
        raise SkillError("该目录里没有 SKILL.md，不是技能包")
    with open(md_path, "r", encoding="utf-8", errors="replace") as f:
        md_text = f.read()
    parsed = parse_skill_md(md_text)
    if not str(parsed["meta"].get("name") or "").strip():
        raise SkillError("SKILL.md 的 frontmatter 缺少 name，不是有效技能包")
    target_slug = _slugify(slug or parsed["meta"].get("name")
                           or os.path.basename(root))
    files: Dict[str, str] = {"SKILL.md": md_text}
    total = len(md_text.encode("utf-8", "replace"))
    has_scripts = False
    for dirpath, _dirs, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            if rel.lower() == "skill.md":
                continue
            low = rel.lower()
            if low.startswith("scripts/") or low.endswith((".py", ".sh", ".ps1",
                                                          ".bat", ".cmd", ".js")):
                has_scripts = True
            if not low.endswith(TEXT_SUFFIXES):
                continue
            try:
                if os.path.getsize(full) > MAX_SKILL_MD_BYTES:
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            if total + len(text.encode("utf-8", "replace")) > MAX_INSTALL_BYTES:
                continue
            total += len(text.encode("utf-8", "replace"))
            files[rel] = text
    result = _atomic_install(target_slug, files, overwrite=overwrite)
    result.update({"commit": "", "ref": "", "repo": root, "dir": "",
                   "has_scripts": has_scripts, "skipped": 0})
    return result


def install_from_url(bundle: Dict[str, Any], slug: str = "",
                     overwrite: bool = False) -> Dict[str, Any]:
    """安装 `catalog.fetch_bundle_from_raw()` 的结果（粘贴 URL 安装）。"""
    bundle = bundle or {}
    files = bundle.get("files") if isinstance(bundle.get("files"), dict) else {}
    target_slug = _slugify(slug or bundle.get("slug") or bundle.get("name") or "skill")
    result = _atomic_install(target_slug, files, overwrite=overwrite)
    result.update({"commit": str(bundle.get("commit") or ""),
                   "ref": str(bundle.get("ref") or ""),
                   "repo": str(bundle.get("repo") or ""),
                   "dir": str(bundle.get("dir") or ""),
                   "has_scripts": bool(bundle.get("has_scripts")),
                   "skipped": 0})
    return result


# ----------------------------------------------------------------------------
# 内置示例技能（首次运行时落地，用户可改可删）
# ----------------------------------------------------------------------------
BUILTIN_SKILLS: Dict[str, str] = {
    "workspace-organizer": """---
name: workspace-organizer
description: 把工作区里散落的文件按类型归档到子目录，并生成归档报告
tools: [fs_list, fs_read, fs_info, fs_move, fs_mkdir]
version: 1.0.0
builtin: true
---
# 目标
把当前工作区根目录下散落的文件，按「文档 / 图片 / 表格 / 代码 / 压缩包 / 其它」
六类归档到对应子目录，并输出一份归档清单。

# 执行要求
1. 用 fs_list 查看工作区根目录（recursive=false），先弄清有哪些文件；
2. 只处理文件，不处理已存在的子目录，也不要动 .trash；
3. 目标目录名固定为：文档、图片、表格、代码、压缩包、其它；
4. 用 fs_move 逐个移动；移动前如果目标目录不存在，先 fs_mkdir；
5. 最后输出 Markdown 表格：原文件名 | 类型 | 去向；并给出总计数量。

# 约束
- 不删除任何文件；
- 不确定类型的文件放进「其它」；
- 遇到同名冲突时在文件名后加 -1、-2。
""",
    "repo-digest": """---
name: repo-digest
description: 扫描一个代码目录，产出结构、技术栈、关键模块与风险的摘要报告
tools: [fs_list, fs_glob, fs_read, fs_grep]
version: 1.0.0
builtin: true
---
# 目标
对指定目录做一次代码库体检，输出一份可直接给人看的中文摘要。

# 执行要求
1. fs_list 看顶层结构；fs_glob 找 README*、package.json、pyproject.toml、
   requirements.txt、*.sln、pom.xml 等项目标识文件；
2. 读 2-4 个最关键的入口文件（main / app / serve / index），不要通读全库；
3. 归纳：做了什么、技术栈、目录职责、启动方式、明显风险（硬编码密钥、
   缺失测试、异常吞噬等）；
4. 输出结构：一句话定位 / 技术栈 / 目录地图 / 关键流程 / 风险清单 / 建议。

# 约束
- 单文件读取不超过 200 行；
- 不修改任何文件；
- 没有证据的结论不要写。
""",
    "web-brief": """---
name: web-brief
description: 抓取一个网页并提炼要点，输出中文简报
tools: [web_fetch]
version: 1.0.0
builtin: true
---
# 目标
把给定 URL 的内容压缩成 200-400 字的中文简报。

# 执行要求
1. web_fetch 抓取正文（max_chars 12000）；
2. 提炼：主题、3-6 条关键信息、对读者有用的结论或行动项；
3. 输出格式：标题 / 三行摘要 / 要点列表 / 原文链接。

# 约束
- 只基于抓到的正文，不编造；
- 页面抓取失败时直接说明失败原因。
""",
}


def ensure_builtin_skills() -> None:
    """首次运行时落地内置技能（不覆盖用户已修改的同名技能）。"""
    ensure_dirs()
    for slug, content in BUILTIN_SKILLS.items():
        d = os.path.join(SKILLS_DIR, slug)
        if os.path.isdir(d):
            continue
        try:
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write(content)
        except OSError:
            continue
    # 安装覆盖会累积 `.backup-<ts>`；启动时收敛到最近 5 份，避免无限增长
    prune_backups()
