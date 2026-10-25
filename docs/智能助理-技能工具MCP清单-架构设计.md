# 智能助理 · 技能 / 工具 / MCP 清单中心（架构设计 + 任务分解）

> 版本：v1.0.8 增量 · 架构：高见远（Bob） · 输入：`docs/智能助理-技能工具MCP清单-PRD.md`
> 关联时序图：`docs/智能助理-技能工具MCP清单-时序图.mermaid`
> 关联任务分解：`docs/智能助理-技能工具MCP清单-任务分解.md`
> **本文件不改任何代码，只给结论与精确改法。**

---

## 0. 范围裁决（先看这一节，避免工程师做多）

### 0.1 P0 必做（5 人日，一个迭代内交付）

| # | 范围 | 对应任务 |
|---|---|---|
| 1 | 在线目录后端 `core/assistant/catalog.py`（MCP Registry + GitHub 技能仓库 + 三份缓存 + TTL + 代理 + 离线退化） | T01 |
| 2 | 在线安装（技能从 GitHub / MCP 从 Registry，原子写入 + 回滚 + 审计） | T01 / T02 |
| 3 | 底部三处手动添加（`[技能]` / `[工具]` / `[MCP]` 菜单 + `[+]` 菜单改造） | T04 |
| 4 | 清单中心（升级 `ManagerDialog`：技能 Tab 左列表 + 右详情、工具 Tab 带开关、MCP Tab 带在线安装入口） | T03 |
| 5 | 工具开关与下发裁剪（`tool_prefs` + `openai_schemas()` 上限 + **同源渲染**） | T02 |
| 6 | 按需求自动加载（profile + 关键词匹配 + `start.auto_loaded` + StepLine + 可关闭） | T02 |

### 0.2 明确允许工程师跳过（做多了反而是负担）

| PRD 条目 | 处理 | 理由 |
|---|---|---|
| P1-1 技能「更新」检测 | **跳过** | 依赖 `skill_sources` 的 commit 比对，先留字段不实现逻辑 |
| P1-2 MCP 目录分类筛选 | **跳过** | 纯前端分类，无验收价值 |
| P1-3 在线技能「安装全部选中」/ 整仓批量装 | **跳过** | 逐条安装已够用；批量装引入"部分成功"状态机 |
| P1-4 自定义 profile 编辑 | **跳过** | 3 个内置 profile 覆盖验收口径；只做"查看 + 启用/禁用" |
| P1-5 代理配置入口（设置页输入框） | **跳过** | P0 期只读 `config.json.catalog.proxy` + 环境变量；不做输入框 |
| P1-6 左栏「能力清单 (N)」入口 | **跳过** | 与 1.0.8「左栏只剩任务/空间」的结构决策冲突，见 A0 |
| P2-1 `catalog/featured.json` 精选清单 | **跳过** | 无数据来源，写了是空壳 |
| P2-3 Markdown 渲染预览 | **降级** | 右侧详情正文用 `QPlainTextEdit` 纯文本（只读），不引 Markdown 渲染 |
| P2-4 MCP 连通性预检 | **跳过** | 拖慢安装 |
| P2-5 全站扫 SKILL.md | **跳过** | 配额不可控 |
| 「刷新在线目录」进度条 | **降级** | 状态栏单行文字「已刷新（N 个技能 / M 个 MCP）」，不做进度条 |
| 右侧详情富文本 / 折叠动画 | **降级** | 纯文本 + `QSplitter` 静态比例 |

### 0.3 两处对 PRD 的否决（按 team-lead 裁决，必须照此实现）

**否决 1：底栏三按钮必须始终可见且带文字。**
PRD §5.1.4 建议 `[工具]`/`[MCP]` 用纯图标 `⚙`/`🔌`（26px）且「窗口宽 < 1100 自动隐藏」——**否决**。用户原话是「可以在对话框底部技能、工具、MCP 处手动添加」，隐藏入口直接违背需求。
定案：三个按钮固定中文文案 `技能` / `工具` / `MCP`，与现有 `工作区` / `⊘ 只读自动` 同款 `BarChip`，**不做宽度自适应隐藏**。宽度可行性由 §5 实测预算表证明。

**否决 2：范围砍到 5 人日。**
PRD 估 9 人日（附录切分 T01–T10）。定案见 §0.1 / §0.2：交付 T01–T05 五个任务包（≈5 人日），P1/P2 全砍。

---

## 1. 实施方式

### 1.1 核心技术难点

| # | 难点 | 事实依据（读过源码） | 方案 |
|---|---|---|---|
| D1 | **在线拉取不能污染现有职责** | `routers/assistant.py` 目前 609 行、全部是薄路由；`skills.py` 只做本地文件系统；`mcp.py` 只做协议 | 新建 `core/assistant/catalog.py` 统一收口（见 A1） |
| D2 | **模型会看到不存在的工具** | `agent.py:480` 用 `registry.list()` 全量渲染工具清单且截断 80 条；`agent.py:320` 用 `registry.openai_schemas()` 全量下发 | 两处必须同源：新增 `registry.select_schemas()`，`_build_messages` 用同一份结果（见 A3） |
| D3 | **安装临时目录会被当成技能** | `skills.py:92` `os.listdir(SKILLS_DIR)` + `:95` 只认含 `SKILL.md` 的目录 → `.installing-*` / `.backup-*` 若含 SKILL.md 就会被列出 | `list_skills()` 跳过 `.` 开头目录 + `_SLUG_RE` 已天然拒绝 `.` 开头（见 A8） |
| D4 | **代理** | 本机 `registry.modelcontextprotocol.io` 直连 000；`core/pool.py:182`、`routers/gateway.py:464` 全部 `trust_env=False` | catalog 的 httpx 一律 `trust_env=False` + 显式 `proxy=`（见 §4.3） |
| D5 | **冻结包无法用原生文件对话框** | `assistant_page.py:519 _pick_file()` / `:2381 _choose_workspace()` 已收口；回归测试 `test_assistant_page.py:1194` 扫源码禁三个静态调用 | 「从本地文件夹导入技能」**必须复用** `_pick_file`（`FileMode.Directory` 分支），不得调 `QFileDialog.getExistingDirectory`（见 §6.3） |
| D6 | **技能挂载污染历史** | `assistant_page.py:1768` 把 `用 xx 技能：` 拼进 message 正文，落库后正文被污染 | 改传 `mounted_skills` 字段 + 后端兼容识别（见 A4） |
| D7 | **`load_config()` 只认已知 key** | `store.py:101` `cfg.update({k: v for k, v in data.items() if k in cfg})` → 新字段**必须**进 `_default_config()`（`store.py:48`） | 见 §4.1 |
| D8 | **后端测试不能用 TestClient** | `tests/test_assistant.py:689` 注释与实现：「不依赖 TestClient（本机 starlette/httpx 版本不兼容），直接调用异步端点」 | 新测试沿用两种既有风格之一（直接 `asyncio.run(api.xxx())`，或 `httpx.AsyncClient(transport=ASGITransport(app=...))`） |

### 1.2 技术选型

| 依赖 | 用途 | 理由 |
|---|---|---|
| `httpx`（已在用） | catalog 的 HTTP 客户端 | `tools.py:536`、`mcp.py:26` 已引；不要引 `requests`（避免新依赖） |
| `PySide6`（已在用） | 客户端全部 UI | 不引 Web 栈（PRD §1.1 硬约束） |
| **不新增任何第三方包** | — | 缓存用 `json` + `os`；TTL 用 `time`；slug 复用 `skills._slugify` |
| LangGraph（可选，已在用） | 技能编排 | 不涉及本次改动 |

**架构模式**：后端「路由（薄）→ 服务（catalog / skills / mcp / tools / agent）」分层；客户端「页面（`AssistantPage`）→ 对话框（`ManagerDialog`）→ API 客户端（`AssistantApi`）」三层，与 1.0.8/1.0.9 既有结构一致，不引入新范式。

---

## 2. 模块图

```
┌──────────────────────────────── 客户端（PySide6） ────────────────────────────────┐
│  ui/pages/assistant_page.py  AssistantPage                                        │
│    ├─ _build_bottom_bar()          底栏：[+] [工作区] [⊘] [技能][工具][MCP] ◐ [模型▾][⚡][↑]
│    │    ├─ _show_attach_menu()     [+] 菜单：附件 / 清单中心 / 添加类 / 刷新 / 管理类
│    │    ├─ _show_skill_menu()      [技能]：挂载 + 三条安装入口 + 自动加载开关
│    │    ├─ _show_tool_menu()   ★新  [工具]：启用开关 + 下发计数 + 去清单中心
│    │    └─ _show_mcp_menu()    ★新  [MCP]：服务器连接态 + 在线安装 + 手动添加
│    ├─ _pick_file() / _choose_workspace()   文件/目录选择唯一入口（DontUseNativeDialog）
│    ├─ _send_with_session()         ★改：payload 增 mounted_skills，不再拼正文
│    ├─ _on_event()                  ★改：读 start.auto_loaded → _add_step_with_action()
│    ├─ _add_step() / _add_step_with_action()  ★新（后者带按钮，不改 StepLine）
│    ├─ open_manager(key)            → ManagerDialog
│    ├─ _build_skill_card_tab()  ★新 技能 Tab：QSplitter(列表 62% + 详情 38%)
│    ├─ _build_tools_tab()       ★改 工具 Tab：QCheckBox 开关 + 下发计数
│    └─ _build_mcp_tab()         ★改 MCP Tab：加「从在线清单安装」入口
│  ui/widgets/assistant_cards.py     StepLine（不动） / ChatBubble / ToolCard / ApprovalCard
│  core/assistant_client.py          AssistantApi（★增 catalog_* / tool_prefs / profiles 方法）
└──────────────────────────────────────────────────────────────────────────────────┘
                                        │ HTTP  /api/v1/assistant/*
┌───────────────────────────────────────▼──────────────────────────────────────────┐
│  routers/assistant.py（薄路由，统一 ok()/fail()）                                 │
│    ├─ /catalog/mcp              → catalog.list_mcp()                              │
│    ├─ /catalog/skills           → catalog.list_repos()                            │
│    ├─ /catalog/skills/{o}/{r}   → catalog.list_repo_skills()                      │
│    ├─ /catalog/skills/install   → catalog.fetch_skill_bundle() + skills.install_from_github()
│    ├─ /catalog/mcp/install      → catalog.build_mcp_config()  + mcp.add_server()
│    ├─ /catalog/refresh          → catalog.refresh_all()
│    ├─ /catalog/skills/from_dir  → skills.install_from_dir()                       │
│    ├─ /catalog/skills/from_url  → catalog.url_to_raw() + skills.install_from_url()
│    ├─ /tools/prefs  GET/PUT     → tools_prefs（store）+ registry
│    └─ /profiles     GET/PUT     → store.profiles
├──────────────────────────────────────────────────────────────────────────────────┤
│  core/assistant/                                                                  │
│    catalog.py   ★新  三源客户端 + 缓存 + TTL + 代理 + 归一化 + 免费分级            │
│    skills.py    ★改  list_skills 跳过 '.'、install_from_github/dir/url、prune_backups
│    mcp.py       ★改  add_server 支持 risk/来源，slugify id                        │
│    tools.py     ★改  set_filter(disabled, allow, max_tools) / select_schemas() / active()
│    agent.py     ★改  start_turn 加 profile 匹配 + 同源裁剪 + start.auto_loaded
│    store.py     ★改  _default_config 增 tool_prefs / auto_load / profiles / catalog / skill_sources
│    profiles.py  ★新  match_profiles()（纯关键词，无网络）                         │
└──────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 新增 / 修改文件清单（函数级）

### 3.1 新增文件

#### `core/assistant/catalog.py`（≈420 行）
```python
"""在线能力目录（MCP Registry + GitHub 技能仓库）。只负责「发现 + 缓存 + 下载字节」，
不负责「安装」——安装动作由 skills.install_from_* / mcp.add_server 完成（见 A1）。"""
PROXY_DEFAULT = "http://127.0.0.1:7897"
TTL_MCP       = 24 * 3600
TTL_GH_REPOS  = 24 * 3600
TTL_GH_SKILLS = 7 * 24 * 3600

class CatalogError(RuntimeError):
    def __init__(self, code: int, message: str, data=None)   # code 见 §7.3

# ---- 基础设施 ----
def catalog_dir() -> str                       # <ASSISTANT_DIR>/catalog（ensure_dirs 内建）
def _proxy(cfg) -> str                         # cfg.catalog.proxy or env HTTP(S)_PROXY or PROXY_DEFAULT
def _client(cfg, timeout=5.0) -> httpx.Client   # trust_env=False + proxy=_proxy(cfg)  ★D4
def _cache_read(name) -> dict                  # 三份 json，读失败返回 {}（绝不抛）
def _cache_write(name, payload) -> None        # 原子写（.tmp → os.replace），失败静默
def _fresh(cache, ttl) -> bool                 # fetched_at 是否在 TTL 内

# ---- MCP Registry ----
def list_mcp(cfg, q="", cursor="", force=False, limit=50) -> dict
    # 返回 {fetched_at, stale, error, cursor, items[]}
def normalize_mcp_server(raw) -> dict
    # {registry_name,title,description,version,transport,free_class,suggest,raw}
def build_mcp_config(item, server_id, name="", env=None, risk="high") -> dict
    # suggest → mcp_servers 配置（含 id/name/transport/command/args/env/url/risk/enabled）
def free_class_of(raw) -> str                 # zero_config | need_key | need_register | unknown

# ---- GitHub 技能 ----
def list_repos(cfg, q="", force=False, per_page=60) -> dict
def list_repo_skills(cfg, owner, repo, ref="", force=False) -> dict
    # tree?recursive=1 → 找 **/SKILL.md → 每个取 frontmatter（name/description）
def fetch_skill_bundle(cfg, owner, repo, ref, dirpath, max_files=20, max_bytes=2*1024*1024) -> dict
    # {slug, files: {rel: text}, bytes, has_scripts, commit}
    # 只取文本类 .md/.txt/.json/.yaml/.yml/.py；超限只取 SKILL.md + skipped 计数

# ---- URL 归一 ----
def url_to_raw(url) -> dict                   # {raw_url, owner, repo, ref, path}；非法抛 CatalogError(2026)
def refresh_all(cfg, force=True) -> dict      # 三源并行（ThreadPoolExecutor(3)），返回 {ok, counts, errors[]}
```

#### `core/assistant/profiles.py`（≈90 行）
```python
"""按需求自动加载的 profile 匹配（纯关键词，无网络、无 IO）。"""
BUILTIN_PROFILES: List[dict]                  # 3 个（file-organize / code-read / web-research）
def ensure_builtin_profiles() -> None         # 首次落 config.json.profiles（不覆盖用户改动）
def match_profiles(text, profiles, available_skills) -> Optional[dict]
    # 中文子串、大小写不敏感；多命中取「命中关键词数最多」再取 priority 高者；
    # 过滤掉不存在的 slug；全部缺失 → None（走 L3 降级）
```

### 3.2 修改文件（精确到函数 / 行）

| 文件 | 位置 | 改法 |
|---|---|---|
| `core/assistant/store.py` | `_default_config()` :48 | 增 5 个 key：`tool_prefs` / `auto_load` / `profiles` / `catalog` / `skill_sources`（**必做**，否则 `load_config` 读不到，D7） |
| | `ensure_dirs()` :71 | 目录元组追加 `CATALOG_DIR`；新增模块常量 `CATALOG_DIR = os.path.join(ASSISTANT_DIR, "catalog")` |
| `core/assistant/skills.py` | `list_skills()` :89–92 | 循环体首行加 `if slug.startswith("."): continue`（A8） |
| | 新增 | `install_from_dir(src, overwrite=False)` / `install_from_url(files, slug, overwrite)` / `install_from_github(...)` 三个函数统一走 `_atomic_install()` |
| | 新增 | `_atomic_install(slug, files, overwrite)`：临时 `.installing-<uuid>` → 校验 → 备份 `.backup-<ts>` → `os.replace` → 失败回滚 |
| | 新增 | `prune_backups(keep=5)`，在 `ensure_builtin_skills()` 尾部调用一次 |
| | `delete_skill()` :180 | 结论：**无需改**。`_SLUG_RE`（:37 `^[a-zA-Z0-9]…`）已拒绝 `.` 开头，`.backup-*` 删不掉，正是我们要的 |
| | `export_zip()` :245 | 结论：**无需改**。走 `load_skill(slug)`，而 `load_skill` 先 `_SLUG_RE.match`（:128）|
| `core/assistant/mcp.py` | `add_server()` :294 | 允许 `risk` 缺省 high（在线安装传入）；`id` 由 catalog `build_mcp_config` 用 `_slugify` 生成并保证冲突加 `-2` |
| `core/assistant/tools.py` | `ToolRegistry.__init__` :85 | 增 `self._filter: Dict[str, Any] = {"disabled": set(), "allow": None, "max_tools": 24}` |
| | 新增 `set_filter(disabled, allow=None, max_tools=24)` | 只存值并重算缓存，不做 IO |
| | 新增 `active()` | 返回「未被 disabled 且（allow 为空 或 在 allow 内）」的 `Tool` 列表，**按 §6.2 优先级排序** |
| | 新增 `select_schemas(disabled=None, allow=None, max_tools=None)` | `active()` 裁到 `max_tools` 后转 schema；**A3 的唯一出口** |
| | `list()` :113 | `to_dict()` 增 `enabled` 字段（UI 直接读，不算二次） |
| | `openai_schemas()` :122 | 改为 `return self.select_schemas()`（保持旧签名可用，内部走过滤）★注意：orchestrator:159 也调它，改了会影响技能内下发——**这是期望行为**（技能也应遵守禁用） |
| `core/assistant/agent.py` | `start_turn()` :203 | ① 入参加 `mounted_skills: Optional[List[str]] = None`（A4）；② 第 212 行前后插入 profile 匹配与工具白名单收紧；③ `emit({"type":"start",...})` :235 增 `auto_loaded` |
| | `_build_messages()` :477 | 改为接收 `schemas: List[Dict]`，文字清单从同一份 schemas 渲染（A3） |
| | `_ctx()` :430 | 无改（`exec_tool` 已足够） |
| `routers/assistant.py` | 文件尾 | 追加 §7.2 的 9 个端点 |
| | `chat()` :178 | `body.get("mounted_skills")` 透传到 `ass.start_turn(...)`；两条路径（stream / 非 stream）都要传 |
| `client/core/assistant_client.py` | `AssistantApi` | 增 `catalog_mcp / catalog_repos / catalog_repo_skills / catalog_install_skill / catalog_install_mcp / catalog_refresh / catalog_from_dir / catalog_from_url / tool_prefs / save_tool_prefs / profiles / save_profiles` |
| `client/ui/pages/assistant_page.py` | `_build_bottom_bar()` :~400 | 在 `_skill_btn` 后插 `_tool_btn` / `_mcp_btn`（`BarChip`，中文文案） |
| | `_build_attach_menu()` :568 | 按 §6.1 改造为 4 段；保留 `添加附件…` 为第一项 |
| | `_build_skill_menu()` :2461 | 空态由「禁用文案」改为 **3 条可点安装入口**（PRD §5.1.2 核心） |
| | 新增 `_build_tool_menu()` / `_build_mcp_menu()` / `_show_tool_menu()` / `_show_mcp_menu()` | 见 §6.1 |
| | 新增 `_import_skill_from_dir()` | 复用 `_pick_file(..., FileMode.Directory)`；**禁止** `getExistingDirectory`（D5） |
| | 新增 `_install_skill_from_url()` | `QInputDialog.getText` 收 URL → `POST /catalog/skills/from_url` |
| | `_send_with_session()` :1757 | payload 增 `"mounted_skills": list(self._skill_slugs)`；`message` 不再拼前缀（A4） |
| | `_on_event()` :1854 | 新增分支 `elif kind == "start":` 读 `ev.get("auto_loaded")` |
| | 新增 `_add_step_with_action(text, action_label, on_action)` | 纯文本 `StepLine` + 行尾一个 `MiniBtn`；**不改造 `StepLine`**（A6） |
| | `_build_skills_tab()` :1249 | 改为 `QSplitter`：左 `QListWidget`（两行文本）+ 工具条（搜索/来源/排序/刷新），右详情 `QPlainTextEdit`（只读）+ 按钮行 |
| | `_build_tools_tab()` :1283 | 改为 `QListWidget` + `setItemWidget` 行内 `QCheckBox`；顶部常驻 `当前下发 N / 上限 M` |
| | `_build_mcp_tab()` :1293 | 按钮行前插「从在线清单安装 MCP…」 |
| | `_fill_tools()` :1537 | 填 `QCheckBox` 状态（读 `tool["enabled"]`），禁用态标灰 + tooltip「超出上限未下发」 |
| `client/ui/widgets/assistant_cards.py` | `StepLine` :156 | **不改**（A6 结论） |

---

## 4. 数据模型

### 4.1 `config.json` 新增字段（进 `_default_config()`）

```jsonc
{
  "tool_prefs": {"disabled": [], "max_tools": 24},
  "auto_load": true,
  "profiles": [ /* 3 个内置，见 profiles.BUILTIN_PROFILES */ ],
  "catalog": {
    "proxy": "",                      // 空 = 读 env HTTP(S)_PROXY，再空 → PROXY_DEFAULT
    "ttl_hours": {"mcp": 24, "gh_repos": 24, "gh_skills": 168},
    "allow_nonfree": false
  },
  "skill_sources": {}                 // slug -> {kind, repo, ref, dir, commit, installed_at, has_scripts}
}
```

> **强制**：`store.py:101` 只回填 `_default_config()` 里存在的 key。不加则整个 feature 静默失效。

### 4.2 缓存文件（`ASSISTANT_DIR/catalog/`）

| 文件 | 结构 | TTL |
|---|---|---|
| `mcp_servers.json` | `{fetched_at, cursor, items:[{registry_name,title,description,version,transport,free_class,suggest,raw}]}` | 24h |
| `gh_repos.json` | `{fetched_at, total_count, items:[{full_name,stars,default_branch,description,topics}]}` | 24h |
| `gh_skills.json` | `{fetched_at, repos:{"owner/repo@ref":{commit, items:[{slug,dir,name,description,files,has_scripts}]}}}` | 7d |

### 4.3 类图

见 `docs/智能助理-技能工具MCP清单-时序图.mermaid` 同目录下的类图段落（`CatalogClient` / `SkillInstaller` / `ToolFilter` / `ProfileMatcher` 四组）。要点：

- `CatalogClient` 持有 `httpx.Client(trust_env=False, proxy=...)`，**唯一**出网点。
- `SkillInstaller` 不做网络，只吃 `{rel: text}`，因此可被单测直接驱动。
- `ToolFilter` 是 `ToolRegistry` 的内部状态，`select_schemas()` 是唯一出口。

---

## 5. 底栏宽度预算（1440 物理 / 200% 缩放 → 720 逻辑宽）

### 5.1 前提

主窗口逻辑宽 720 → 左栏 `NavPanel` 约 200（`NAV_ROW` 相关常量见 `assistant_page.py:73-77`，左栏宽 200 在 `:72` 注释中明确）→ **右侧对话列 ≈ 520 逻辑 px**。
底栏左右各 8px 边距 + `QHBoxLayout.setSpacing` ≈ 4（暂按 4 估，工程可按 QSS 实测微调 2px 级），**可用宽 ≈ 520 − 16 = 504 px**。

### 5.2 逐控件估值（单位：逻辑 px）

| 控件 | 文案 | 字宽估算 | 左右内边距 | 合计 | 说明 |
|---|---|---|---|---|---|
| `_attach_btn` | `+` | 8 | 18（固定 26 见 `:403`） | **26** | `setFixedSize(26,26)` |
| `_ws_btn` | 目录名（≤ 12 字，`:1423`）→ 实测常见 6~7 字 | 7×13 = 91 | 16 | **107** | 降级方案见 5.4 |
| `_policy_btn` | `⊘ 只读自动` | 1×14 + 4×13 = 66 | 16 | **82** | `POLICY_ICON` + 4 个汉字 |
| `_skill_btn` | `技能` | 2×13 = 26 | 16 | **42** | ★新增 |
| `_tool_btn` | `工具` | 2×13 = 26 | 16 | **42** | ★新增 |
| `_mcp_btn` | `MCP` | 3×9 = 27 | 16 | **43** | ★新增（拉丁字母更窄） |
| `_skill_chip_cont` | 空（0 挂载） | — | — | **0** | 挂载后**最宽 5 个 chip，每 chip ≈ 60+，会溢出 → 见 5.4 |
| `addStretch(1)` | — | — | — | **0** | 弹性，可被压到 0 |
| `_busy_lbl` | 空 | — | — | **16** | `setFixedWidth(16)`（`:445`） |
| `_model_btn` | `模型` 或模型名（`:150`） | 2×13 = 26 | 16 | **42** | 实测会显示模型名，按 90 估（见 5.3 保守列） |
| `_stop_btn` | `■` | — | — | **30** | 固定 30，仅忙碌可见 |
| `_guide_btn` | `⚡` | — | — | **26** | 固定 26，仅忙碌可见 |
| `_send_btn` | `↑` | — | — | **30** | 固定 30 |
| 控件间距 | 12 个控件 → 11 个 gap × 4 | — | — | **44** | 估 |
| 左右边距 | — | — | — | **16** | 8+8 |

### 5.3 结论：**默认样式放不下，必须按 5.4 压缩；压缩后放得下**

求和口径：仅计**该状态下可见**的控件（`_stop_btn` / `_guide_btn` 空闲时 `setVisible(False)`，不计）。

| 场景 | 明细求和 | 合计 | 可用宽 | 结论 |
|---|---|---|---|---|
| **空闲（乐观 `_model_btn` 42）** | 26+107+82+42+42+43+0+0+16+42+30+44+16 | **490** | 504 | ✅ 放得下（余 14） |
| **空闲（保守 `_model_btn` 90）** | 26+107+82+42+42+43+0+0+16+90+30+44+16 | **538** | 504 | ❌ 超 34 |
| **忙碌（保守 90，含 `■`30 + `⚡`26）** | 538+30+26 | **594** | 504 | ❌ 超 90 |
| **挂载 2 技能 chip（每 chip ≈ 62，含间距）** | 538+124 | **662** | 504 | ❌ 超 158 |

> 结论：**7 个控件默认样式在 720 逻辑宽下必然溢出**。这不是否决理由（入口必须可见），而是要求按 §5.4 的压缩规则实现 —— **压的是文案与间距，不是隐藏按钮**。
> 注：`_busy_lbl` 16px 与 `addStretch` 0 已计入；间距按「可见控件数 − 1」计（空闲 11 控件 → 10 gap × 4 ≈ 40，取 44 保守）。

### 5.4 压缩方案（按优先级依次生效，**不隐藏按钮**）

**空闲态**：从保守基线 **538** 起压：

| 步 | 措施 | 变化 | 结果 |
|---|---|---|---|
| ① | `工作区` 文案压到 **目录名首字 + `…`**（≤ 4 字；把 `:1423` 的 12 字规则改为 4 字，完整路径进 tooltip） | `_ws_btn` 107 → **58**（4×13+16） | 538 − 49 = **489** ✅ ≤ 504 |
| ② | `_model_btn` 只显**短名**（`:1462` 里 `_ellipsis(name, 8)`），tooltip 给全名 | 90 → **66** | 489 − 24 = **465** |
| ③ | 底栏 `setSpacing(2)`（4→2）+ 边距 8→6 | 44→22（释放 22）；16→12（释放 4） | 465 − 26 = **439** |
| ④ | 挂载 ≥ 3 个 chip 时，`_skill_chip_cont` **折行到输入框上方**（从 `bar` 移入 `lay`，`_refresh_skill_chips` 判断 `len(slugs) >= 3` 时 reparent） | chip 区不占底栏 | 底栏恒 ≤ **439** |

> **下限**：① ② ③ 三条落地后，空闲态底栏只需 **439px**，在 504px 可用宽下留出 **65px** 余量给 `_busy_lbl`、异常字号、QSS padding 实测偏差。

**忙碌态**（`■`30 + `⚡`26 = +56，且 `addStretch` 被压到 0）：439 + 56 = **495** ≤ 504 ✅ —— **本身就放得下，⑤ 仅为保险**。

| 步 | 措施（仅忙碌态生效，保险） | 变化 |
|---|---|---|
| ⑤（可选） | 忙碌时 `_ws_btn` → `目录`、`_policy_btn` → `⊘`（tooltip 保留完整语义） | 再释放 `_ws_btn` 58→42（16）、`_policy_btn` 82→30（52）→ **495 − 68 = 427** |

**最终结论**：在 720 逻辑宽、右侧对话列 **504px** 可用宽下，**三按钮全文字方案通过 ①②③④ 稳定放下（空闲 439 / 忙碌 495），完全不需要任何宽度自适应隐藏逻辑**。①②③ 作为硬性样式规则写入 `_build_bottom_bar` / `_refresh_ws_btn` / `_refresh_model_btn`；④ 为条件分支；⑤ 为紧急保险，不实现也可接受。

> **工程师落地要求**：把 §5.3 的「空闲保守基线 538 / 忙碌 594」作为**回归门禁**——若实现后实测 `bar.sizeHint().width() > 504`（720×680 下），说明 ①②③ 未全部生效，T04 验收即判失败（见任务分解 T04 验收第 2 条）。

---

## 6. 关键交互的落地细节

### 6.1 四个入口菜单

**`[+]`（`_build_attach_menu` :568 改造）**
```
添加附件…
─────
能力清单（技能 / 工具 / MCP）…      → open_manager("skills")
─────
从清单添加技能…                    → open_manager("skills") + 聚焦搜索框
从清单添加 MCP…                    → open_manager("mcp")
─────
从本地文件夹导入技能…              → _import_skill_from_dir()   ★复用 _pick_file
导入技能 zip…                      → 原 _import_skill
粘贴 URL 安装技能…                 → _install_skill_from_url()
粘贴 JSON 添加 MCP…                → 原 McpDialog（预填 JSON 输入）
─────
刷新在线目录                       → POST /catalog/refresh → 状态栏文字
─────
工具 / MCP / 审计                   → 原跳转（保留）
```
> 回归测试 `test_assistant_page.py:407 test_attach_plus_menu_has_five_entries` 断言 `texts[1:] == ["技能","工具","MCP","审计"]` 且 `seps == 1` → **该测试必须同步更新**（列入 T05 验收）。

**`[技能]`（`_build_skill_menu` :2461 改造）** — 空态改为 3 条可点安装入口（PRD §5.1.2 核心需求），并加：
```
自动加载：开 / 关                     → PUT /settings {"auto_load": false}
```

**`[工具]`（新增 `_build_tool_menu`）**
```
当前下发 18 / 上限 24                 （禁用行，实时计算）
─────
☑ fs_list    只读   内置              （逐工具 QAction，checkable）
☐ shell_run  高危   内置              → 关闭需二次确认
☑ 文件系统 · 12 个工具                （MCP 按 server 折叠，整组开关）
─────
工具清单与开关…                       → open_manager("tools")
⟳ 重连已启用 MCP                      → 逐台 connect_mcp，失败逐条报错
```
> `[工具]` 不区分「在线获取」——工具是代码资产（PRD §9.3，我同意）。

**`[MCP]`（新增 `_build_mcp_menu`）**
```
已配置 2 台 · 已连接 1 台              （禁用行）
─────
● 文件系统  本地 · 12 工具  [断开]
○ github    远程 · 未连接   [连接]
─────
从在线清单安装 MCP…                   → open_manager("mcp")
＋ 手动添加（stdio / http）…          → 原 McpDialog
＋ 粘贴 JSON 添加…
─────
管理 MCP 服务器…                      → open_manager("mcp")
```

### 6.2 工具优先级排序（`active()` 的实现顺序）

```
1. 内置 low       : fs_list, fs_read, fs_glob, fs_grep, fs_info, skill_list
2. 内置 medium    : web_fetch
3. 技能类         : skill_run
4. 内置 medium 写 : fs_write, fs_edit, fs_mkdir, fs_move
5. 内置 high      : fs_delete, shell_run
6. MCP 工具       : 按 mcp_servers 配置顺序，server 内按 name
```
被裁掉的工具在 `[工具]` Tab 标灰 + tooltip「超出上限未下发」。`max_tools` 允许 8~64，小于内置数量时**不报错**，按上序裁。

### 6.3 「从本地文件夹导入技能」的对话框（D5 硬约束）

```python
def _import_skill_from_dir(self) -> None:
    # 复用 _pick_file，追加 Directory 模式分支（不改 _pick_file 的默认行为）
    src = self._pick_file("选择技能文件夹（需含 SKILL.md）", start,
                          "所有文件 (*.*)", directory=True)
```
`_pick_file`（:519）新增关键字参数 `directory: bool = False`，为真时 `setFileMode(QFileDialog.FileMode.Directory)` + `ShowDirsOnly`，标签沿用现有中文映射并新增 `FileName → "目录："`、`Accept → "选择此文件夹"`。
**禁止**：`QFileDialog.getExistingDirectory(...)`（`test_assistant_page.py:1194` 会扫源码失败）。`_choose_workspace` 的 `QFileDialog(...)` 实例化写法可以照抄，但**不允许**直接复制其 20 行——统一走 `_pick_file` 以免两份中文标签漂移。

---

## 7. 架构问题逐条结论

### A1 · 在线拉取收口：**新建 `core/assistant/catalog.py`，职责只到「发现 + 缓存 + 下载字节」**

**结论**：新建。职责边界如下表（安装动作**不进** catalog）：

| 动作 | 归属 | 理由 |
|---|---|---|
| HTTP 请求 / 重试 / 超时 / 代理 | `catalog.py` | 唯一出网点，便于统一 `trust_env=False`（D4）|
| 缓存读写 + TTL 判定 | `catalog.py` | 三份 json 同构，放一起 |
| Registry → `mcp_servers` 配置映射 | `catalog.py:nomalize_mcp_server/build_mcp_config` | 纯函数，可单测 |
| tree → 技能列表 / frontmatter 解析 | `catalog.py`（复用 `skills.parse_skill_md`） | 复用已有解析器，不重复实现 |
| URL → raw 归一 | `catalog.py:url_to_raw` | 纯字符串处理 |
| **落盘（SKILLS_DIR 写入 / 原子 / 回滚 / 备份）** | **`skills.py:install_from_*`** | 目录所有权在 skills，catalog 碰 SKILLS_DIR 就会绕过 `_SLUG_RE` 与 `ensure_dirs` |
| **MCP 配置持久化 + 连接** | **`mcp.py:add_server/connect`** | 已有 `save_servers` 原子写 |
| 审计 | 路由层调 `store.audit`（安装成功后） | 与现状一致（`routers/assistant.py:528`）|

**关键约束**：`fetch_skill_bundle()` 返回**内存里的 `{rel: text}`**，不落盘。这样 `_atomic_install` 的回滚只需处理一个临时目录，且 `install_from_url` 与 `install_from_github` 共享同一条落盘路径。

### A2 · 工具裁剪在哪一层：**`ToolRegistry` 内部存 filter，`agent.py` 传入**

采纳 PRD 倾向，但**不**做成「registry 自己读 config」——registry 是纯内存对象（`:85` 只接 `workspace`），让它读盘会破坏 `tests/test_assistant.py:66` 一类直接构造 `ToolRegistry(str(dir))` 的用例。定案：

```python
# tools.py
def set_filter(self, disabled, allow=None, max_tools=24) -> None   # 只存值
def active(self) -> List[Tool]                                     # 过滤 + 排序
def select_schemas(self, disabled=None, allow=None, max_tools=None) -> List[dict]
    # 参数全 None 时用 self._filter；显式传入时用传入值（供 profile 收紧用）
```
`agent.start_turn` 在匹配到 profile 后调用 `registry.select_schemas(disabled=..., allow=profile_tools, max_tools=...)` 得到**一份** `schemas`，然后：① `provider.chat(tools=schemas)`；② `_build_messages(schemas=...)` 渲染文字清单。**同一个列表对象，两处使用。**

### A3 · 同源：**必须同源，给出精确改法**

**现状（两条事实）**
- `agent.py:480`：`for t in registry.list()` 生成文字清单，`tools_desc[:80]` 截断（`:487`）。
- `agent.py:320`：`tools=registry.openai_schemas()` 全量下发。

裁剪后若只改一处，模型会看到「系统提示里有 fs_write，但 function schema 里没有」→ 本地小模型会照提示去调 → `registry.call` 返回「未知工具」→ 白烧一轮 + 用户看到假失败。

**精确改法**
```python
# agent.py  start_turn 内，_build_messages 之前
schemas = registry.select_schemas(
    disabled=set((cfg.get("tool_prefs") or {}).get("disabled") or []),
    allow=profile_tools,                       # None = 不额外限制
    max_tools=int((cfg.get("tool_prefs") or {}).get("max_tools") or 24))
...
emit({"type": "start", ..., "auto_loaded": auto_loaded})
messages = self._build_messages(session, registry, cfg, schemas)   # ← 传 schemas
...
reply = provider.chat(messages, tools=schemas, ...)                # ← 同一个 schemas
```
```python
# agent.py:477 _build_messages 签名改为
def _build_messages(self, session, registry, cfg, schemas=None) -> List[dict]:
    if schemas is None:                       # 向后兼容（其他调用点）
        schemas = registry.select_schemas()
    tools_desc = []
    risk_by_name = {t["name"]: t["risk"] for t in registry.list()}
    for s in schemas:
        fn = s.get("function") or {}
        name = fn.get("name") or ""
        risk = {"low": "只读", "medium": "写入", "high": "高危"}.get(
            risk_by_name.get(name, "low"), "low")
        tools_desc.append(f"- {name}（{risk}）：{fn.get('description') or ''}")
    ... tool_list="\n".join(tools_desc) or "（暂无工具）"      # 不再 [:80]
```
**注意两点**
1. `registry.list()`（`tools.py:113`）返回的是 `Tool.to_dict()`：`{name, description, risk, source, parameters, meta}` —— **没有** `function` 键，所以文案渲染必须走 `select_schemas()` 的 `function.name/description`，risk 从 `list()` 建索引查（如上）。
2. `[:80]` 截断可以**去掉**：`max_tools` 上限 64 < 80，天然收敛。

### A4 · 技能挂载传参：**改传 `mounted_skills`，保留旧路径兼容**

**问题**：`assistant_page.py:1768` 拼 `用 {slugs} 技能：{text}` 进 message，`agent.py:229` 原样落库 → 历史记录里每条消息都带前缀，用户复制/回看时看到污染。

**后端改动（`routers/assistant.py:chat`）**
```python
mounted = body.get("mounted_skills")
mounted = [str(s) for s in mounted] if isinstance(mounted, list) else None
# 两条路径都透传
ass.start_turn(session_id, first_text, provider_id, model, emit,
               mounted_skills=mounted)
```

**`agent.start_turn` 签名**
```python
def start_turn(self, session_id, text, provider_id="", model="",
               emit=None, mounted_skills: Optional[List[str]] = None) -> dict:
```
> `mounted_skills` **放在最后且带默认值**——现有调用点 `tests/test_assistant.py`、`routers/assistant.py:263/291` 全部按位置传前 5 个参数，加尾参不破坏任何调用。

**向后兼容（三层）**
1. 老客户端不带 `mounted_skills` → `None` → 行为与今天完全一致。
2. 老客户端正文里有 `用 xx 技能：` 前缀 → **保留**（不解析、不剥离），因为老客户端的语义确实依赖正文。PRD A4 建议「识别前缀并忽略」——**我不采纳**：剥离正文会让老客户端的意图丢失（模型看不到技能指定），且需要在 `agent` 里写脆弱的正则。成本高、收益仅"历史更干净"。
3. 新客户端：`message` 传**原始文本**，`mounted_skills` 传列表。落库的是原始文本，`_build_messages` 组装时把挂载信息写进 **system** 段：
```python
if mounted:
    system += "\n## 本轮指定技能\n用户明确指定使用技能：" + "、".join(mounted) + \
              "。请优先调用 skill_run 执行它们。\n"
```
这样历史干净、意图不丢。

**回归测试影响**：`test_assistant_page.py:392 test_multi_skill_prefix_injected_in_payload` 断言 `payload["message"] == "用 repo-digest、code-review 技能：看看这个项目"` → **必须改为**断言 `payload["message"] == "看看这个项目"` 且 `payload["mounted_skills"] == ["repo-digest","code-review"]`（列入 T05 验收）。

### A5 · MCP 在线安装的 id：**`_slugify(name)` + 冲突 `-2`**

`com.pulsemcp/remote-filesystem` → `skills._slugify`（`:262`，`re.sub(r"[^a-zA-Z0-9._-]+", "-")`）→ `com.pulsemcp-remote-filesystem`（`.` 被保留，`/` 变 `-`）。
`-` 与 `.` 在 `mcp.py:_register_tools`（`:346` `prefix = f"mcp__{server_id}__"`）里安全。冲突时在 `build_mcp_config` 内比对 `MCPManager.servers()` 的 id 集合，追加 `-2`、`-3`。

### A6 · StepLine 复用：**不改 `StepLine`，新增 `_add_step_with_action()`**

**现状**（`assistant_cards.py:156`）：`StepLine(text, parent, error)` 只有一个居中 `QLabel`，**没有按钮位**；页面侧 `_add_step(text, error=False)`（`:2162`）只有两参，5 处调用（`:1831`、`:1859`、`:1922`、`:1956`、`:1986` 等）。

**结论**：改造 `StepLine` 会波及这 5 处调用签名与 QSS（`#StepLine` 居中，加按钮会破坏居中）。**最小改法**：

```python
# assistant_page.py 新增（不动 StepLine）
def _add_step_with_action(self, text: str, action_label: str,
                          on_action, error: bool = False) -> None:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 2, 0, 2)
    lay.addStretch(1)
    lay.addWidget(StepLine(text, error=error))       # 既有组件，原样复用
    btn = QPushButton(action_label)
    btn.setObjectName("MiniBtn")                     # 复用已有 QSS
    btn.setFixedHeight(20)
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(on_action)
    lay.addWidget(btn)
    lay.addStretch(1)
    self._append_widget(row)
    self._scroll_bottom()
```
`start.auto_loaded` 分支调用：
```python
elif kind == "start":
    al = ev.get("auto_loaded") or {}
    if al and not self._auto_tip_shown(self._session_id, al.get("profile")):
        self._add_step_with_action(
            f"已自动加载：{al.get('name')} · 技能 {'、'.join(al.get('skills') or [])} "
            f"· 工具 {al.get('tools')} 个",
            "关闭自动加载", self._disable_auto_load)
```
`/review/:` 「同一任务同一 profile 只提示一次」用 `self._auto_tips: set[tuple[str,str]]` 记录，`_clear_chat()` 里清空（`:2169` 已有清理块，追加一行）。

### A7 · 680px + 200% 可用性：**结构可行，但必须实测**

- 清单中心 `ManagerDialog` 现 `setMinimumSize(760, 560)`（`:2745`）—— 逻辑 720 宽**放不下 760**！必须改为 `setMinimumSize(700, 520)` + 打开前 `resize(主窗口宽×0.86, 高×0.84)` 且封顶到屏幕可用区。
- 技能 Tab 左列表用**两行纯文本** `QListWidget`（不用自绘 widget，PRD §5.2 结论一致）：行高 ≈ 13×2+8 = 34，加间距 ≈ 38 → 520px 高（减 Tab 32 + 工具条 36 + 状态行 24 + 边距 24 = 404 列表区）→ **≥ 10 行**，远超 PRD 的「≥5 张」。
- 详情面板 38%：`QPlainTextEdit` 只读纯文本，`setWordWrapMode(WidgetWidth)`。
- **必须加一个 UI 测试**：`test_manager_dialog_fits_720_logical_width`（断言 `minimumWidth() <= 700` 且列表区 `viewport()` 高度 ≥ 5 行）。列入 T05 验收。

### A8 · 扫目录：**`list_skills()` 跳过 `.` 开头目录**

**改法**（`skills.py:89-92`）：
```python
for slug in sorted(os.listdir(SKILLS_DIR)):
    if slug.startswith("."):        # .installing-<uuid> / .backup-<ts> / .trash
        continue
    d = os.path.join(SKILLS_DIR, slug)
```

**对 `delete_skill` / `export_zip` 的影响 —— 经核对：零影响，无需改**
- `delete_skill`（`:180`）：首行 `if not _SLUG_RE.match(slug): return False`；`_SLUG_RE = ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$`（`:37`）**首字符必须是字母数字**，`.backup-abc` 直接被拒 → 删不掉备份。✅
- `export_zip`（`:245`）：只走 `load_skill(slug)`，而 `load_skill`（`:117`）对每个候选先 `_SLUG_RE.match`（`:128`）失败即 `continue` → 导不出备份目录。✅
- `load_skill` 的 name→slug 回退走 `list_skills()`（`:124`），改后也不会把备份目录当候选。✅

**但要注意一个新问题**：`import_zip`（`:194`）与 `save_skill`（`:159`）都用 `os.path.join(SKILLS_DIR, slug)`，slug 已过 `_SLUG_RE`，安全。新增的 `_atomic_install` 生成的临时名 `.installing-<uuid>` **必须**绕过 `_SLUG_RE` 校验（它不落在 `<slug>` 目标位置，而是同级临时目录），所以 `_atomic_install` 内部直接 `os.path.join(SKILLS_DIR, f".installing-{uuid4().hex}")`，**不要**复用一个会校验 slug 的助手。

### 自动加载与审批：**可接受，不做「直达执行」旁路**

**结论**：用户说「帮我整理下载目录」→ 自动挂载 `workspace-organizer` → 模型调 `skill_run` → `skill_run` 是 `medium`（`tools.py:319`）→ `readonly_auto` 下弹审批卡。**这是可接受的，且必须保持。**

理由（三条，写在设计里供 review 引用）：
1. **审批不看"谁发起的"，只看 risk**。`approval.py` 的判定入口是 `registry.risk_of(name, args)`（`agent.py:273`），自动加载只是收窄 `allow` 白名单，不触碰 risk（PRD §8.2 亦明确）。
2. **`skill_run` 内部会真的改文件**：`workspace-organizer` 的正文要求 `fs_move`，而 `orchestrator._Runner.ctx["exec_tool"]`（`orchestrator.py:239`）**复用 `agent.exec_tool` 同一个函数** → 内部的 `fs_move` 同样会逐个弹审批。也就是说：**即使给 `skill_run` 开口子，技能内部的每次写入仍会弹卡**，用户会看到更多卡，而不是更少。
3. **开口子会破坏统一安全模型**：若「orchestrator 内部技能执行直达」，就必须在 `exec_tool` 里区分 `source == "skill"` 并跳过 `approval.request` → 一个可被技能包正文诱导的免审通道（在线安装的技能来自 GitHub，含 `scripts/` 时是高危）。**这是安全红线（PRD §8.5 第 1 条）的反面。**

**体验缓解（P0 内做，成本极低）**：`start.auto_loaded` 的 StepLine 文案里明确写「运行技能时会请求你确认」，让用户在弹卡前有预期：
`已自动加载：文件整理 · 技能 workspace-organizer · 工具 7 个（运行时会请求你确认）`

**若未来确实要降摩擦**，正确方向是 `approval.py` 的「本任务允许同类」（已存在，`ApprovalCard.btn_allow_once` → `resolve_approval(remember=True)`），**不是**新增免审通道——写进设计留档。

### `start` 事件扩展：**安全，无 SSE 广播影响**

**改法**（`agent.py:235`）：
```python
auto_loaded = {}                 # 未命中 profile → 空 dict
...
emit({"type": "start", "session_id": session_id,
      "provider": provider_cfg.get("id"), "label": provider_cfg.get("label"),
      "auto_loaded": auto_loaded})
```
**受影响面核查：**
- `routers/assistant.py:_broadcast`（`:161`）/ `_STREAM_SINKS`（`:158`）→ **不受影响**。它们按 `session_id` 转发**任意** dict，不校验字段；`gen()`（`:268`）逐事件 `yield _sse(ev)` 原样透传。✅
- 非流式路径（`stream=false`，`:259`）：`events.append` 收全部事件，`start` 在 `events[0]` 附近，`auto_loaded` 自然带上。✅
- 客户端：`_on_event`（`:1854`）**当前没有 `start` 分支**（`kind == "session"` 才有），新增 `elif kind == "start":` 是纯增量，不覆盖既有行为。✅
- `tests/test_assistant.py` 现有断言若对 `start` 事件做严格等值比较会挂 —— 已核查：**没有**对 `start` 的字段做 `==` 断言，安全。

### 缓存刷新：同步阻塞 vs 后台

**定案（A6 in PRD）**：
- `GET /catalog/mcp|skills`：**先返缓存**（`stale=True` 也返），`force=1` 才同步拉（`timeout=5.0`）。
- `POST /catalog/refresh`：`ThreadPoolExecutor(3)` 并行三源，总超时 **12s**；超时源标 `error`，其余照常返回 `{ok: 部分成功, counts, errors[]}`。
- 首屏：客户端打开清单中心 → 立即渲染 `state` 里已有的技能/工具/MCP（走 `/state`），再发 `GET /catalog/*` 补在线区，**不阻塞**。

---

## 8. 最终 REST 接口表

**返回约定（已读源码确认）**：`routers/assistant.py:77-82` 有 `ok(data)` → `{code:0, message:"ok", data}` 与 `fail(code, message, data)` → `{code!=0, message, data}`。**业务错误一律 HTTP 200 + `fail()`**（唯一例外：走 SSE 的路径返回 HTTP 400，如 `:238`）。客户端 `_unwrap`（`assistant_client.py:39`）据此判 `code != 0` 抛 `RuntimeError(message)`。

| method | path | 入参 | 出参 `data` | 错误码 |
|---|---|---|---|---|
| GET | `/catalog/mcp` | `q=`, `cursor=`, `force=0|1` | `{items[], cursor, fetched_at, stale, error}` | 2020 不可达 / 2021 上游非 200 |
| GET | `/catalog/skills` | `q=`, `force=0|1` | `{items[], total_count, fetched_at, stale, error}` | 2020 / 2021 |
| GET | `/catalog/skills/{owner}/{repo}` | `ref=` | `{commit, items[], fetched_at, stale}` | 2020 / 2021 / 2022 |
| POST | `/catalog/skills/install` | `{repo, ref, dir, slug, overwrite:false}` | `{slug, files, bytes, has_scripts, commit}` | 2022 结构非法 / 2023 已存在 / 2020 |
| POST | `/catalog/mcp/install` | `{registry_name, id?, name?, env:{}, risk?, connect:true}` | `{server, connected, error}` | 2024 必填 env 缺失 / 2025 连接失败（配置保留）/ 2020 |
| POST | `/catalog/refresh` | `{}` | `{ok, counts:{mcp,gh_repos}, errors[]}` | —（总是 `ok()`；错误在 data 里） |
| POST | `/catalog/skills/from_dir` | `{path, overwrite:false}` | `{slug, files, bytes}` | 2022（目录无 SKILL.md）/ 2023 |
| POST | `/catalog/skills/from_url` | `{url, overwrite:false}` | `{slug, files, bytes}` | 2026 URL 不受支持 / 2022 |
| GET | `/tools/prefs` | — | `{disabled[], max_tools, total, active, over:null|str[]}` | — |
| PUT | `/tools/prefs` | `{disabled[], max_tools}` | 同上 | 2002 参数非法 |
| GET | `/profiles` | — | `{auto_load, profiles[]}` | — |
| PUT | `/profiles` | `{auto_load?, profiles?}` | 同上 | 2002 |

**新增错误码表**（接在现有 2001–2011 之后，见 `routers/assistant.py` 既有码）：

| code | 含义 | 客户端提示 |
|---|---|---|
| 2020 | 在线目录不可达（无网络/无代理/超时） | 「无法连接在线目录。国内网络访问 GitHub / MCP Registry 通常需要代理…（当前代理：{值}）」 |
| 2021 | 上游非 200（含 GitHub 403 限流） | 「GitHub 接口限流，请 {N} 秒后重试，或先使用缓存」 |
| 2022 | 技能包结构非法（无 SKILL.md / frontmatter 缺 name） | 「该目录/仓库里不是有效的技能包」 |
| 2023 | 目标 slug 已存在且 `overwrite=false` | 确认框「覆盖 / 取消」 |
| 2024 | MCP 必填环境变量缺失 | 安装对话框逐项标红，确定按钮禁用 |
| 2025 | MCP 安装后连接失败（配置已保留） | 「已保存配置但连接失败：{err}」+ 卡片 `×` |
| 2026 | URL 不是受支持的技能地址 | 「只支持 GitHub / Gitee 的 SKILL.md 直链」 |

---

## 9. 风险清单（按「会崩 / 会卡 / 会错」分级）

### 9.1 会崩（进程级）

| # | 风险 | 触发场景 | 兜底 |
|---|---|---|---|
| R-C1 | **原生文件对话框 `0x8001010E`** | 「从本地文件夹导入技能」若直接调 `QFileDialog.getExistingDirectory` | 强制复用 `_pick_file(directory=True)`；`test_assistant_page.py:1194` 静态扫描作为门禁；**不改 `_pick_file` 默认行为**，只加 `directory` 分支 |
| R-C2 | **QThread GC 崩溃 `0xC0000409`** | 新的 catalog REST 调用若自己写 `QThread` 子类 | 一律用现有 `core/task_runner.Worker`（`:55` 已 import），不新增 `QThread` 子类 |
| R-C3 | **client 退出码 127（Qt 析构）** | 新增 `QSplitter` / `QPlainTextEdit` 未正确 `deleteLater` | 沿用 `ManagerDialog` 生命周期（页面持有 `self._manager`，`:1090`）；新增 widget 都挂到已存在的父 widget；测试按 passed 计数判定 |
| R-C4 | **MCP 子进程泄漏** | 在线安装 `connect=false` 后用户删除服务器 | `mcp.delete` 已 `remove_server` → `disconnect`（`:306`），无需新增 |

### 9.2 会卡（体验级）

| # | 风险 | 触发场景 | 兜底 |
|---|---|---|---|
| R-S1 | **首屏空白** | 缓存也没有 + 网络不可达 | 列表区空态文案（PRD §7）；`GET` 先返缓存、**永不抛**（`_cache_read` 失败返回 `{}`） |
| R-S2 | **`refresh` 卡 12s+** | 三源都超时 | `ThreadPoolExecutor(3)` + 单源 `timeout=5.0` + 总 `12s` 上限；`routers` 用 `run_in_threadpool`（`:260` 已有用法），不阻塞事件循环 |
| R-S3 | **安装大包卡死 UI** | GitHub 仓库 20 文件 × 2MB | `max_files=20` / `max_bytes=2MB` 硬限；超限只取 `SKILL.md` + 提示「已跳过 N 个附件」；`POST` 客户端 `timeout=120`（`assistant_client.py:55` 已支持） |
| R-S4 | **`/state` 变慢** | `state()` 里新增计算（下发计数） | 下发计数从 `select_schemas()` 的**长度**取，不重复算；`state` 已 `list_sessions(500)`（`agent.py:114`），不要在里面加 IO |
| R-S5 | **菜单超屏** | `[+]` 加到 12 项，200% 下高约 480px | `[+]` 按 §6.1 保留 4 段 + `addSeparator`，管理类压缩成一组 |

### 9.3 会错（正确性）

| # | 风险 | 触发场景 | 兜底 |
|---|---|---|---|
| R-E1 | **模型看到不存在的工具** | 只改 `openai_schemas()` 未改 `_build_messages` | **A3 强制同源**：`schemas` 一份对象两处用；单测 `test_build_messages_matches_schemas` 断言文字清单的工具名集合 == schema 名集合 |
| R-E2 | **`.backup-*` 被当技能列出** | 安装覆盖后 | **A8**：`list_skills` 跳 `.` 前缀；单测 `test_list_skills_skips_dot_dirs` |
| R-E3 | **安装半成品残留** | 网络中断 | `.installing-<uuid>` 全删；旧目录 `.backup-<ts>` 自动回滚（`_atomic_install`）；单测 `test_install_rollback_on_failure` |
| R-E4 | **代理未生效 → 全 2020** | 依赖环境变量 | `_client` **显式** `proxy=_proxy(cfg)`（默认 `http://127.0.0.1:7897`）+ `trust_env=False`；单测 `test_catalog_client_trust_env_false` |
| R-E5 | **配置新字段读不到** | 忘了加 `_default_config()` | D7；单测 `test_default_config_has_new_keys` |
| R-E6 | **老客户端挂载失效** | 后端开始只读 `mounted_skills` | A4 三层兼容；单测 `test_start_turn_mounted_skills_none_is_noop` |
| R-E7 | **自动加载把审批降级** | 有人为了少弹卡给 profile 加免审 | 设计红线写明（§7「自动加载与审批」）；`approval.py` **零改动**（PRD §9.5 一致）；review 检查点 |
| R-E8 | **回归测试被改坏** | 三个既有断言会必然失败（见 §10） | 列入 T05 的**验收标准**，必须显式更新而非删除 |

---

## 10. 必须同步更新的既有测试（不改就会红）

| 测试 | 位置 | 为什么会红 | 改法 |
|---|---|---|---|
| `test_attach_plus_menu_has_five_entries` | `client/ui_tests/test_assistant_page.py:407` | `[+]` 菜单项从 4 个变 10+ | 改为断言「首项是 `添加附件…`」+「包含 `从本地文件夹导入技能…` / `粘贴 URL 安装技能…` / `刷新在线目录`」+「以 `审计` 结尾」 |
| `test_multi_skill_prefix_injected_in_payload` | `:392` | `_send_with_session` 不再拼正文 | 改为断言 `payload["message"] == 原文` 且 `payload["mounted_skills"] == [..]` |
| `test_manager_dialog_has_five_tabs` | `:597` | Tab 数不变（仍 5 个）→ **不会红** | 保持 |
| `test_manager_lists_fill_from_state` | `:609` | 技能 Tab 改 `QSplitter` 后 `_skill_list` 仍是 `QListWidget` → 仍能 `count()` | 保持；但若重命名 widget 需同步（设计保名 `_skill_list` / `_tool_list` / `_mcp_list`，**不要改名**） |
| `test_light_theme_covers_bottom_bar_and_nav` | `:1362` | 新增 `#StepAction`（若给按钮单独 objectName）会漏 | 按钮复用 `#MiniBtn`（已覆盖），或把 `#StepAction` 加进该断言的期望列表 |

> **设计决策**：技能/工具/MCP 三个 `QListWidget` **保持现有属性名** `_skill_list` / `_tool_list` / `_mcp_list`，避免大面积改测试。新增控件用新名（`_skill_search` / `_skill_detail` / `_tool_checks`）。

---

## 11. 执行顺序建议（给工程师）

```
T01 后端目录层（catalog + profiles + store 字段）        ← 无 UI 依赖，可独立跑单测
  ├─ T02 后端装配（tools 过滤 / agent 同源 / 路由 / 技能安装）   ← 依赖 T01
  │     └─ T03 客户端清单中心（ManagerDialog 三 Tab）              ← 只依赖 T02 的接口表
  │           └─ T04 客户端底栏四入口（[+] [技能][工具][MCP]）      ← 依赖 T03 的 open_manager(focus=)
  └─ T05 联调 + 回归 + 既有测试更新                                ← 依赖 T01–T04
```
**2 人并行建议**：工程师 A 走 T01→T02（后端一竿到底，上下文复用率高）；工程师 B 在 T02 的**接口表定稿后**（§8 已定稿，可直接开工）并行做 T03→T04 的**纯 UI 骨架 + 假数据联调**，最后两人合流 T05。详见任务分解文档。
