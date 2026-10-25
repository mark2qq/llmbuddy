# 智能助理 · 技能 / 工具 / MCP 清单中心（增量 PRD）

> 版本：v1.0.8 增量 · 作者：许清楚（产品经理） · 关联基线：`docs/智能助理.md`、`docs/智能助理-引导会话-PRD.md`

## 1. 背景与目标

### 1.1 项目信息

| 项 | 内容 |
|---|---|
| Language | 中文 |
| Programming Language | Python —— 后端 FastAPI（`serve.py` + `routers/assistant.py`）+ 客户端 PySide6，**不引入 Web 技术栈** |
| Project Name | `assistant_capability_hub` |
| 原始需求 | 「做一个技能、工具、MCP 列表 参考 workbuddy，手动获取全部在线开源免费的技能、工具、MCP，1) 可以在对智能助理对话框底部技能、工具、MCP 处手动添加本地及在线的技能、工具、MCP，参考 workbuddy 2) 根据用户需求在处理任务时默认加载常用技能、工具、MCP」 |

### 1.2 现状事实（已核对源码，作为设计的硬约束）

| 能力 | 现状 | 代码位置 |
|---|---|---|
| 技能 | `SKILLS_DIR/<slug>/SKILL.md`，list/load/save/delete/import_zip/export_zip，内置 3 个 | `core/assistant/skills.py` |
| 工具 | `ToolRegistry` 内置 12 个（fs_* 11 + shell_run + web_fetch + skill_list/skill_run），每个带 risk | `core/assistant/tools.py` |
| MCP | stdio/http 双传输，配置在 `config.json.mcp_servers`，注册为 `mcp__<id>__<tool>` | `core/assistant/mcp.py` |
| 左栏 | **1.0.8 起已无「技能/工具/MCP/审计」四个二级入口**（`styles.qss:884` 注释明确「迁到底栏 `[+]` 菜单」），只剩「任务 / 空间」两区 | `assistant_page.py:586 build_nav_panel` |
| 管理面板 | `ManagerDialog` 5 Tab（任务/技能/工具/MCP/审计），**单列表 + 底部按钮行**，非卡片流 | `assistant_page.py:2731` |
| 底栏 | `[+] [工作区] [⊘只读自动] [技能] → ◐ [模型▾] [⚡][↑]`，`[+]` 菜单 = 添加附件 + 技能/工具/MCP/审计 4 个跳转 | `assistant_page.py:569` |
| 技能挂载 | `_show_skill_menu` 只列**已安装**技能，上限 5；挂载后消息拼成「用 xx 技能：<text>」 | `assistant_page.py:2461 / 1768` |
| 下发 | `registry.openai_schemas()` **全量**下发；系统提示工具清单截断 80 条 | `agent.py:320 / 487` |
| 无 | **工具无启用/禁用开关**；**无在线目录/缓存**；**无 profile/自动加载机制** | — |

### 1.3 问题定义

1. **供给断裂**：只有「自己写 SKILL.md / zip 导入 / 手填 MCP 命令行」三条路，普通用户拿不到任何现成能力。生态里已有 12,618 个 `topic:claude-skills` 仓库和官方 MCP Registry，产品侧完全没有入口。
2. **入口错位**：用户原话要的「对话框底部技能/工具/MCP 处手动添加」现在不存在——`[+]` 里的四项只是跳转，`[技能]` 只能挂已安装的。
3. **上下文污染**：工具全量下发（12 内置 + 每个 MCP 若干）没有上限控制，本地小模型上下文容易被挤爆，模型也更容易乱调工具。

### 1.4 产品目标（3 个，正交）

| # | 目标 | 可衡量标准 |
|---|---|---|
| G1 | **供给可获取**：让用户在一个清单中心里看到、筛选、安装在线开源技能与 MCP | 冷启动 3 分钟内可装好 1 个技能 + 1 个 MCP；在线目录首屏 ≤ 2s（有缓存）/ ≤ 8s（首次拉取） |
| G2 | **添加就在手边**：在对话框底部的技能/工具/MCP 入口完成「本地 + 在线」添加，不离开对话 | 从底栏完成一次技能安装 ≤ 3 次点击；`[+]`、`[技能]`、`[工具]`、`[MCP]` 四个入口都覆盖「添加」动作 |
| G3 | **按需自动加载**：按用户消息自动选出常用技能/工具/MCP，减少手动挂载与上下文浪费 | 命中内置 3 个 profile 的典型请求时，自动挂载命中率 ≥ 80%；下发工具数从全量降到 ≤ 24 |

### 1.5 本次范围 / 非范围（一句话）

**做**：清单中心页面（升级 ManagerDialog）、三类在线数据源接入 + 落盘缓存、四类「手动添加」入口、工具启用开关 + 下发上限、按需求自动加载。
**不做**：技能/MCP 的市场评分与评论、在线技能仓库的全站爬取、工具的热插拔（自定义脚本工具）、按 skill 计费。

---

## 2. 用户故事（P0 场景）

| # | 角色 | 想要 | 以便 | 对应需求 |
|---|---|---|---|---|
| US-1 | 刚装好 llmBuddy 的普通用户 | 在清单中心看到一批开源技能（带 star 数和一句话说明），点一下就装好 | 不用自己写 SKILL.md 就能用上现成能力 | P0-1 / P0-2 |
| US-2 | 想接外部数据源的用户 | 从官方 MCP Registry 搜到一个远程 MCP，点「安装」，产品自动判断是 `npx` 还是 HTTP 并填好配置 | 不用手抄 command/args/url | P0-3 |
| US-3 | 手上已有技能 zip / 本地文件夹 / 别人的 SKILL.md 链接的用户 | 在底部 `[技能]` 菜单里直接「导入 zip / 从本地文件夹 / 粘贴 URL」 | 不跳到别的页面也能加 | P0-4 |
| US-4 | 关心上下文与模型能力的用户 | 在 `[工具]` 菜单里关掉不用的工具、看到当前会下发多少 | 本地小模型不被工具清单淹没 | P0-5 |
| US-5 | 只想提需求的用户 | 输入「帮我把这个目录整理一下」，产品自动挂上 `workspace-organizer` 和必要工具 | 不用知道「技能」是什么概念 | P0-6 |
| US-6 | 对第三方代码有顾虑的用户 | 安装前看到来源仓库、star、是否含脚本、默认风险等级，装坏了能一键回滚 | 敢用在线来源 | P0-7 |

---

## 3. 竞品形态拆解（WorkBuddy）

| 维度 | WorkBuddy 的做法 | 本产品的落地结论 | 差异理由 |
|---|---|---|---|
| 信息架构 | 「技能 / 工具 / 连接器」三分栏平级管理页 | **保留三分栏，但统一进一个「能力清单中心」**，Tab 顺序：清单(技能)/工具/MCP，任务与审计留在 ManagerDialog 原位 | 用户原话是「技能、工具、MCP 列表」，三分栏是心智锚点；任务/审计不是「能力」，不该混在一起 |
| 列表形态 | 卡片流：名称 + 一句话描述 + 安装/启用状态 + 来源标记，每卡 2~3 行 | **两行紧凑卡片 + 右侧操作按钮**（见 §5.2） | 本机 200% 缩放、窗口最小高 680；标准 WorkBuddy 卡片（约 96px 高）一屏只放 3 张 |
| 顶部 | 搜索框 + 分类筛选 chip | 「搜索 + 来源/类别下拉 + 状态筛选」三件套，横向一行 | 200% 缩放下 chip 行会换行，改用下拉更省高度 |
| 安装/启用 | 每卡一个主按钮（安装 / 已安装 / 启用 / 停用） | 同左，但**已安装态文案分三类**：已安装·内置 / 已安装 / 已安装·有更新 | 内置 3 个技能不可删，必须区分 |
| 在线目录 | 自家市场 | **官方 MCP Registry + GitHub topic 仓库**（双源），带落盘缓存与离线退化 | 无自建市场，只能接公开源 |
| 详情 | 抽屉/弹层展示 README | 右侧「详情面板」（列表 62% + 详情 38%），不弹新窗口 | 200% 缩放下弹窗叠加会挡住来源信息，同屏对照更安全 |
| 底部入口 | 输入框附近有入口能打开清单并添加 | `[+]` / `[技能]` / `[工具]` / `[MCP]` 四入口，每个都含「添加」项 | 用户原话点名「对话框底部技能、工具、MCP 处」 |
| 默认加载 | 按任务类型预置常用集合 | **预置 profile（主）+ 关键词匹配（次）+ 系统提示兜底（降级）** | 见 §5.4 |

---

## 4. 功能范围

### 4.1 P0（Must have —— 一个迭代内必须做完）

| # | 需求 | 验收标准 |
|---|---|---|
| **P0-1** | **能力清单中心（技能 Tab）升级** | `ManagerDialog` 技能 Tab 改为「左列表（62%）+ 右详情（38%）」：列表为两行卡片（第 1 行 `名称 · 版本`，第 2 行 `一句话描述` + 右侧状态徽标），支持搜索框（匹配 name/description）、「来源」下拉（全部/内置/本地/在线）、排序（star 降序 / 更新时间）；点击卡片右侧显示来源（仓库 `owner/repo`、⭐star、文件数、是否含 `scripts/`、正文预览前 400 字）；**680px 高窗口下一屏可见 ≥ 5 张卡片**（实测：卡片高 52px + 间距 4px ≈ 56px，列表区 ≥ 300px） |
| **P0-2** | **在线技能目录（GitHub 源）** | 接入 `GET https://api.github.com/search/repositories?q=topic:claude-skills&sort=stars&per_page=60`；选中仓库后 `GET /repos/{o}/{r}/git/trees/{branch}?recursive=1` 枚举 `**/SKILL.md`，列表展示每个技能名 + 描述（从 SKILL.md frontmatter 读，读不到则显示「来自 {repo}」）；「安装」按钮把 SKILL.md 及同目录附属文本文件（≤ 20 个、总 ≤ 2MB）写入 `SKILLS_DIR/<slug>/`；安装成功列表出现新项、`/state` 的 `skills` 数量 +1、状态栏提示「已安装技能 X」；重复安装弹确认框（覆盖/取消）；**离线或 401/403 时不报空白**，退回缓存并在顶部提示条显示缓存时间（见 P0-9） |
| **P0-3** | **在线 MCP 目录（官方 Registry 源）+ 自动生成配置** | 接入 `GET https://registry.modelcontextprotocol.io/v0/servers?limit=50&search=<q>&cursor=<c>`，支持分页（`metadata.nextCursor`）；卡片显示 `title`（回退 `name`）+ `description` + 传输徽标（远程/本地）+ 免费标记（§4.3）；「安装」自动映射：`remotes[0].type=="streamable-http"` → `transport=http, url=<url>`；`packages[0].registryType in ("npm","pypi")` → `transport=stdio, command="npx"/"uvx", args=[runtimeArguments 按序] + [identifier@version]`；必填环境变量（`environmentVariables.isRequired`）弹输入框，缺一项则不允许安装；安装后自动 `connect`，失败时保留配置并在卡片显示 `× 连接失败` + 错误摘要（≤ 200 字，可展开全文） |
| **P0-4** | **底栏四入口「手动添加」（本地 + 在线）** | 菜单结构与行为严格按 §5.1 落地：`[+]`、`[技能]`、`[工具]`、`[MCP]` 四个入口都含对应添加项；「从本地文件夹导入技能」选目录 → 校验目录内有 `SKILL.md` → 复制进 `SKILLS_DIR`；「粘贴 URL 安装技能」支持 `raw.githubusercontent.com` 直链与含 `/blob/` 的网页链接（自动转 raw）；「刷新在线目录」触发三源并行刷新并更新缓存；四个入口点击后**不离开对话页**（清单中心以 `QDialog` 打开，关闭后回到原位） |
| **P0-5** | **工具启用/禁用 + 下发上限** | 工具 Tab 每行有启用开关（`QCheckBox`），内置工具与 MCP 工具一视同仁；持久化到 `config.json.tool_prefs = {"disabled": [names], "max_tools": 24}`；禁用后 `/state.tools` 返回 `enabled=false` 且**不下发给模型**；`registry.openai_schemas()` 按「启用 + 优先级排序」裁到 `max_tools`，顺序为：内置低风险 fs_* → web_fetch → skill_list/skill_run → shell_run → MCP 工具（按 server 顺序）；禁用 `skill_run`/`skill_list` 时 UI 提示「关掉后技能无法被调用」并要求二次确认；工具 Tab 顶部常驻文案「当前下发 N 个 / 上限 M 个」 |
| **P0-6** | **按需求自动加载常用集合** | 按 §5.4 三层机制：① 内置 3 个 profile 在设置页可查看/编辑；② 用户消息关键词命中 profile 的 triggers → 自动挂载对应技能 + 收紧工具白名单到 profile 的 tool set；③ 命中不了则走降级：把已安装技能清单 + 一句话说明写进系统提示（≤ 20 条、每条 ≤ 30 字），由模型自行 `skill_run`；**每次自动加载在对话流插入一条可点击的 `StepLine`**：「已自动加载：文件整理（3 个技能 · 8 个工具）｜点此关闭自动加载」；`config.json.auto_load = false` 时完全不自动加载 |
| **P0-7** | **安装前信息展示与安全留痕** | 任何在线安装（技能/MCP）点「安装」后先弹确认框，必须含：来源标识（`owner/repo` 或 Registry `name`）、⭐star 数（技能）/ 传输方式（MCP）、**是否含 `scripts/` 目录或可执行文件**（是则黄色警示「该技能包含脚本，运行时有额外风险」）、默认风险等级（技能=medium，MCP=`high`）；确认后写审计：`skill_installed_online` / `mcp_installed_online`（含 source、slug、版本、sha/commit）；失败回滚见 §7 |
| **P0-8** | **缓存与离线退化** | 三份缓存落 `assistant/catalog/{mcp_servers.json, gh_repos.json, gh_skills.json}`，各含 `fetched_at`；TTL：MCP Registry 24h、GitHub 仓库列表 24h、单仓库技能树 7d；进入清单中心先渲染缓存（无网络也能看到上次结果），再后台刷新；刷新失败顶部提示条：「在线目录刷新失败（{原因}）· 正在显示 {MM-dd HH:mm} 的缓存 · [重试]」，**绝不出现空白页**；无代理直连失败时错误文案固定为：「无法连接在线目录。国内网络访问 GitHub / MCP Registry 通常需要代理，请在系统代理中开启后点重试（当前代理：{可用/不可用}）」 |
| **P0-9** | **回归不破坏** | 两套测试**分开跑**全绿：`tests/test_assistant.py`、`client/ui_tests/test_assistant_page.py`；新增单测覆盖：目录解析（Registry→配置映射、GitHub tree→技能列表）、缓存读写与 TTL、工具裁剪 `openai_schemas()`、profile 匹配、URL 转 raw |

### 4.2 P1（Should have）

| # | 需求 | 验收标准 |
|---|---|---|
| P1-1 | 技能「更新」检测 | 已安装技能的 `meta.source` 记录 commit，刷新目录比对新 commit → 卡片显示「有更新」，一键更新前二次确认 |
| P1-2 | MCP 目录分类筛选 | 按 Registry 的 `title`/`description` 关键词做「文件/数据库/浏览器/搜索/其他」本地分类，纯前端，不依赖上游字段 |
| P1-3 | 在线技能批量安装（整仓） | 仓库详情页「安装全部 N 个技能」，逐个失败不中断，结束汇总「成功 8 / 失败 1」 |
| P1-4 | 自定义 profile | 用户新建 profile（选技能 + 选工具 + 触发关键词），存 `config.json.profiles` |
| P1-5 | 代理配置入口 | 设置页加「在线目录代理」输入框（默认读 `HTTP(S)_PROXY` 环境变量），持久化到 `config.json.catalog_proxy` |
| P1-6 | 清单中心入口常驻 | 左栏「智能助理」下恢复一行「能力清单 (N)」（N = 技能+MCP 数），点击直接打开清单中心 |

### 4.3 P2（Nice to have）

| # | 需求 | 说明 |
|---|---|---|
| P2-1 | 自建精选清单（`catalog/featured.json`） | 随包内置 10~20 个经过验证的技能/MCP，解决首次冷启动的「不知道装什么」 |
| P2-2 | 技能市场评分/下载量 | 需要第三方数据源，暂不做 |
| P2-3 | 技能正文结构化预览（Markdown 渲染） | 现在纯文本预览即可 |
| P2-4 | MCP 服务器连通性预检（安装前 HEAD 探测 remote url） | 会拖慢安装流程，后续做 |
| P2-5 | 全量扫描 GitHub 所有 `SKILL.md`（不止 topic 仓库） | 配额与噪声都不可控 |

---

## 5. 关键交互流程

### 5.1 底部入口菜单结构（决策 4 答案）

#### 5.1.1 `[+]` 菜单（`_build_attach_menu`，assistant_page.py:569 改造）

```
添加附件…                          → 原逻辑（插入 @路径）
────────────────────────────
能力清单（技能 / 工具 / MCP）…      → ManagerDialog(key="hub")，停在「技能」Tab
────────────────────────────
从清单添加技能…                     → ManagerDialog(key="hub", focus="skills")
从清单添加 MCP…                     → ManagerDialog(key="hub", focus="mcp")
────────────────────────────
从本地文件夹导入技能…               → 选目录（含 SKILL.md）→ 复制进 SKILLS_DIR
导入技能 zip…                       → 原 _import_skill
粘贴 URL 安装技能…                  → 输入框收 URL（raw / blob 均支持）
粘贴 JSON 添加 MCP…                 → 收一段 mcpServers JSON（兼容 Claude Desktop 配置片段）
────────────────────────────
刷新在线目录                        → 三源并行刷新 + 更新缓存 + 状态栏「已刷新（N 个技能 / M 个 MCP）」
────────────────────────────
工具 / MCP / 审计                    → 原跳转（保留，术语与 Tab 对应）
```

> **决策点**：`[+]` 只保留「添加类」动作 + 一个汇总入口，把「管理类」四个跳转压缩成一组，避免菜单超过 10 项（200% 缩放下 10 项菜单高约 400px，已接近屏高 1/3）。

#### 5.1.2 `[技能]` 菜单（`_show_skill_menu`，assistant_page.py:2511 改造）

```
挂载技能（0/5）                                ← 原「最多同时挂载 5 个」
────────────────────────────
（已安装技能列表，checkable，hover 显示 description + 工具数）
 ☑ workspace-organizer  内置
 ☐ repo-digest          内置
 ☐ awesome-skill  ⭐1.2k 在线      ← 已安装的在线技能带来源徽标
────────────────────────────
🔍 更多技能（清单中心）…              → ManagerDialog(key="hub", focus="skills")
＋ 从在线清单安装…                    → 同上，且打开时搜索框聚焦
＋ 从本地文件夹导入…
＋ 粘贴 URL 安装…
⟳ 刷新在线目录
────────────────────────────
清除全部（挂载 N 个时可见）
```

**关键改动**：原来的空态文案「（暂无技能，去底栏 [+] → 技能 里新建或导入 zip）」改为**三条可点的添加入口**，空态也能完成安装——这是用户原话「在技能处手动添加」的核心。

#### 5.1.3 `[工具]` 新入口（底栏新增，紧跟 `[技能]`）

```
当前下发 18 / 上限 24
────────────────────────────
☑ fs_list   只读      内置
☑ fs_read   只读      内置
☐ shell_run 高危      内置     ← 关闭需二次确认
☑ 文件系统 · 12 个工具          ← MCP 服务器按组折叠，整组开关
────────────────────────────
工具清单与开关…                → ManagerDialog(key="tools")
⟳ 重连已启用 MCP                → 逐台 connect，失败逐条报错不中断
```

- 底栏按钮文案：`工具`（已下发数 > 上限时显示 `工具 ⚠`，tooltip 说明被裁掉的名字）。
- **决策点**：工具只能「启用/禁用」，不能「在线获取」——内置工具是代码写死的，MCP 工具随服务器带入，所以 `[工具]` 的添加语义就是「打开开关」+「去加 MCP」。

#### 5.1.4 `[MCP]` 新入口（底栏新增，紧跟 `[工具]`）

```
已配置 2 台 · 已连接 1 台
────────────────────────────
● 文件系统        本地 · 12 工具    [断开]
○ github         远程 · 未连接      [连接]
────────────────────────────
🔍 从在线清单安装 MCP…      → ManagerDialog(key="hub", focus="mcp")
＋ 手动添加（stdio / http）…  → 原 McpDialog
＋ 粘贴 JSON 添加…
────────────────────────────
管理 MCP 服务器…            → ManagerDialog(key="mcp")
```

底栏布局变更（一行放不下时折行）：

```
[+] [工作区] [⊘只读自动] [技能] [工具] [MCP] → ◐ [模型▾] [⚡][↑]
```

> **风险与对策**：底栏已 6 个控件，再加 2 个在 200% 缩放、最小窗口下会挤压。对策：`[工具]`、`[MCP]` 用**纯图标 + 数量徽标**（`⚙` / `🔌`，宽 26px，同 `[+]`），tooltip 给全称；窗口宽 < 1100（逻辑像素）时自动隐藏这两个按钮，改由 `[+]` 菜单进入（用户可配置）。

### 5.2 清单中心布局（决策 1 答案）

**结论：升级现有 `ManagerDialog`，不新建独立页面。**

理由：
1. 左栏 1.0.8 起已无「技能/工具/MCP/审计」二级入口，**没有可承载新页面的导航位**；新建页面要么改左栏（挤占 680px 高度，与已有 6 个一级入口冲突），要么做成一/二级页面切换（改动 `main_window` 的 `QStackedWidget`，波及面远大于收益）。
2. 用户原话要的是「打开清单 → 添加 → 回到对话」，`QDialog` 天然满足「不离开对话页」；改为页面反而要处理「回来后对话滚动位置 / 流式状态」。
3. `ManagerDialog` 已有技能/工具/MCP 三个 Tab 与全部按钮行，改造成本低于新建 + 迁移。

窗口与尺寸：

| 项 | 值 | 说明 |
|---|---|---|
| 默认尺寸 | 占主窗口的 86% × 84% | `QDialog` 非模态前先 `resize`，保证 200% 缩放（逻辑 1366×768 屏）下可用 |
| 最小尺寸 | 720 × 520 | 低于此值卡片区退化为单列，按钮改图标 |
| 分栏 | 左列表 62% / 右详情 38%（`QSplitter`，可拖拽，比例记忆到 QSettings） | 详情面板可折叠（宽度 < 720 时自动折叠为底部预览） |

```
┌─ 能力清单中心 ─────────────────────────────────────────────────┐
│ [技能 43] [工具 18/24] [MCP 12]  [任务] [审计]                  │  ← QTabWidget
├───────────────────────────────────────────────────────────────┤
│ 技能 Tab                                                       │
│ ┌ 搜索 name/描述 ────────────┐ [来源▾] [排序▾] [⟳ 刷新]        │
│ ├────────────────────────────┬────────────────────────────────┤
│ │ ▸ workspace-organizer  v1.0│  workspace-organizer           │
│ │   把散落文件按类型归档      │  内置 · 5 个工具 · 12 KB        │
│ │              [内置]        │  ─────────────────────────────  │
│ │ ▸ repo-digest        v1.0  │  描述：…                        │
│ │   扫描代码库产出摘要        │  允许工具：fs_list fs_read …    │
│ │              [内置]        │  文件：SKILL.md (3.1 KB)        │
│ │ ▸ web-brief          v1.0  │  正文预览（前 400 字）…         │
│ │   抓网页提炼中文简报        │  ─────────────────────────────  │
│ │              [内置]        │  [挂载] [导出 zip] [删除]       │
│ │ ▸ pdf-extract  ⭐2.4k      │                                │
│ │   PDF 表格提取              │                                │
│ │              [在线·已安装]  │                                │
│ └────────────────────────────┴────────────────────────────────┘
│ 共 43 项 · 显示缓存于 08-08 14:20 · [安装全部选中]              │
└───────────────────────────────────────────────────────────────┘
```

单张卡片（高 52px）在 200% 缩放下用 `QListWidget` + `setItemWidget`；也可用 `QListWidget` 纯文本两行 + 右侧状态列（**推荐后者**：纯文本随 DPI 自动缩放，自绘 widget 在 200% 下容易错位）。列表用 `QListWidget` 单行两行文本 = `name · vx.y` + `\n` + 描述，`setWordWrap(false)`，右侧状态用 `QListWidgetItem` 的 tooltip + 前置标识字符（`[内置] / [在线] / [已装]`）。

**屏效测算（680px 最小窗口）**：Tab 栏 32 + 工具条 36 + 列表区（680 - 32 - 36 - 状态行 24 - 边距 24 ≈ 564）→ 每行 52px 可显示 **10 行**；即便窗口只剩 520px 也可显示 8 行。**结论：卡片流可行，但必须是紧凑两行卡片，不用 WorkBuddy 的 96px 大卡片。**

### 5.3 在线来源的「免费 / 需密钥」界定（决策 3 答案）

**结论：不隐藏「需密钥」的条目，但用「零配置可启动」作为默认筛选，且明确三级标注。**

| 标记 | 判定规则（纯本地推断，不请求上游） | 呈现 |
|---|---|---|
| `零配置` | MCP：`packages[0].registryType in (npm, pypi)` 且 `environmentVariables` 无 `isRequired=true`；或 `remotes[0].type=="streamable-http"` 且 `headers` 为空 | 绿色徽标「零配置」，**默认可见** |
| `需密钥` | 任一 `environmentVariables.isRequired`，或 `headers` 非空，或 description 命中 `api key / token / oauth` 的关键词 | 黄色徽标「需密钥」，默认**隐藏**，需在筛选里主动勾选 |
| `需注册` | `remotes` 存在且 url 主机名不含 `localhost / 127.0.0.1`，且 description 命中 `sign up / register / account` | 灰徽标「需注册」，同上 |
| `未知` | 既无 `packages` 也无 `remotes`（Registry 只登记了元数据） | 显示但不给「安装」按钮，只给「查看源」 |

- 筛选下拉默认值：**「零配置可启动」**，用户可切到「全部（含需密钥）」。
- 「全部在线开源免费」的界定口径写进 UI 顶部提示条：「以下目录来自官方 MCP Registry 与 GitHub 公开仓库，均为公开可获取；标注「需密钥」的服务器运行时需要你自己提供 API Key，产品本身不收费、不上传任何密钥。」
- **不采用**「只展示零配置」的方案：会漏掉大量实际有用的服务器（如需要 GitHub Token 的），用户搜不到会认为产品残缺。

### 5.4 按需求默认加载常用集合（决策 6 答案）

**主方案：预置 profile + 关键词匹配（a + b 组合）；降级兜底：系统提示技能清单（c）。**

#### 5.4.1 三层机制

| 层 | 机制 | 触发条件 | 行为 |
|---|---|---|---|
| L1 主 | **预置 profile 关键词命中** | 用户消息文本命中 profile 的 `triggers` 任一关键词（中文子串匹配，大小写不敏感） | ① 自动挂载 profile 的 `skills`（不足 5 个时追加，已满则替换最久未用的）；② 本轮工具白名单 = 内置全启用工具 ∩ profile.tools ∪ 已启用的 MCP 低风险工具；③ 对话流插入一条 `StepLine`（见 5.4.3） |
| L2 次 | **profile 互斥优先级** | 同时命中多个 profile | 取命中关键词数量最多者；相同则取 `priority` 字段高者；不叠加（避免工具集合被放大） |
| L3 降级 | **系统提示技能清单，模型自选** | 未命中任何 profile，且 `auto_load=true` | 把已安装技能（≤ 20 条）以 `- 名称：一句话描述` 写进系统提示的「## 可用技能」段，让模型主动调 `skill_run`；不改工具白名单（就是 P0-5 的默认裁剪结果） |

> **不采用**「让模型输出 `use_skill` 作为主方案」：本地小模型 function calling 不稳定，且 `skill_run` 是 medium 风险要审批，主方案若依赖模型选择，普通用户会先撞上审批卡片，体验断裂。L3 只作为「没命中」时的兜底。

#### 5.4.2 内置 profile（3 个，随包落地，用户可编辑）

| profile id | 名称 | triggers（示例） | 技能 | tools |
|---|---|---|---|---|
| `file-organize` | 文件整理 | 整理、归档、分类、收拾、重命名、移走、清理目录 | `workspace-organizer` | `fs_list, fs_read, fs_info, fs_mkdir, fs_move, fs_glob, skill_run` |
| `code-read` | 代码阅读 | 代码、仓库、项目、分析一下、技术栈、架构、体检、review | `repo-digest` | `fs_list, fs_glob, fs_grep, fs_read, fs_info, skill_run` |
| `web-research` | 网页调研 | 网页、链接、文章、抓取、调研、总结一下这、http、url | `web-brief` | `web_fetch, fs_write, skill_run` |

- 落到 `config.json.profiles`（首次运行由代码写入，之后用户改了不被覆盖）。
- profile 可在「设置 → 技能偏好」查看/启停/编辑（P1-4 支持自定义）。
- `triggers` 为空数组 = 永不自动加载（用户手动禁用某 profile 即置空）。

#### 5.4.3 可见性与可关闭（必须满足）

- **可见**：每次自动加载在对话流插入一条 `StepLine`（复用 `ui/widgets/assistant_cards.py` 现有组件，`_add_step` 已有此能力）：
  `已自动加载：文件整理 · 技能 workspace-organizer · 工具 7 个 ｜[关闭自动加载] [查看]`
  - 同一任务内同一个 profile 只提示一次（避免每轮刷屏）。
  - `StepLine` 不参与模型上下文（纯 UI）。
- **可关闭**：
  1. 点提示里的「关闭自动加载」→ `PUT /settings {"auto_load": false}`，后续轮次不自动加载，状态栏提示「已关闭自动加载（可在设置里重新打开）」。**本次会话也立即失效**。
  2. 设置页有「按需求自动加载常用技能」总开关（默认开）。
  3. `[技能]` 菜单顶部显示「自动加载：开 / 关」一行，可直接切换。
- **可观测**：`/chat` 的 `start` 事件增加 `auto_loaded` 字段：`{"profile": "file-organize", "skills": ["workspace-organizer"], "tools": 7}`，客户端据此渲染 `StepLine`；非流式路径同名字段出现在返回的 `events[0]` 里。

#### 5.4.4 自动加载时序

```
用户发送 "帮我把下载目录整理一下"
   │
   ├─[客户端] POST /chat {session_id, message, mounted_skills}
   │
   ├─[后端 agent.start_turn]
   │   1. profile = match_profiles(text, cfg.profiles)      ← 纯关键词，无网络
   │   2. if profile: 合并挂载技能（profile.skills + mounted_skills，上限 5）
   │      if not profile: skills_hint = list_skills()[:20]  → 写进系统提示
   │   3. registry.openai_schemas() 按 tool_prefs + profile.tools 裁剪到 max_tools
   │   4. emit({"type":"start", ..., "auto_loaded": {...}})
   │   5. 正常工具循环 / 审批
   │
   ├─[客户端] 收到 start.auto_loaded ≠ 空 → _add_step("已自动加载：…")
   │
   └─[客户端] 本任务同一 profile 已提示过 → 静默跳过
```

### 5.5 在线安装流程（技能）

```
用户点「安装」
  → 弹确认框（来源 / ⭐star / 文件数 / 含脚本警示 / 风险=medium）
  → [取消] / [安装]
  → 后端 POST /assistant/catalog/skills/install
       {repo, ref, dir, slug}
     1. 拉 SKILL.md（codeload tarball 整包抽取；先校验 frontmatter 有 name）
     2. 走 codeload.github.com/{owner}/{repo}/tar.gz/{ref} 整包下载
        —— 一次性拿全目录（含二进制 / 大体积），不受旧「纯文本 20 文件 / 2MB」管线限制
     3. 抽取时做安全校验：剥离顶层目录、拒绝 .. / 绝对路径 / 盘符逃逸、拒绝符号链接 / 硬链接
     4. 二进制（svg/wav/png/webp…）原样 bytes 落盘；文本类同前
     5. 写入临时目录 SKILLS_DIR/.installing-<uuid>/
     6. 校验通过后原子 rename 到 SKILLS_DIR/<slug>/
     7. 若 <slug> 已存在：按 overwrite 参数决定覆盖（旧目录先移到 .backup-<ts>）
     8. audit("skill_installed_online", ..., skipped=N)
     9. 失败：删除临时目录，返回错误，不留半成品
  → 客户端刷新清单 + /state，状态栏「已安装 X」
```

---

## 6. 数据模型

### 6.1 `assistant/config.json` 新增字段

```jsonc
{
  // 已有字段省略 ……

  // ── 工具偏好（P0-5）────────────────────────────
  "tool_prefs": {
    "disabled": ["shell_run", "mcp__filesystem__write_file"],  // 按工具名精确禁用
    "max_tools": 24                                           // 下发给模型的上限，8~64
  },

  // ── 自动加载（P0-6）────────────────────────────
  "auto_load": true,
  "profiles": [
    {
      "id": "file-organize",
      "name": "文件整理",
      "enabled": true,
      "priority": 10,
      "triggers": ["整理", "归档", "分类", "收拾", "重命名", "移走"],
      "skills": ["workspace-organizer"],
      "tools": ["fs_list", "fs_read", "fs_info", "fs_mkdir",
                "fs_move", "fs_glob", "skill_run"]
    }
    // code-read / web-research 同构
  ],

  // ── 在线目录（P0-2/P0-3/P0-8）──────────────────
  "catalog": {
    "proxy": "",                 // 空=读环境变量 HTTP(S)_PROXY；P1-5 才暴露到设置页
    "ttl_hours": {"mcp": 24, "gh_repos": 24, "gh_skills": 168},
    "allow_nonfree": false       // 默认只展示「零配置可启动」
  },

  // ── 来源标记（写入技能 frontmatter 之外，单独记录）──
  "skill_sources": {
    "pdf-extract": {
      "kind": "github",
      "repo": "owner/repo",
      "ref": "main",
      "dir": "skills/pdf-extract",
      "commit": "a1b2c3d",
      "installed_at": 1765000000.0,
      "has_scripts": false
    }
  }
}
```

> 兼容性：`load_config()` 的 `cfg.update({k: v for k, v in data.items() if k in cfg})` 要求新字段**必须加进 `_default_config()`**，否则读不到（这是现有实现的硬约束，架构师需注意）。

### 6.2 缓存文件结构

`%LOCALAPPDATA%\llmBuddy\assistant\catalog\mcp_servers.json`
```jsonc
{
  "fetched_at": 1765000000.0,          // 写入时间，判断 TTL
  "cursor": "eyJwYWdlIjoyfQ==",        // 下次翻页游标（null=已到末尾）
  "items": [
    {
      "registry_name": "com.pulsemcp/remote-filesystem",
      "title": "Remote Filesystem",
      "description": "读写 GCS 上的文件…",
      "version": "0.1.2",
      "transport": "stdio",            // 本地归一化结果：stdio | http
      "free_class": "need_key",        // zero_config | need_key | need_register | unknown
      "suggest": {                     // 直接可用的 mcp_servers 配置（不含 id/name）
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "remote-filesystem-mcp-server@0.1.2"],
        "env": {},
        "required_env": [{"name": "GCS_BUCKET", "description": "…"}]
      },
      "raw": { }                       // 上游原对象，留着给详情面板
    }
  ]
}
```

`catalog/gh_skills.json`（按仓库分片，key = `owner/repo@ref`）
```jsonc
{
  "fetched_at": 1765000000.0,
  "repos": {
    "anthropics/skills@main": {
      "items": [
        {"slug": "pdf", "dir": "document-skills/pdf",
         "name": "pdf", "description": "PDF 处理…",
         "files": ["SKILL.md"], "has_scripts": false}
      ]
    }
  }
}
```

`catalog/gh_repos.json`
```jsonc
{
  "fetched_at": 1765000000.0,
  "total_count": 12618,
  "items": [
    {"full_name": "anthropics/skills", "stars": 24100,
     "default_branch": "main", "description": "…",
     "topics": ["claude-skills"]}
  ]
}
```

### 6.3 新增 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/assistant/catalog/mcp?q=&cursor=&force=` | MCP 目录（命中缓存直接返回，`force=1` 强制刷新） |
| GET | `/api/v1/assistant/catalog/skills?q=&force=` | GitHub 仓库列表（`q` 为空走 `topic:claude-skills` 排序 star） |
| GET | `/api/v1/assistant/catalog/skills/{owner}/{repo}?ref=` | 单仓库技能枚举（命中缓存 7d） |
| POST | `/api/v1/assistant/catalog/skills/install` | `{repo, ref, dir, slug, overwrite}`，见 §5.5 |
| POST | `/api/v1/assistant/catalog/mcp/install` | `{registry_name, id, name, env, risk, connect}` |
| POST | `/api/v1/assistant/catalog/refresh` | 三源并行刷新，返回 `{ok, errors[], counts}` |
| GET | `/api/v1/assistant/tools/prefs` | 工具启用清单 + 当前下发数 + 上限 |
| PUT | `/api/v1/assistant/tools/prefs` | `{disabled[], max_tools}` |
| GET/PUT | `/api/v1/assistant/profiles` | profile 列表读写 |

**状态码约定**（沿用现有风格：业务错误 HTTP 200 + `fail(code, msg)`）：

| code | 含义 |
|---|---|
| 2020 | 在线目录不可达（无网络/无代理/超时） |
| 2021 | 上游返回非 200（含 GitHub 403 限流，附 `rate_limit_reset`） |
| 2022 | 技能包结构非法（无 SKILL.md / frontmatter 缺 name） |
| 2023 | 目标 slug 已存在且 `overwrite=false` |
| 2024 | MCP 必填环境变量缺失 |
| 2025 | 安装后连接失败（配置已保留） |
| 2026 | URL 不是受支持的技能地址 |

---

## 7. 边界与异常

| 场景 | 行为 | 文案 |
|---|---|---|
| **离线 / 无代理** | 先渲染缓存；刷新失败顶部提示条常驻 | 「在线目录刷新失败：无法连接（可能需要代理）· 正在显示 08-08 14:20 的缓存 · [重试]」；缓存也没有 → 列表区空态：「还没有在线目录缓存。请检查网络/代理后点「重试」，或先用「导入 zip / 本地文件夹」添加技能」 |
| **代理不可用** | 探测 `httpx.Client(trust_env=True)` 连 `https://api.github.com` 5s 超时 | 「无法连接在线目录。国内网络访问 GitHub / MCP Registry 通常需要代理，请在系统代理中开启后点重试（当前代理：{HTTP_PROXY 值 or 未设置}）」 |
| **GitHub 403 限流** | 30 次/分钟的未授权限额；退避 60s，只读缓存 | 「GitHub 接口限流（未登录每小时 60 次），请 {N} 秒后重试，或先使用缓存」 |
| **安装失败（网络中断）** | 临时目录 `.installing-<uuid>` 整个删除，`SKILLS_DIR` 不留残渣；已有同名技能时旧目录保持在 `.backup-<ts>` 并**自动回滚** | 「安装失败：{原因}，已回滚到安装前状态」 |
| **重名（slug 冲突）** | 确认框：「已存在技能 X（内置 / 在线）。覆盖会替换其全部文件，原技能备份在 `.backup-<ts>」 → [覆盖]/[取消] | — |
| **超大包 / 二进制技能** | v1.0.8 起改为 **codeload tarball 整包下载**：不受 20 文件 / 2MB 限制，支持大体积（实测 PPT Master ≈13030 文件 / 39MB）与二进制（svg/wav/png/webp…）。抽取预算：`ARCHIVE_MAX_FILES=20000` / `ARCHIVE_MAX_BYTES=256MB`（落盘）/`ARCHIVE_MAX_DOWNLOAD=512MB`（按 content-length 预检）；超出则拒收。仍排除 `.git`/`__pycache__`/`node_modules` 等；SKILL.md > 200KB 仍拒收 | — |
| **技能正文含巨长内容** | SKILL.md > 200KB 拒绝 | 「SKILL.md 过大（{n}KB > 200KB），疑似非技能文件」 |
| **URL 不是技能地址** | 校验 host ∈ {`raw.githubusercontent.com`, `github.com`, `gitee.com`, `raw.gitmirror.com`} | 「只支持 GitHub / Gitee 的 SKILL.md 直链（可粘贴网页链接，会自动转 raw）」 |
| **MCP 必填 env 缺失** | 安装对话框逐项标红，确定按钮禁用 | 「该服务器需要 GCS_BUCKET，请填写后再安装」 |
| **MCP 安装后连不上** | 配置保留、`enabled=true`、卡片状态 `×`，详情给 stderr 尾部 8 行 | 「已保存配置但连接失败：{err}。可检查 npx 是否可用、环境变量是否完整」 |
| **npx / uvx 不存在** | 安装前检测 `shutil.which("npx")`；缺失时给「该服务器需要 Node.js，未检测到 npx」+ 官网链接，仍允许保存配置 | — |
| **工具全部被禁用** | 允许，但 `skill_run`/`skill_list` 禁用时对话会失去技能能力；只提示不阻止 | 「全部工具已禁用，助理将无法操作文件。可在 [工具] 里打开」 |
| **max_tools < 内置数量** | 不报错，按优先级裁；被裁掉的在工具 Tab 标灰 + 「超出上限未下发」 | — |
| **profile 技能已被删除** | 匹配时过滤掉不存在的 slug；若全部缺失则 profile 不生效，走 L3 | — |
| **自动加载与手动挂载冲突** | 手动挂载优先保留；profile 技能追加至上限 5，超出则替换本轮最久未用的 | — |

---

## 8. 安全与审计（决策 7 答案）

### 8.1 安装前展示什么（强制）

| 项 | 技能 | MCP |
|---|---|---|
| 来源 | `owner/repo · dir`（可点开浏览器） | Registry `name` + `title` |
| 可信信号 | ⭐ star 数、`has_scripts`（tree 中含 `scripts/` 或出现 `.py/.sh/.ps1`）、文件数、总字节 | 传输类型（远程/本地）、是否需密钥、`packages` 的 registry |
| 风险默认值 | **medium**（技能正文只会作为提示词，但可能引导模型调用高危工具） | **high**（本地子进程 = 任意代码执行） |
| 二次确认 | 一律弹框，标题「从在线来源安装」，黄色警示块 | 同 |

### 8.2 审批策略如何介入

- 安装动作本身**不走** `approval.py`（不是工具调用），走独立确认框 + 审计。
- 安装后的**运行时行为**仍归现有审批：
  - 技能 → 其 `skill_run` 是 medium，`readonly_auto` 策略下每次运行要审批（现状不变）。
  - MCP → 服务器 `risk` 默认写 `high`，其所有 `mcp__<id>__*` 工具在 `readonly_auto` 下逐次审批。
- 自动加载（P0-6）**不得降低**审批要求：profile 只是收窄工具白名单，不改变任何工具的 risk。

### 8.3 审计留痕（写 `audit/audit-YYYYMMDD.jsonl`）

| event | 字段 |
|---|---|
| `skill_installed_online` | `slug, repo, ref, dir, commit, bytes, has_scripts, overwrite` |
| `skill_installed_local` | `slug, from_path` |
| `skill_installed_url` | `slug, url, bytes` |
| `mcp_installed_online` | `server_id, registry_name, version, transport` |
| `mcp_installed_json` | `server_id, keys[]` |
| `catalog_refreshed` | `source, ok, count, error, duration_ms` |
| `catalog_install_failed` | `kind, target, error` |
| `tools_prefs_updated` | `disabled[], max_tools` |
| `auto_load_applied` | `session_id, profile, skills[], tool_count`（用户可关闭提示，但审计照记） |

审计面板（ManagerDialog 审计 Tab）已按 `event` 平铺展示，无需改结构；只需在 `_apply_audit` 的文案里补中文映射（P1）。

### 8.4 失败回滚

1. **技能**：写入 `.installing-<uuid>/` → 校验（有 SKILL.md、frontmatter 有 name、≤ 200KB、文件数 ≤ 20）→ 若目标存在则 `rename` 到 `.backup-<ts>/` → `rename` 临时目录到目标。任一步失败：删临时目录、把 `.backup` 移回原处，状态栏报「已回滚」。
2. **MCP**：先 `add_server(connect=false)` 落配置 → 再 `connect`。连接失败**保留配置**（用户可能只是没填对 env，方便就地修）并标 `×`；用户可在卡片上一键「删除」清干净。
3. **备份清理**：`.backup-*` 保留最近 5 份，启动时清更早的（`skills.py` 已有 `ensure_dirs`，加一个 `prune_backups()`）。

### 8.5 明确的安全红线

- 在线安装**永不自动执行**任何下载到的脚本；只落文件。
- 不允许 `dir` / `slug` 含 `..`、盘符、绝对路径（复用 `skills._SLUG_RE` 与 `commonpath` 校验）。
- MCP `command` 白名单：`npx / uvx / python / node / docker`，其余需用户手动在 `McpDialog` 里填（在线安装路径不放行任意命令）。
- 缓存文件不含任何用户密钥；`required_env` 只记录变量名与说明。

---

## 9. 非目标

1. 不做自建在线市场/服务端（无账号、无上传、无评分）。
2. 不做技能的自动更新（P1-1 只做「有更新」提示，不自动覆盖）。
3. 不实现在线获取「工具」——工具是代码资产，不通过网络分发。
4. 不做工具的脚本化自定义（用户写 Python 工具）。
5. 不改 `approval.py` 的策略模型、不改 `agent.py` 的单轮工具循环结构（只在 `start_turn` 头部加 profile 匹配 + 工具裁剪）。
6. 不改左栏「任务/空间」两区、不改引导会话队列（1.0.9 已交付）。
7. 不做多语言（仅简体中文）。

---

## 10. 待架构确认项

| # | 问题 | 我的倾向（供架构参考） |
|---|---|---|
| A1 | 在线拉取放在哪一层？`routers/assistant.py` 直接调 httpx，还是新建 `core/assistant/catalog.py` 统一收口？ | **新建 `catalog.py`**：三源解析 + 缓存 + TTL + 代理探测集中，路由只做参数校验与错误码映射 |
| A2 | 工具裁剪在哪一层做？`ToolRegistry.openai_schemas()` 内部读 config，还是 `agent.py` 传入过滤后的 registry 视图？ | **在 `ToolRegistry` 上加 `set_filter(disabled, max_tools, allow)`**，`openai_schemas()` 与 `list()` 共用同一过滤；这样 `state()` 的 `tools` 也能带 `enabled` 字段，UI 不用二次计算 |
| A3 | `_build_messages` 的「工具清单」段落（现截断 80 条）与 `openai_schemas()` 必须用同一集合，否则模型会看到不存在于 schema 的工具 | **确认必须同源**；建议 `agent.py` 里取一次 `active_tools = registry.active()` 同时供两处使用 |
| A4 | 自动加载的 profile 匹配在 `start_turn` 里做，是否会与「多技能 chip 拼消息」（客户端 `_send_with_session` 把「用 xx 技能：」写进 message）重复？ | 建议：**客户端只传 `mounted_skills` 列表**（新增 payload 字段），不再把技能名拼进 message 正文，避免正文污染历史；同时保留拼串作为旧客户端兼容（后端识别到 `mounted_skills` 空但正文有「用 xx 技能：」前缀时忽略） |
| A5 | MCP 在线安装生成的 `id` 如何保证唯一且可读？Registry `name` 形如 `com.pulsemcp/remote-filesystem` 含 `.` 和 `/` | 建议 `_slugify(name)`（复用 skills 的同名函数）→ `com-pulsemcp-remote-filesystem`；冲突时追加 `-2` |
| A6 | 缓存刷新是同步阻塞还是后台线程？首屏渲染必须 ≤ 2s | 建议：**GET catalog 一律先返缓存**，`force=1` 时才同步拉取（5s 超时）；「刷新」按钮走后端后台线程 + 客户端轮询两次（2s / 6s） |
| A7 | 680px 窗口 + 200% 缩放的实际可用性需要实机验证 | 建议架构/工程师在 `client/ui_tests` 加一个「清单中心最小尺寸快照」用例，或由 QA 在 200% 缩放实机跑一遍 |
| A8 | `.backup-*` 与 `.installing-*` 目录会被 `list_skills()` 遍历到（它是 `os.listdir(SKILLS_DIR)` + 找 SKILL.md） | 建议：`list_skills()` 增加前缀跳过规则（`.` 开头一律跳过），并在 PR 里注明 |

---

## 附：P0 工作量估算与切分（供架构拆任务）

| 任务 | 内容 | 预估 |
|---|---|---|
| T01 | `core/assistant/catalog.py`：三源客户端 + 缓存 + TTL + 归一化（Registry→MCP 配置、tree→技能列表、URL→raw） | 1.5d |
| T02 | `routers/assistant.py` 新增 catalog / tools/prefs / profiles 路由 + 错误码 | 0.5d |
| T03 | `tools.py` 过滤（`set_filter`/`active()`）+ `agent.py` 同源裁剪 + `store.py` 新配置字段 | 0.5d |
| T04 | `profiles` 匹配 + `auto_load` + `start.auto_loaded` 事件（`agent.py`） | 0.5d |
| T05 | `skills.py`：本地文件夹导入 / URL 安装 / 原子写入 + 回滚 + `.backup` 清理 | 1d |
| T06 | 客户端：`ManagerDialog` 技能 Tab 改造（列表+详情+搜索+筛选） | 1.5d |
| T07 | 客户端：四入口菜单改造（`[+]` / `[技能]` / `[工具]` / `[MCP]`）+ 工具 Tab 开关 | 1d |
| T08 | 客户端：清单中心的「在线」区块（缓存态、提示条、安装确认框） | 1d |
| T09 | 客户端：自动加载 StepLine + 关闭开关 | 0.5d |
| T10 | 单测（catalog 解析/缓存/TTL/裁剪/profile/URL）+ 两套回归 | 1d |
| | **合计** | **约 9d** |

> 若单迭代（5 个工作日 × 1 人）吃不下，**建议按此顺序砍**：先交付 T01+T02+T03+T05+T07+T10（≈5d，满足「底部手动添加本地+在线」与「工具开关」），T04 自动加载与 T06/T08 清单中心 UI 精细化顺延到下一个 patch。**但用户原话的需求 ②（自动加载）属于 P0，若只能砍一处，砍 T06 的右侧详情面板，保 T04。**
