# -*- coding: utf-8 -*-
"""
PyInstaller 打包规格（client/build/pyinstaller.spec）。

产物：onedir 模式 dist/llmBuddy/llmBuddy.exe（窗口程序 console=False，无控制台窗口）。
选择 onedir 而非 onefile 的理由：C/S 场景下后端进程（exe --backend）会被反复重启，
onefile 每次都要解压自身（数十 MB），启动慢且临时目录易被清理；onedir 直接跑安装目录。

内嵌内容：
    - PySide6 桌面 GUI（client/ 全部源码，入口 main.py）
    - 完整后端：serve.py + core/ + routers/ + fastapi/uvicorn/httpx/psutil
      （运行时 GUI 以子进程方式拉起「llmBuddy.exe --backend」运行内嵌服务）
    - 资源：resources/styles.qss、core/catalog_builtin.json
不含：
    - 引擎 llama-server.exe 及 DLL（由 Inno Setup 从 engine-runtime/ 装入安装目录 engine/，
      后端经 LLM_ENGINE 环境变量定位）；模型文件由用户在客户端模型库页下载
"""
import os

# PyInstaller 6.x 在 spec 命名空间提供 SPECPATH（spec 文件所在目录）；旧版本用 __file__
try:
    _SPEC_DIR = SPECPATH  # noqa: F821
except NameError:
    _SPEC_DIR = os.path.dirname(os.path.abspath(__file__))

SPEC = os.path.join(_SPEC_DIR, "pyinstaller.spec")
CLIENT_DIR = os.path.dirname(_SPEC_DIR)                # E:/llm-service/client
REPO_DIR = os.path.dirname(CLIENT_DIR)                 # E:/llm-service

a = Analysis(
    [os.path.join(CLIENT_DIR, "main.py")],
    pathex=[CLIENT_DIR, REPO_DIR],       # REPO_DIR 使 serve.py 可被 import（内嵌后端）
    binaries=[],
    datas=[
        # GUI 主题
        (os.path.join(CLIENT_DIR, "resources", "styles.qss"), "resources"),
        # 应用图标（窗口/托盘图标源图）
        (os.path.join(CLIENT_DIR, "resources", "llmbuddy.png"), "resources"),
        # 内嵌后端：serve.py + 后端 core/ + routers/ 作为「数据文件」整体打入
        # _internal/backend/，与客户端 client/core 彻底隔离（避免同名 core 包冲突）。
        # 运行时由 main.py 的 --backend 分支将该目录插到 sys.path 最前再 import serve。
        (os.path.join(REPO_DIR, "serve.py"), "backend"),
        (os.path.join(REPO_DIR, "core"), "backend/core"),
        (os.path.join(REPO_DIR, "routers"), "backend/routers"),
    ],
    hiddenimports=[
        # ---- PySide6 ----
        "PySide6",
        "PySide6.QtCore",
        "PySide6.QtWidgets",
        "PySide6.QtGui",
        "PySide6.QtNetwork",
        "PySide6.QtSvg",
        # ---- 后端第三方依赖（serve.py 经 --backend 模式在运行时 import）----
        # 注意：serve/core/routers 本身作为 data 文件分发（见上方 datas），此处只列依赖。
        "fastapi",
        "fastapi.middleware.cors",
        "starlette",
        "starlette.routing",
        "starlette.responses",
        "uvicorn",
        "uvicorn.logging",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan.on",
        "click",
        "pydantic",
        "pydantic_core",
        "anyio",
        "anyio.to_thread",
        "anyio.lowlevel",
        "httpx",
        "httpcore",
        "h11",
        "certifi",
        "idna",
        "sniffio",
        "psutil",
        # ---- 客户端业务包（client/core 与 backend/core 同名，仅收集客户端侧）----
        "app_config",
        "core.api_client",
        "core.backend_launcher",
        "core.port_scanner",
        "core.task_runner",
        "core.signals",
        "core.theme",
        "ui",
        "ui.main_window",
        "ui.pages",
        "ui.pages.api_page",
        "ui.pages.assistant_page",
        "ui.pages.chat_page",
        "ui.pages.dashboard_page",
        "ui.pages.library_page",
        "ui.pages.settings_page",
        "ui.widgets",
        "ui.widgets.api_key_dialog",
        "ui.widgets.assistant_cards",
        "ui.widgets.auth_dialog",
        "ui.widgets.auto_scroll_area",
        "ui.widgets.log_view",
        "ui.widgets.model_combo",
        "ui.widgets.progress_dialog",
        "ui.widgets.status_bar",
        "core.assistant_client",
        # ---- 智能助理后端可选依赖（1.0.8）----
        # 后端代码以 data 文件分发，运行期才 import，必须显式收集：
        #   pyyaml    技能 SKILL.md 的 frontmatter 解析
        #   langgraph LangGraph 编排引擎（缺失时 orchestrator 自动降级为顺序执行）
        "yaml",
        "langgraph",
        "langgraph.graph",
        "langgraph.graph.state",
        "langchain_core",
        "langchain_core.messages",
        "langchain_core.runnables",
        "langchain_core.tools",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 排除体积大且不需要的模块
        "tkinter",
        "matplotlib",
        "numpy",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtQml",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    [],
    name="llmBuddy",
    icon=os.path.join(CLIENT_DIR, "resources", "llmbuddy.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,               # 不启用 UPX：避免压缩 PySide6/uvicorn DLL 引发运行时问题
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # 窗口程序：不弹控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# COLLECT 生成 onedir 布局：dist/llmBuddy/llmBuddy.exe + dist/llmBuddy/_internal/
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="llmBuddy",
)
