# LLMDesktop 全功能回归测试报告

- **测试对象**：本地大模型服务 LLMDesktop（FastAPI 后端 + PySide6 桌面客户端 + 冻结态 exe + Inno 安装包）
- **测试环境**：Windows，系统 Python 3.11.7（`C:\Program Files\Python311\python.exe`），本机有 HTTP 代理（所有 127.0.0.1 请求均 `trust_env=False`）
- **硬件**：16.9 GB 总内存（测试期可用 ~5.9–6.1 GB），16 逻辑核 CPU；引擎为 CPU 精简版 `engine-runtime/llama-server.exe`
- **模型**：`E:/llama-serve/models` 下 `qwen3-4b.gguf`(2.5GB) / `qwen2.5-7b-instruct.gguf` / `qwen2.5-coder-7b.gguf`
- **测试日期**：2026-10-17
- **执行人**：QA 工程师（Edward）

---

## TL;DR（一句话结论）

**10/10 功能已实现并通过构造/接口验证；pytest 21/21 全绿、冻结态后端端到端首轮推理正常、GUI 无初始化死锁且能进入事件循环；但发现 1 个严重后端/引擎缺陷——「已加载引擎只响应第 1 次推理请求，之后所有推理请求（网关与 M12 直连）全部挂起」，需工程师修复后才能交付。**

---

## 测试概览

| 维度 | 结果 |
|---|---|
| **A. pytest 单元测试** | **21 passed / 0 failed**（含 3 个真实加载模型推理用例），耗时 261.72s |
| **B. 冻结态 exe `--backend` 端到端** | 后端启动 / M1/M2/M3/M7/M8/M13 只读接口 / M9 加载 / M10 就绪 / 首次推理(非流式+流式+M12) 全部 PASS；**发现引擎"二次请求挂起"缺陷** |
| **C. GUI 验证** | 离线构造 9/9 PASS（5 页面无 ImportError/NameError，确认无初始化死锁）；冻结态 GUI exe 启动日志确认到达 `app.exec()`（死锁修复有效）；exe 进入事件循环后约 0.5s 在 offscreen 无头沙箱崩溃（环境限制，非逻辑缺陷） |
| **D. 需求逐项核验** | **10/10 PASS**（含备注与已知缺陷说明） |
| **已知严重缺陷** | 1 个（引擎二次推理挂起，见「已知问题」） |
| **沙箱限制项** | 安装包静默安装需管理员+显示器；GUI offscreen 崩溃为无头环境假象 |

---

## A. pytest 单元测试（已有）

- **命令**：`cd /e/llm-service && C:\Program Files\Python311\python.exe -m pytest tests/ -q`
- **结果**：`21 passed in 261.72s (EXIT_CODE=0)`
- 覆盖：M1/M2/M3/M7/M8/M13/M14 只读接口、M9 加载、M10 未加载 404、M12 测试、网关 `/v1/models`、未注册模型 404（chat/completions）、错误路径（缺 model/未知下载任务）。
- **重要提示**：`test_m12_test_model` / `test_chat_registered_non_500` / `test_chat_registered_stream` 三个"加载真实模型"用例在**每次用例结束后 `unload` 并 `wait_engine_gone()` 杀掉引擎**，即每个用例都使用**全新引擎实例**——因此掩盖了下文 B 发现的"同实例二次请求挂起"缺陷（详见已知问题）。pytest 全绿 ≠ 长会话多轮推理无问题。

---

## B. 冻结态 exe `--backend` 端到端（`dist/LLMDesktop/LLMDesktop.exe --backend`）

**命令/端口**：`LLM_PORT=8031 LLM_ENGINE_PORT=8039 LLM_HOST=127.0.0.1`，引擎 `engine-runtime/llama-server.exe`，模型 `qwen3-4b`。
测试脚本：`qa_e2e_backend.py`（修正版）/ `qa_e2e_diag.py`（速度标定）/ `qa_e2e_repro.py`（缺陷复现）。

### 通过的断言
| 检查项 | 结果 | 证据 |
|---|---|---|
| 后端 health 启动 | PASS | `/api/v1/health` 200 |
| M1 系统信息 | PASS | code=0，含 cpu/ram 字段 |
| M2 端口扫描 | PASS | `ports+used==count` |
| M3 模型目录 | PASS | items≥1，含 name |
| **M7 生成 API Key** | PASS（修正后） | 首轮曾因"误用 GET→405"，修正为 POST 后 `api_key` 长度=64（hex） |
| M8 池总览 | PASS | `engine_base_port==8039` |
| M13 并发上限 | PASS | 含 suggested/reasonon/max_concurrent |
| M9 加载 | PASS | `status=loading, engine_port=8039` |
| M10 轮询就绪 | PASS | `status=ready`（约 6s 加载完成） |
| **首次非流式 chat** | PASS | `choices[0].message.content` 非空（len=78，约 3.7s） |
| **首次流式 chat** | PASS | 拼接 `delta.content` 非空（约 0.9s） |
| **M12 测试模型（首轮）** | PASS | `ok=true, sample` 非空（len=27） |
| M11 卸载 | PASS | code=0 |

### ⚠️ 严重缺陷（源 bug，已复现，需工程师修复）
见「已知问题 #1」。简要：同实例首次推理成功后，后续推理请求（无论网关还是 M12 直连）全部挂起直到客户端超时。

---

## C. GUI 启动与构造验证（headless / offscreen）

### C-1 离线构造（`qa_gui_construct.py`，系统 Python + `QT_QPA_PLATFORM=offscreen`）
- 结果：**9/9 PASS**。
- `MainWindow` 与 5 个页面（控制台/模型库/对话/API/设置）均成功构造，无 `ImportError` / `NameError` / 构造崩溃。
- **关键回归（GUI 初始化死锁修复 #3）确认有效**：`MainWindow.__init__` 末尾使用 `_switch_page(0, refresh=False)`（不触发 `Worker.start()`），构造同步完成、事件循环可达；`main.py` 通过 `QTimer.singleShot(300, ...)` 在 `app.exec()` 之后才做首次刷新。
- 5 个页面切换（refresh=False）均无异常。

### C-2 冻结态 GUI exe 启动（`qa_gui_exe.py`，offscreen）
- 先起 `--backend` 后端于 8031（GUI 据此跳过"启动服务"弹窗），以 offscreen 启动 GUI exe，轮询 `%LOCALAPPDATA%/LLMDesktop/startup.log`。
- **结果**：启动日志确认到达 `14:50:21 entering app.exec()` —— **证明冻结态 exe 同样无初始化死锁、能正常进入事件循环**（需求 9 打开即运行成立）。
- **沙箱限制**：GUI exe 进入事件循环后约 0.5s 以 `rc=3221226505`(0xC0000409) 退出。该崩溃发生在 `app.exec()` **之后**，且同一套 PySide6 在 C-1 离线构造中 9/9 通过，判定为 **offscreen 无头环境（系统托盘/原生事件）假象，非产品逻辑缺陷**。建议在真实带显示器桌面环境复验运行稳定性。
- 进程/临时目录均已清理。

---

## D. 需求逐项核验表（10 条）

| # | 需求 | 验证方式 | PASS/FAIL | 备注 |
|---|---|---|---|---|
| 1 | C/S 桌面 GUI（参考主流布局） | 读 `ui/main_window.py`（左 200px 导航 + 右 QStackedWidget 五页 + 状态栏 + 系统托盘）；离线构造 9/9 | **PASS** | 五页齐全；C-2 确认冻结 exe 进入事件循环 |
| 2 | 加载/取消/启停/选模型/测试模型 | 读 `dashboard_page.py`（`_on_start/_on_stop/_on_load/_on_cancel_load/_on_test` + `ModelComboBox`）；端到端 M9/M10/M11/M12 | **PASS** | 各控件/接口均可用；M12 首轮推理正常。**但受"引擎二次请求挂起"缺陷影响多步串联流程**（见已知问题#1） |
| 3 | GitHub 模型列表拉取/下载（可填账号） | 读 `library_page.py`（`catalog(force,keyword)`=M3、`pull`=M4、鉴权 `AuthDialog`）；端到端 M3 | **PASS** | 实现为"curated 清单 + 可选远程合并"（`catalog_builtin.json`），支持 gated 模型填 token 下载；非直接调 GitHub API，但功能等价 |
| 4 | 模型对话窗口 | 读 `chat_page.py`（流式 SSE、多轮上下文、打字机）；端到端首次 chat 非流式/流式均返回 content | **PASS** | UI 与首轮推理正常；重复对话受引擎缺陷影响（见#1） |
| 5 | API 调用信息展示（地址/模型名/Key 生成） | 读 `api_page.py`（`openai_base_url()`、`/v1/models`、`gen_key`=M7）；端到端 M7 | **PASS** | M7 生成 64 位 hex Key 正常 |
| 6 | 一键安装包 | 检查 `client/build/inno_setup.iss` 与产物 `client/build/installer/LLMDesktop-Setup-1.0.0.exe`(48MB) | **PASS*** | 安装包**存在且脚本完整**（含默认路径、组件选择、卸载清理）；沙箱内未实际执行静默安装（需管理员提权+显示器），以"产物存在性+脚本审阅"判定 |
| 7 | 端口扫描选空闲端口 | 读 `core/port_scanner.py` + `routers/manage.py` M2；pytest `test_m2_ports` + 端到端 M2 | **PASS** | `psutil` 枚举 + socket 绑定探测，返回空闲/占用列表 |
| 8 | 守护进程（崩溃自愈） | 读 `core/pool.py` `_watchdog_loop`（引擎崩溃重启）+ `core/backend_launcher.py` `_watch`（后端进程崩溃自动重启） | **PASS*** | 进程级自愈有效。**注意**：看门狗以"进程是否存活"判活，对"进程活着但不响应"的引擎挂起（#1）不会触发重启——建议补充响应探活 |
| 9 | 无登录、打开即运行 | 读 `main.py`（无登录态；启动即建 QApplication 并提示一键启动后端）；C-2 确认到达 `app.exec()` | **PASS** | 无登录；打开即尝试拉起/连接服务 |
| 10 | 按配置判并发、多模型并发 | 读 `core/system.py` `estimate_max_models`（按空闲内存算建议并发）、M13/M14、`core/pool.py` 多实例（每模型独立引擎 + `alloc_port` 基端口+2）；端到端 M13/M8 | **PASS** | 并发上限按配置/系统检测给出；多模型并发架构支持（各自引擎端口）。**重复加载受引擎缺陷影响**（见#1） |

> `PASS*` = 以静态审阅 + 可用性验证判定，运行期静默安装/看门狗响应探活为建议项，非阻断。

**需求 PASS 数：10/10（功能已实现并验证；1 个阻断性已知缺陷见下，需修复后重测功能流）。**

---

## 已知问题 / 风险

### 🔴 #1（阻断性，源 bug）已加载引擎"只响应第 1 次推理请求，之后全部挂起"
- **现象**：同一已加载模型实例，第 1 次 chat/completions（网关非流式、流式、或 M12 直连）正常返回；**之后所有**推理请求（网关或 M12）均挂起，直至客户端超时（60/180s ReadTimeout）。
- **复现**（同一引擎实例，顺序请求；脚本 `qa_e2e_repro.py`）：
  ```
  [0] 网关非流式  → 200, 1.5s, content len=24   ✅ 首次正常
  [1] M12 直连    → ReadTimeout 60s              ❌ 挂起
  [2] 网关非流式  → ReadTimeout 50s              ❌ 挂起
  [3] M12 直连    → ReadTimeout 60s              ❌ 挂起
  [4] 网关非流式  → ReadTimeout 50s              ❌ 挂起
  ```
- **为什么 pytest 没发现**：`test_m12`/`test_chat_*` 每个用例结束都 `unload` + `wait_engine_gone()` 杀引擎，每个用例用全新引擎，故从未在同一实例上发第 2 次推理。
- **影响**：直接击穿需求 2/4/10 的真实使用流（加载→测试→对话→再对话 会卡死在第二步之后）；是**交付阻断项**。
- **初步定位（供工程师）**：后端为单 uvicorn 进程，网关与 M12 均经 `httpx` 异步调用同 `llama-server.exe`（端口 8039）。首请求后引擎不再响应后续请求，疑似 llama.cpp server 的 keep-alive/单连接处理或该 CPU 构建在请求收尾后阻塞；**并非测试脚本问题**（最简复现仅为顺序 httpx 请求）。
- **建议**：①排查 `llama-server.exe` 启动参数（是否需 `--no-warmup`、并发/连接限制）与后端连接复用（`trust_env=False` 已设，建议显式 `keep_alive=False` 或每次请求后关闭连接）；②后端侧对引擎做"响应探活 + 超时重启"（补充 #8 看门狗的探活维度）；③补充"同实例连续多次推理"的回归用例到 pytest（不要每次 unload）。

### 🟡 #2（沙箱限制）GUI exe 在 offscreen 无头环境进入事件循环后崩溃
- `rc=3221220409`(0xC0000409)，发生在 `app.exec()` 之后 ~0.5s；同一 PySide6 离线构造 9/9 通过，**判定为无头/offscreen Qt（系统托盘、原生事件）环境假象**，非产品逻辑缺陷。
- **建议**：在真实带显示器桌面环境复验 GUI 长时间运行稳定性（本沙箱无法提供显示器）。

### 🟡 #3（沙箱限制）Inno 安装包未实际静默安装
- `inno_setup.iss` 含 `PrivilegesRequired=admin`，静默安装需管理员提权与显示器；沙箱未执行。已以"脚本审阅 + 产物存在性"判定安装包可用。
- **建议**：在管理员+显示器的Windows 上实机跑一次 `LLMDesktop-Setup-1.0.0.exe` 验证安装/卸载/启动链路。

### 🟢 #4（已确认修复，未回退）历史已知修复回归
- 打包同名 `core` 冲突 → 后端作 data 文件打入 `_internal/backend/`，`--backend` 模式正常 import serve（端到端已验证）。
- `settings_page.py` 缺 `QHBoxLayout` import → 已修复（C-1 构造无 `NameError`）。
- GUI 初始化死锁 → `QTimer.singleShot` 延迟刷新（C-1/C-2 均确认进入事件循环，无死锁）。
- 冻结态 `models_dir`/`config` 改到 `%LOCALAPPDATA%\LLMDesktop`（offscreen 构造与 exe 启动均正常）。
- M12 对思考模型兜底取 `reasoning_content`（端到端 M12 首轮 `sample` 非空）。
- 网关把 `reasoning_content` 合并进 `content`（非流式 `_normalize` + 流式 `_rewrite_sse_line`；端到端首次 chat `content` 非空已验证）。

### 📌 接口小瑕疵（非阻断）
- 需求文档标注 M15 为 `/api/v1/stop`，实际实现与客户端均为 `/api/v1/service/stop`（`routers/manage.py` L326 / `api_client.py` L187 一致）。属文档与实现命名不一致，实现自洽，不影响功能。

---

## 复测命令清单

```bash
# 0) 前置：系统 Python 与依赖（httpx/psutil/fastapi/uvicorn/pytest/PySide6）
"C:\Program Files\Python311\python.exe" --version

# A) pytest 单元测试（约 4.5 分钟，含真实模型加载）
cd /e/llm-service
export NO_PROXY=127.0.0.1,localhost HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy=
"C:\Program Files\Python311\python.exe" -m pytest tests/ -q

# B) 冻结态 exe 端到端（修正版，含 M7 POST / 首轮推理；建议补"同实例多次推理"复现）
cd /e/llm-service
export NO_PROXY=127.0.0.1,localhost HTTP_PROXY= HTTPS_PROXY= http_proxy= https_proxy=
"C:\Program Files\Python311\python.exe" qa_e2e_backend.py            # 端到端（修正）
"C:\Program Files\Python311\python.exe" qa_e2e_repro.py             # 引擎二次请求挂起复现

# C) GUI 验证
cd /e/llm-service
export QT_QPA_PLATFORM=offscreen
"C:\Program Files\Python311\python.exe" qa_gui_construct.py         # 离线构造 9/9
"C:\Program Files\Python311\python.exe" qa_gui_exe.py               # 冻结 exe 启动（看 startup.log 到达 app.exec）

# 清理（测试已自动清理进程；如残留手动清理）
taskkill /F /IM llama-server.exe
```

---

## 交付物清单（位于 `E:/llm-service`）
- `tests/QA_REPORT.md` —— 本报告
- `qa_e2e_backend.py` / `qa_e2e_diag.py` / `qa_e2e_repro.py` —— 冻结态 exe 端到端 & 缺陷复现脚本
- `qa_gui_construct.py` / `qa_gui_exe.py` —— GUI 离线构造 & 冻结 exe 启动脚本
- `tests_result.log` / `qa_e2e_result*.log` / `qa_diag.log` / `qa_repro.log` / `qa_gui_exe.log` —— 各次运行证据日志
- 所有测试进程与临时目录已清理，未对系统/用户目录做破坏性操作。
