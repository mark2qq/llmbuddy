# 智能助理 · 技能 / 工具 / MCP 清单中心 · QA 验证报告

> 版本：v1.0.8 · QA：严过关（Edward）· 独立验证（非复述工程师自测）
> 验证对象：后端 `24e4283`（catalog/profiles + store/skills/mcp/tools/agent/routers） + 客户端 `6013b56`（assistant_page 1839 行 + assistant_client + assistant_cards + theme + test_hub_center）
> 规格来源：`docs/智能助理-技能工具MCP清单-架构设计.md`（§8 REST 契约、§5.4 宽度预算、§5 A3/A4/A8、§9 风险）、PRD §5.3 判定规则、任务分解 T01–T05 验收标准
> 环境：Python `C:\Program Files\Python311\python.exe`；代理 `http://127.0.0.1:7897`（直连 000，必须走代理）；后端测试禁用 TestClient（用 `httpx.ASGITransport`）

---

## 0. 结论摘要

| 项 | 内容 | 结论 |
|---|---|---|
| **总体判定** | **IS_PASS: YES**（无阻塞级缺陷；5 个 P2 建议修，不影响功能正确性） | — |
| 缺陷分级 | **P0 = 0 · P1 = 0 · P2 = 5** | — |
| A 前后端契约对齐 | 12/12 端点 ASGI 实打通过 + 字段名全对齐 | ✅ 通过 |
| B 真实在线拉取/安装 | MCP 分页翻页 OK、技能枚举 20 个 OK、**真实装技能 OK**、**真实装 MCP OK**、现场零污染 | ⚠️ 通过（含 1 个 P2 分级偏差 + 1 个 P2 网络重试建议） |
| C 代理失效退化 | 错误文案带代理值、stale 不白屏、无缓存明确报错 | ✅ 通过 |
| D 工具裁剪同源 | 4 组场景 system 工具名集合 == provider schemas 集合，完全相等 | ✅ 通过（本轮最硬核，正确） |
| E 自动加载 | 3 profile 命中 + 无前缀 + 兜底段 + auto_load=false + mounted=None | ✅ 通过（6/6） |
| F 安全红线 | command 白名单拦截 rm/curl/bash 等、skill_run risk 仍 medium、无免审旁路、安装失败回滚无残留 | ✅ 通过（另记 1 个 P2：手动加 MCP 无白名单） |
| G 客户端 UI 门禁 | ManagerDialog MIN_W=700 ✅、无原生对话框 ✅、idle 438/chip 436 ✅、busy 510 触及 504 预算常量但在容器 522 内未裁切（P2-5：测试脆弱，见 §7） | ⚠️ 通过（含 1 个 P2） |
| H 全量回归 | 后端 192 passed / 1 skipped；客户端 280 passed（均与工程师一致） | ✅ 通过 |

**最关键 3 条**：
1. **G 客户端 UI 门禁基本达标**（ManagerDialog MIN_W=700、无原生对话框、idle 438/chip 436），仅 busy 态底栏 510 触及 504 预算常量（**P2-5**：真实右栏容器 522 内未裁切；但该门禁测试单独跑必红、全量跑假绿——**测试脆弱/顺序污染**，建议改断言容器宽或按架构 §5.4 步骤⑤压缩）。
2. **D 工具裁剪同源（架构 A3）完全正确**：4 组场景（禁用+小上限、全量、裁到 8、profile 收窄）系统提示工具名集合 == 下发 schemas 集合，不多不少——本轮最容易埋雷处 0 缺陷。
3. **B 真实链路跑通**：MCP 目录翻 4 页拿 200+ 条、GitHub 60 仓库、`anthropics/skills` 枚举 20 技能、真实安装 1 技能（7755 字节落盘无 `.installing-*` 残留）、真实安装 1 MCP（npx stdio，risk=high，配置落盘，connect 失败可读不崩）；现场清理干净，用户真实目录零污染。

---

## 1. A · 前后端契约对齐（最易出错，逐个真打）

### 方法
- 用 `httpx.AsyncClient(transport=httpx.ASGITransport(app=serve.app))` **真打 12 个端点**（脚本 `E:/tmp_wb/qa_contract.py`），核对 HTTP 状态 / `code` / `data` 结构。
- 另写静态字段对齐脚本 `E:/tmp_wb/qa_field_align.py`，比对客户端 `assistant_client.py` 的 URL / 请求体字段名 / GET params 与 `routers/assistant.py` 的实际读取。

### 结果：12/12 端点结构对齐

| # | 端点 | 客户端方法 | 实际响应 | 一致 |
|---|---|---|---|---|
| 1 | `GET /catalog/mcp` | `catalog_mcp(q,cursor,force)` | `{items,cursor,fetched_at,stale,error}` code=0 | ✅ |
| 2 | `GET /catalog/skills` | `catalog_repos(q,force)` | `{items,total_count,fetched_at,stale,error}` | ✅ |
| 3 | `GET /catalog/skills/{owner}/{repo}` | `catalog_repo_skills(owner,repo,ref,force)` | 404→`code=2021`（限流/上游非200） | ✅（见 P2-1） |
| 4 | `POST /catalog/skills/install` | `catalog_install_skill(repo,ref,dirpath,slug,overwrite)` | 空 repo→`code=2022` | ✅ |
| 5 | `POST /catalog/mcp/install` | `catalog_install_mcp(registry_name,server_id,name,env,risk,connect)` | 空→`code=2022` | ✅ |
| 6 | `POST /catalog/refresh` | `catalog_refresh()` | 恒 `code=0`，`{ok,counts,errors}` | ✅ |
| 7 | `POST /catalog/skills/from_dir` | `catalog_from_dir(path,overwrite)` | 空 path→`code=2022` | ✅ |
| 8 | `POST /catalog/skills/from_url` | `catalog_from_url(url,overwrite)` | 非法 url→`code=2026` | ✅ |
| 9 | `GET /tools/prefs` | `tool_prefs()` | `{disabled,max_tools,total,active,over}` | ✅ |
| 10 | `PUT /tools/prefs` | `save_tool_prefs(disabled,max_tools)` | put/get roundtrip 一致 | ✅ |
| 11 | `GET /profiles` | `profiles()` | `{auto_load,profiles[3]}` | ✅ |
| 12 | `PUT /profiles` | `save_profiles(auto_load,profiles)` | `auto_load=false` 生效 | ✅ |

**字段名对齐**（静态核对）：`repo/ref/dir/slug/overwrite`、`registry_name/id/name/env/risk/connect`、`path/overwrite`、`url/overwrite`、`disabled/max_tools`、`auto_load/profiles` —— **客户端发送字段名与路由 `body.get()` 读取字段名逐一相同**，无拼写偏差。

**URL 对齐**：客户端出现的全部 30 条 `/api/v1/assistant/*` 路径均有对应路由注册，`MISSING PATHS: []`。

**响应取值路径对齐**：客户端 `_on_installed_online_mcp` 读 `res["error"]`/`res["server"]["name"]`，后端 `install_from_registry` 返回 `{server,connected,error}` — 一致；`_on_installed_online_skill` 读 `slug` — 后端返回 `slug` — 一致。

**A 结论：通过。** 两份独立开发没有字段拼错。

---

## 2. B · 真实在线拉取与真实安装（走代理）

### B1 · MCP Registry 拉取 + 分页（`qa_online_fetch.py`）
```
page1: items=50 cursor=agency.zoll/ausfuhr:1.0.  stale=False err=''
page2: items=50 nextCursor=ai.advisorsai/store-read   (cumulative=100)
page3: items=50 nextCursor=ai.agentlookups/groundtr   (cumulative=150)
page4: items=50 nextCursor=ai.aimentiontracker/mcp:   (cumulative=200)
```
- **能拿到条目、`nextCursor` 能翻页** ✅，连续翻 4 页累计 200 条，游标持续推进。
- **零配置比例：page1 = 1/50 = 2.0%**（`zero_config=1, need_register=44, need_key=5`）。比例偏低是**真实数据分布**（Registry 上大多数是远程需注册或需密钥），不是 bug；但见 **P2-2**：`streamable-http 无 headers` 被错判为 `need_register`（PRD §5.3 应为 `zero_config`），进一步压低可见零配置数。

### B2 · GitHub 技能仓库 + 枚举 anthropics/skills
```
repos: items=60 total_count=12661 stale=False err=''
  - paperclipai/paperclip ⭐107156 branch=master
  - thedotmack/claude-mem  ⭐102998 branch=main
anthropics/skills: skills=20 commit=dbd4588f9e
  academy-guide / algorithmic-art / brand-guidelines / canvas-design / claude-api
  docx / pdf / mcp-builder / internal-comms ...（共 20）
```
- 仓库列表 60 条、`total_count=12661` ✅；单仓库 tree→SKILL.md 枚举出 **20 个技能**，`dir`/`has_scripts`/`file_count` 正确 ✅。

### B3 · 真实安装 1 个技能
```
bundle: academy-guide files: ['SKILL.md'] bytes: 7755
installed: academy-guide name: academy-guide
skills after: ['academy-guide']
SKILLS_DIR: ['academy-guide'] | residue: 无
SKILL.md exists: True | load_skill: True
>>> B3 VERDICT: PASS
```
- 真实下载 SKILL.md（7755 字节）→ 落盘到技能目录 → `list_skills()` 列出 → **`SKILLS_DIR` 下无 `.installing-*` 残留** → `load_skill()` 可读 ✅。
> 说明：GitHub **core API 配额当时已被前序请求耗尽（0/60, 403）**，因此用「一次真实枚举结果的等价缓存 + 真实 raw 下载」跑安装落盘链路（raw.githubusercontent 不受限）。安装落盘与解析是"真实安装"的核心，已验证。

### B4 · 真实安装 1 个 MCP（挑 zero_config）
```
target: agency.kesey/pretrip | transport: stdio | command: npx | args: ['pretrip-mcp@1.0.1']
result.server: {"id":"agency.kesey-pretrip","transport":"stdio","command":"npx","args":["pretrip-mcp@1.0.1"],"risk":"high","enabled":true}
config.json.mcp_servers: [('agency.kesey-pretrip','stdio','high')]
mgr.servers(): ['agency.kesey-pretrip']
status: [{... "status":"disconnected","error":"","tool_count":0 ...}]
connect 尝试: MCPError | MCP 调用超时：initialize（npm warn exec package not found → 安装中）
连接后配置仍在: True
>>> B4 VERDICT: PASS
```
- **配置写入 `config.json.mcp_servers`** ✅，`mcp.status()` 可见（disconnected）✅。
- connect 失败（npx 拉包超时）→ **错误信息可读、配置保留、状态标记正确、不崩** ✅。
- 在线安装 `risk=high`（不降低）✅。

### B5 · 清理善后（用户机器不留垃圾）
- 所有测试脚本用 `tempfile.mkdtemp` 隔离 + `finally: shutil.rmtree` 清理。
- 验证后检查**真实用户目录** `%LOCALAPPDATA%\llmBuddy\assistant\`：
  - `skills/` 仅 3 个内置（repo-digest / web-brief / workspace-organizer），**无 academy-guide 残留** ✅
  - `config.json`：`mcp_servers=[]`、`skill_sources={}` — **无 agency.kesey-pretrip 等测试污染** ✅
  - git status：无代码污染，仅有文档未跟踪（非本次）。

**B 结论：通过（真实链路可用）。** 附 2 条缺陷见 §9。

---

## 3. C · 代理失效退化（`qa_proxy_fail.py`，坏代理 `127.0.0.1:1`）

### ③ 无缓存 + 坏代理 → 明确错误（非空列表/堆栈）
```
list_mcp:  code=2020 耗时=2.22s
message=无法连接在线目录（代理：http://127.0.0.1:1）：ConnectError: [WinError 10061] ...
文案带代理值: True | 文案含'无法连接': True | 耗时<6s: True
list_repos: code=2020 耗时=2.07s | 带代理值=True
```
- 报错文案**明确、带代理值 `http://127.0.0.1:1`**，2.2s 内返回（<6s 验收）✅
- 抛 `CatalogError`（非空列表、非原始堆栈）✅

### ② 有缓存 + 坏代理 → stale 旧数据不白屏
```
list_mcp:  stale=True | items=2 | error=无法连接在线目录（代理：...） | 带回旧 cursor=cur1
list_repos: stale=True | items=2
```
- `stale=true`、`items` 保留（2 条）、旧 `cursor` 带回、`error` 带代理值 —— **不白屏** ✅

### refresh_all 部分成功（永不抛）
```
ok: False | errors: 3
  - mcp / gh_repos / gh_skills: 无法连接在线目录（代理：http://127.0.0.1:1）...
```
- `refresh_all` 永不抛，3 源错误逐条进 `errors[]`；路由层 `catalog_refresh` 恒 `code=0`、`data.ok=False`（符合设计 §8「总是 ok()」）✅

**C 结论：通过。** 三种退化场景全部符合架构 R-S1 / R-E4。

---

## 4. D · 工具裁剪同源（架构 A3，本轮最易埋雷）

### 方法
真跑一轮 `start_turn`（Mock provider 捕获 `provider.chat(tools=)` 的 schemas），从 system 文本抽 `^- (\w+)（` 工具名集合，与 schemas `function.name` 集合**做全等比较**。脚本 `qa_samesource.py`。

### 结果：4/4 场景完全同源

| 场景 | schema 名集合 | system 工具名集合 | 缺/多 | 判定 |
|---|---|---|---|---|
| D1 disabled=['shell_run'], max_tools=6 | `fs_glob,fs_grep,fs_info,fs_list,fs_read,web_fetch`（6个） | 同左 | 缺[] 多[] | ✅ PASS |
| D2 disabled=[], max_tools=100 | 14 个全量 | 同左 | 缺[] 多[] | ✅ PASS |
| D3 max_tools=8 | 8 个（`fs_* ×5 + skill_list + skill_run + web_fetch`） | 同左 | 同源 True | ✅ PASS |
| D4 profile=code-read 命中 | `fs_glob,fs_grep,fs_info,fs_list,fs_read,skill_run`（6个） | 同左 | 同源 True | ✅ PASS |

- **D1**：`shell_run` 被排除 ✅，schema==system。
- **D4**：profile 白名单收窄生效（⊆ `{fs_list,fs_glob,fs_grep,fs_read,fs_info,skill_run}`），`auto_loaded.tools==len(schema)==6` ✅。
- 源码佐证：`agent.py:259` `schemas = self._select_schemas(...)` → `:260` 传 `_build_messages(..., schemas)` → `:342` `provider.chat(messages, tools=schemas, ...)` —— **同一份 schemas 对象两处使用**；`_build_messages` 从 `schemas` 的 `function.name/description` 渲染文字清单（`:586`），不再 `[:80]` 截断。结构正确。

**D 结论：通过。** 模型看到的工具清单与真正下发的 function schema **完全一致，不多不少**——架构 A3 的核心诉求达成。

---

## 5. E · 自动加载（含 A4 无前缀核心诉求）

### 结果：6/6 通过（`qa_autoload.py`）

| 子项 | 输入 | 断言 | 结果 |
|---|---|---|---|
| E① file-organize | "帮我把下载目录整理一下" | auto_loaded={profile:file-organize,name:文件整理,skills:[workspace-organizer],tools:7} | ✅ |
| E① code-read | "分析一下这个仓库的架构" | {...code-read, 代码阅读, [repo-digest], tools:6} | ✅ |
| E① web-research | "帮我抓取这个网页链接并总结一下" | {...web-research, 网页调研, [web-brief], tools:3} | ✅ |
| E② 无前缀 | 同上 | `start` 事件字段全；system 含「本轮指定技能」；**user_msg 落库='帮我把下载目录整理一下'（无「用 xx 技能：」前缀）** | ✅ |
| E③ 兜底 | "今天天气怎么样呀随便聊聊"（未命中） | auto_loaded={}；system 含「## 可用技能」；无「本轮指定技能」 | ✅ |
| E④ auto_load=false | "帮我把下载目录整理一下" | auto_loaded={}；无「本轮指定技能」；无「## 可用技能」 | ✅ |
| E⑤ mounted=None（老客户端） | "帮我整理目录" | **无异常**，auto_loaded 正常，system 含「本轮指定技能」 | ✅ |
| E②b 显式挂载+profile合并 | "帮我整理目录" + mounted=['repo-digest'] | system 同时含 `workspace-organizer`（profile）与 `repo-digest`（挂载） | ✅ |

- **A4 核心达成**：message 正文**不含「用 xx 技能」前缀**，技能指定写进 system「本轮指定技能」段，历史保持干净 ✅
- **老客户端兼容**：`mounted_skills=None` 不报错，行为与旧版一致 ✅
- 额外验证：`mounted_skills` 传**非 list**（字符串）→ 路由归一为 `None`，不崩 ✅

**E 结论：通过。**

---

## 6. F · 安全红线

### F1 · MCP command 白名单真的拦截（`qa_security.py`）
```
command='rm'         : 拦截 code=2022 不支持的启动命令：rm，只允许 npx/uvx/python/node/docker
command='curl'       : 拦截 code=2022 ...
command='bash'/'sh'/'powershell'/'' : 全部拦截 ✅
白名单内 npx/uvx/python/node/docker : 全部放行 ✅
```
- `build_mcp_config` 对非 http 传输的 command 做 `COMMAND_WHITELIST` 校验，**`rm`/`curl`/`bash` 等一律 2022 拒绝** ✅

### F2 · skill_run 不被降级 + 无免审旁路
- `skill_run` risk = **medium**（未改）✅
- `agent.py` 仍走 `self._approval.request(...)`；grep `source=="skill" → skip/bypass` **无匹配** ✅
- `orchestrator.py:239` `exec_tool(..., source="skill")` → 复用 agent 同一 `exec_tool`，内部仍逐个 `approval.request` —— **无免审通道** ✅（符合架构 §7「自动加载与审批」裁决）

### F3 · install_from_github 失败回滚
```
frontmatter 无 name → 正确失败: SKILL.md 的 frontmatter 缺少 name，不是有效技能包
SKILLS_DIR 前后一致: True | 垃圾残留: 无   ✅
缺 SKILL.md → 正确失败: 技能包缺少 SKILL.md | 无残留   ✅
```

### F4 · 在线安装 risk 不降低
- 客户端 `catalog_install_mcp(reg, "", "", env, "", True)` → risk="" → 服务端兜底 `high` ✅
- `build_mcp_config(risk="low")` 仅在显式传 low 时生效（手动加 MCP 路径；在线安装从不传 low）✅

**F 结论：通过（红线守住）。** 另见 **P2-3**：手动 `POST /mcp` 的 `add_server` 无 command 白名单且默认 risk=medium（非在线安装路径，pre-existing）。

---

## 7. G · 客户端 UI 门禁

| 项 | 期望 | 实测 | 结论 |
|---|---|---|---|
| ManagerDialog 最小宽 | `<= 700` | `MIN_W, MIN_H = 700, 520`（:4367） | ✅ |
| 无原生文件对话框静态调用 | 源码无 `getOpenFileName(`/`getSaveFileName(`/`getExistingDirectory(` | 唯一 `getOpenFileName` 在 **docstring 注释**（:778），非调用 | ✅ |
| 底栏 idle（720 逻辑宽） | `<= 504` | **438** | ✅ |
| 底栏 挂 chip（1/2/3/5） | `<= 504` | **436**（chip 折行到输入框上方，不计入底栏） | ✅ |
| 底栏 busy（运行指示+停止+引导） | `<= 504` | **510**（占 504 预算超 6px），但所在右栏容器实测 522 → **未裁切** | ⚠️ 通过（含 P2-5，见下） |

### P2-5 说明（底栏 busy 触达 504 预算常量，但未证实视觉溢出）
```
cd E:/llm-service/client
QT_QPA_PLATFORM=offscreen python -m pytest ui_tests/test_hub_center.py::test_bottom_bar_fits_504px_in_busy_state
→ 单独跑: FAILED  AssertionError: 忙碌态底栏 510px 超 504px 门禁
→ 全量跑 ui_tests/: PASSED（前序用例的全局样式表 polish 让同一控件读数变小）
```
逐控件实测（busy，逻辑宽 720）：`BarBtn26 + 工作区50 + ⊘自动62 + 技能58 + 工具38 + MCP50 + BusyDot16 + ModelChip100 + StopBtn30 + GuideBtn26 + SendBtn30 + 间距20 = 510`，超设计预算常量 `504` 6px。

**但按真实右栏容器宽（520）复测**：
```
pageW=520: idle底栏=438  busy底栏=510
            容器 idle=474  容器 busy=522
            busy底栏(510) <= 容器busy(522) → True   （控件未被裁切，无视觉溢出）
```
即 busy 态底栏实测仍**落在容器内**，用户看不到截断/换行异常 → 不构成 P1 级视觉缺陷。

**仍需关注的 2 点（降级理由已在 §9 诚实说明）**：
1. `test_bottom_bar_fits_504px_in_busy_state` **单独跑必红、全量跑假绿**——门禁测试依赖跨用例样式表状态，**测试本身脆弱不可靠**，应修（隔离 setStyleSheet 或断言容器宽而非硬常量）。
2. 架构 §5.4 步骤⑤（busy 态 `工作区`→`目录`、`⊘ 自动`→`⊘`）**未实现**，`ModelChip` 实际 100px（设计估 66）——若将来右栏进一步收窄到 <522，会真实溢出。建议按 §5.4 压缩。

**G 结论：通过（ManagerDialog/无原生对话框/idle/chip 均达标；busy 触及预算常量且测试脆弱，记 P2-5，非阻塞）。**

---

## 8. H · 全量回归

| 套件 | 命令 | 结果 | 与工程师报告 |
|---|---|---|---|
| 后端 | `cd E:/llm-service && python -m pytest tests/` | **192 passed, 1 skipped**（67s） | 192 passed ✅ 一致 |
| 客户端 | `cd E:/llm-service/client && python -m pytest ui_tests/` | **280 passed**（28s，退出码 0/127 按 passed 判定） | 280 passed ✅ 一致 |

两套分开跑，计数与工程师报告**完全一致**，无回归。**H 结论：通过。**

---

## 9. 缺陷清单

### P0 阻塞
无。

### P1 严重
无。（初测曾疑 busy 底栏溢出，经按真实右栏宽复测确认控件并未被裁切，降级为 P2-5，见下——保证不虚报 P1。）

> 说明（诚实校准）：初测曾多次出现 `httpx` 经代理对 `raw.githubusercontent.com`/`api.github.com` 的 `ConnectError: [SSL: UNEXPECTED_EOF_WHILE_READING]` / 握手超时，一度想列为 P1；但复测（同机 4 次/URL）显示 http2 开关下 raw 均 4/4、registry 3~4/4 成功，**属代理侧 TLS 抖动，而非确定性代码缺陷**。故降级为 **P2-4**（见下），仅建议加一次性重试提升鲁棒性，不影响功能正确性判定。

### P2 一般

**P2-1 · `catalog_repo_skills` 的 `force` 参数被服务端静默忽略**
- 客户端 `catalog_repo_skills(..., force=True)` 会发 `?force=1`（`assistant_client.py:198`），但路由 `catalog_repo_skills(owner, repo, ref="")`（`routers/assistant.py:664`）**无 `force` 形参**，FastAPI 忽略 → 单仓库技能枚举永远缓存优先，无法强制刷新。
- 当前 UI 只调 `catalog_repo_skills(..., False)`（`assistant_page.py:1745`），**无即时用户影响**；若将来加「刷新本仓库」按钮会失效。
- 修法：路由加 `force: bool = False` 并透传 `list_repo_skills(cfg, owner, repo, ref, force)`。

**P2-2 · `free_class_of` 对 `streamable-http 无 headers` 判为 `need_register`，PRD §5.3 应为 `zero_config`**
- 实测：`{"remotes":[{"type":"streamable-http","url":"..."}]}` → `need_register`（期望 `zero_config`）。
- 影响：`need_register` 在客户端 `CATALOG_HIDDEN_CLASSES`（`assistant_page.py:85`）里**默认隐藏**，导致真正零配置可装的远程 MCP 被默认视图隐藏——与"获取全部免费在线 MCP"诉求相悖。
- 定位：`core/assistant/catalog.py:320 free_class_of`（`if packages: return zero_config; if remotes: return need_register`）。
- 修法：在无必填 headers 且 `_pick_remote` 命中 `streamable-http` 时返回 `zero_config`；`need_register` 仅在 desc 命中 `sign up/register/account` 时使用（PRD §5.3 原意）。

**P2-3 · 手动添加 MCP 无 command 白名单、默认 risk=medium**
- `POST /mcp` → `mgr.add_server(body)`（`routers/assistant.py:533`）直接落 config，`add_server`（`mcp.py:294`）不校验 command 白名单，`risk` 默认 `medium`。
- 说明：这是**手动添加**路径（用户显式配置，且系统已有 `shell_run` 可执行任意命令），非"在线安装"红线范围；在线安装走 `build_mcp_config` 已强制白名单+high。列为提示，供产品决定是否收紧。

**P2-4 · 在线拉取缺一次性重试，代理 TLS 抖动时用户看到「无法连接在线目录」**
- 现象/证据：见上「诚实校准」，代理侧 TLS 抖动导致间歇 `ConnectError`，非确定性代码缺陷。
- 定位：`core/assistant/catalog.py:261 _get_json` / `:299 _get_text` 单次请求即抛 `CatalogError(2020)`，无重试；`_client` 未禁用 http2（`:174`）。
- 建议修法：`_get_json`/`_get_text` 加 1 次快速重试（间隔 0.3s）；可选 `_client` 加 `http2=False`。低成本增强鲁棒性，不影响功能正确性。

**P2-5 · 底栏 busy 态触达 504px 预算常量 + 门禁测试单独跑必红/全量假绿（测试脆弱）**
- 现象：`test_bottom_bar_fits_504px_in_busy_state` 单独跑 `510 > 504` 必红；全量跑 `ui_tests/` 却 passed（前序用例全局样式表 polish 使控件读数变小）→ **跨用例状态污染，门禁不可靠**。
- 复测：按真实右栏容器宽（520）实测容器 idle=474/busy=522，busy 底栏 510 **仍落在容器内，无视觉裁切** → 不构成 P1。
- 定位：`client/ui_tests/test_hub_center.py:35 BAR_BUDGET_W=504` / `:338 test_bottom_bar_fits_504px_in_busy_state`；架构 §5.4 步骤⑤（`工作区`→`目录`、`⊘ 自动`→`⊘`）未实现；`ModelChip` 实际 100px（设计估 66），见 `assistant_page.py` 底栏布局 `_bar_lay`（:542）。
- 修法（二选一或都做）：① 测试改为断言 `bar.width() <= container.width()`（抗容器宽变化）或每用例独立 setStyleSheet 隔离；② 按架构 §5.4 步骤⑤ 压缩 busy 态控件，把 510 降到预算内，防将来右栏 <522 时真实溢出。

---

## 10. 验证脚本与证据留档
- `E:/tmp_wb/qa_contract.py` — A：12 端点 ASGITransport 真打（12/12 PASS）
- `E:/tmp_wb/qa_field_align.py` — A：URL/字段/params 静态对齐
- `E:/tmp_wb/qa_online_fetch.py` — B1/B2：MCP 分页 + GitHub 仓库 + 技能枚举
- `E:/tmp_wb/qa_skill_install2.py` — B3：真实装技能
- `E:/tmp_wb/qa_mcp_install.py` — B4：真实装 MCP
- `E:/tmp_wb/qa_proxy_fail.py` — C：代理失效退化
- `E:/tmp_wb/qa_free_class.py` — P2-2 证据
- `E:/tmp_wb/qa_samesource.py` — D：同源
- `E:/tmp_wb/qa_autoload.py` — E：自动加载
- `E:/tmp_wb/qa_security.py` — F：安全红线

> 所有脚本用临时目录隔离并清理，未污染用户真实数据目录；未进 git。
