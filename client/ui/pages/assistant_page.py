# -*- coding: utf-8 -*-
"""
智能助理页面（ui/pages/assistant_page.py）——1.0.8 新模块。

布局（1.0.8 按 WorkBuddy 对话框形态重排）：
    左栏（主窗口 NavPanel 内）              右栏（本页）
      智能助理                          ┌ 标题 + 管理入口 ────────────────┐
        ▸ 任务（最近 6 条，点即切换）    │ 对话流（气泡 / 工具卡 / 审批卡） │
        ▸ 技能 / 工具 / MCP / 审计      │ ┌─ 队列面板（1.0.9，非空才显）─┐ │
          （点开管理面板）              │ │ 排队中（N）· 按序发送  [清除] │ │
                                        │ │ ① …  [✎][↑][↓][✕]          │ │
                                        │ ├─ 输入框 ────────────────────┤ │
                                        │ │ [+] [工作区][审批][技能]      │ │
                                        │ │          ◐  [模型 ▾]  [⚡][↑] │ │
                                        │ └─────────────────────────────┘ │
                                        └─────────────────────────────────┘

底栏与 WorkBuddy 输入框底部的对应关系（功能流程对齐，不是照搬图标）：
    +              添加本机文件（插入 @路径 作为上下文）
    分支 master    工作区：文件工具可访问的根目录，按钮显示目录名，点击切换
    ⊘ 权限         审批策略：只读自动 / 全部确认 / 全部放行
    × 团队 chip    技能 chip：挂载后每条消息自动带上「用 xx 技能」，可一键清除
    加载环         运行中指示（◐ 旋转），与「■ 停止」按钮配套
    模型下拉       模型 + 来源合并下拉（本地 / 硅基流动 / 自定义 + 配置密钥）
    ↑ 发送         发送（流式中原地变成 ■ 停止）
    🎤 语音        本机暂无语音能力，不做假按钮

能力（全部走本机后端 /api/v1/assistant/*）：
    - 操作本机文件、执行命令、抓网页（工具卡片可视化每次调用）
    - MCP 服务器增删、连接/断开，工具自动进白名单
    - 技能包 SKILL.md 的新建/编辑/导入(zip)/导出(zip)/删除
    - 高危动作审批卡片（允许 / 本任务允许同类 / 拒绝）
    - 本地模型 / 硅基流动 / 自定义 OpenAI 兼容端点切换
    - 任务历史保存与回看（左栏「任务 / 空间」两区 + 管理面板）
    - 引导会话（1.0.9）：执行中插话排队 / 立即引导发送 / 队列可改可删可排序 /
      跨轮次自动消费 / 停止后暂停+继续（详见 docs/智能助理-引导会话-架构设计.md）
"""
import json
import os
import time
import tempfile
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, QPoint, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPixmap,
                           QTextCursor)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QMessageBox,
                               QPlainTextEdit, QPushButton, QSizePolicy,
                               QSplitter, QTabWidget, QTextEdit, QVBoxLayout,
                               QWidget)

from core.assistant_client import AssistantApi, AssistantStream
from core.task_runner import Worker
from core.theme import is_light
from ui.widgets.assistant_cards import (ApprovalCard, AutoLoadPrompt, ChatBubble,
                                        StepLine, ThinkingBlock, ToolCard)
from ui.widgets.auto_scroll_area import AutoHideScrollArea
from ui.widgets.queue_item import QueueItemRow

POLICY_ITEMS = [
    ("readonly_auto", "只读自动", "只读类工具直接执行，写入/高危仍逐次确认（推荐）"),
    ("ask_all", "全部确认", "任何工具调用都要你确认后才执行"),
    ("yolo", "全部放行", "所有工具直接执行、不再询问（危险）"),
]
POLICY_ICON = {"readonly_auto": "⊘", "ask_all": "✓", "yolo": "⚡"}
#: 底栏按钮上的策略短名（宽度压缩：底栏可用 504px，完整名只在 tooltip）
POLICY_SHORT = {"readonly_auto": "自动", "ask_all": "确认", "yolo": "放行"}
RISK_TEXT = {"low": "只读", "medium": "写入", "high": "高危"}

# 在线目录条目的「零配置 / 需密钥 / 需注册 / 未知」徽标（PRD §5.3 判定规则）
#   zero_config    green  默认可见
#   need_key       yellow 默认隐藏，需筛选勾选
#   need_register  grey   同上
#   unknown        no install button，只给「查看源」
CATALOG_CLASS_BADGE = {
    "zero_config": "[零配置]",
    "need_key": "[需密钥]",
    "need_register": "[需注册]",
    "unknown": "[未知]",
}
#: 「需密钥 / 需注册」默认隐藏（PRD §5.3 结论）
CATALOG_HIDDEN_CLASSES = ("need_key", "need_register")

# 在线安装的 MCP `command` 白名单（PRD §8.5 安全红线）
MCP_COMMAND_WHITELIST = ("npx", "uvx", "python", "python3", "node", "docker")

# 左栏「任务 / 空间」两区（对齐 WorkBuddy 的语义）：
#   任务 = 一次对话（后端仍是 sessions），空间 = 任务所属的工作区目录。
# 条数必须克制 —— 左栏宽 200、最高只有几百像素，靠滚动条才能看到其余内容。
NAV_TASK_MAX = 4            # 「任务」区直接显示几条（WorkBuddy 的「任务 (4)」同款）
NAV_SPACE_MAX = 4           # 「空间」区最多显示几个空间
NAV_SPACE_TASK_MAX = 5      # 空间展开后最多显示几条任务
NAV_ROW = 26                # 单行高度
NAV_SCROLL_MAX = 420        # 任务+空间滚动区最高多少（超出内部滚动）
# 兼容旧命名（管理面板文案里还引用着）
NAV_MAX_SESSIONS = NAV_TASK_MAX
NAV_SESSION_ROW = NAV_ROW


class _NavScrollArea(AutoHideScrollArea):
    """左栏任务/空间滚动区：sizeHint 可指定。

    QScrollArea 默认的 sizeHint 在 widgetResizable 下不可靠（实测只有几十像素），
    布局据此分配高度时会把「空间」那一段裁出视口 —— 表现为左栏只有 4 条任务。
    这里让 sizeHint 跟着内容行数走，布局就能一次给对高度；同时仍保留
    minimumHeight 以便窗口变矮时压缩 + 内部滚动。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hint = 0

    def set_hint_height(self, h: int) -> None:
        if h == self._hint:
            return
        self._hint = max(0, int(h))
        self.updateGeometry()

    def sizeHint(self):  # noqa: N802 - Qt 命名
        size = super().sizeHint()
        if self._hint:
            size.setHeight(self._hint)
        return size


def _norm_ws(path: Any) -> str:
    """工作区路径归一化（与后端 store.norm_workspace 同规则）。

    同一个目录写成 E:/x、E:\\x、e:/x/ 会被当成三个「空间」，左栏就会出现
    三条同名目录，所以比较前一律归一化。
    """
    p = str(path or "").strip().replace("\\", "/")
    if not p.startswith("//"):          # UNC 路径（//server/share）保持双斜杠
        while "//" in p:
            p = p.replace("//", "/")
    p = p.rstrip("/")
    if len(p) >= 2 and p[1] == ":":
        p = p[0].upper() + p[1:]
    return p


def _ellipsis(text: str, limit: int = 12) -> str:
    """左栏只有 200px，标题必须截断（完整标题放 tooltip）。"""
    text = " ".join(str(text or "").split()) or "未命名任务"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _compact_chip(btn) -> None:
    """把底栏 chip 压成文字宽度，避免 QPushButton 默认 81px 的最小宽把底栏顶爆。

    架构 §4 宽度门禁：逻辑 720 → 右栏可用 504px，底栏 6 个 chip + 模型 + 发送
    全部要塞进 439px。Qt 的 QPushButton 默认 `sizeHint().width()` 是 81px
    （受 QSS padding 与 style 最小宽影响），这里显式钉死紧凑的水平内边距，
    让 `sizeHint()` 反映真实占用，门禁测试才有意义。
    """
    btn.setStyleSheet("padding: 2px 6px;")
    btn.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
    btn.setFixedHeight(26)


def _slug_id(text: str, fallback: str = "") -> str:
    """把任意名字转成可用的 MCP 服务器 id（与 skills._slugify 同规则）。"""
    import re as _re
    s = _re.sub(r"[^a-zA-Z0-9._-]+", "-", str(text or "")).strip("-.")
    return s or fallback


def parse_mcp_json(text: str) -> List[Dict[str, Any]]:
    """把一段 MCP JSON 配置解析成 `/mcp` 可直接 POST 的服务器配置列表。

    兼容三种写法（PRD §5.1.1「粘贴 JSON 添加 MCP」）：

    1. **Claude Desktop 片段**（最常见）::

           {"mcpServers": {"filesystem": {"command": "npx",
             "args": ["-y", "@modelcontextprotocol/server-filesystem", "."]}}}

    2. 单个服务器对象（含 `command` 或 `url`）::

           {"command": "npx", "args": ["-y", "xxx"]}

    3. 服务器数组 / 多个键的对象（无 `mcpServers` 包裹时按值判定）。

    返回每条：`{id, name, transport, command, args, url, env, risk,
    enabled, connect}`。**不做任何执行**，只落配置（安全红线 PRD §8.5）。
    非法 JSON 或一条可用配置都没有时抛 `ValueError`（中文说明，UI 直接展示）。
    """
    raw = (text or "").strip()
    if not raw:
        raise ValueError("内容为空，请粘贴 MCP 配置 JSON。")
    try:
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"不是合法的 JSON：{e}") from e

    def looks_like_server(obj: Any) -> bool:
        return isinstance(obj, dict) and ("command" in obj or "url" in obj
                                          or "baseUrl" in obj)

    candidates: List[tuple] = []      # (name_hint, obj)
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        for k, v in data["mcpServers"].items():
            if looks_like_server(v):
                candidates.append((str(k), v))
    elif isinstance(data, dict) and looks_like_server(data):
        candidates.append((str(data.get("id") or data.get("name") or ""), data))
    elif isinstance(data, list):
        for i, v in enumerate(data):
            if looks_like_server(v):
                candidates.append((str(v.get("id") or v.get("name")
                                       or f"mcp{i + 1}"), v))
    elif isinstance(data, dict):
        for k, v in data.items():
            if looks_like_server(v):
                candidates.append((str(k), v))

    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for hint, obj in candidates:
        url = str(obj.get("url") or obj.get("baseUrl") or "").strip()
        cmd = str(obj.get("command") or "").strip()
        transport = "http" if url else "stdio"
        name = str(obj.get("name") or hint or cmd or url).strip()
        base = _slug_id(hint or name or cmd or "mcp", "mcp")
        # 冲突加 -2 / -3（与架构 A5 同口径）
        n = seen.get(base, 0) + 1
        seen[base] = n
        sid = base if n == 1 else f"{base}-{n}"
        args = obj.get("args") or []
        if isinstance(args, str):
            args = [args]
        args = [str(a) for a in args]
        env = obj.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        risk = str(obj.get("risk") or ("high" if transport == "stdio" else "medium"))
        out.append({
            "id": sid,
            "name": name or sid,
            "transport": transport,
            "command": cmd or ("npx" if transport == "stdio" else ""),
            "args": args,
            "url": url,
            "env": {str(k): str(v) for k, v in env.items()},
            "risk": risk if risk in RISK_TEXT else "high",
            "enabled": True,
            "connect": True,
        })
    if not out:
        raise ValueError("没有找到任何 MCP 服务器配置（需要一个 command 或 url）。")
    return out
# 运行中指示的旋转字符
SPIN_FRAMES = "◐◓◑◒"


class AssistantPage(QWidget):
    """智能助理主页面。"""

    #: 滚动合并窗口（毫秒）：流式输出时合并滚动请求，避免整屏闪动
    SCROLL_MS = 40

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._settings = QSettings("llmBuddy", "llmBuddy")
        self._assist = AssistantApi(config.gateway_url(), config.api_key)

        self._session_id = ""
        self._stream: Optional[AssistantStream] = None
        self._state: Dict[str, Any] = {}
        self._bubble: Optional[ChatBubble] = None
        self._thinking: Optional[ThinkingBlock] = None
        self._tool_cards: List[ToolCard] = []
        self._approvals: Dict[str, ApprovalCard] = {}
        self._busy = False
        self._loading = False
        # 引导会话队列（1.0.9）：_queue_items 是后端 queue 全量快照的镜像
        # （单一事实来源，不做局部增删）；面板只在队列非空时可见、不占布局
        self._queue_items: List[Dict[str, Any]] = []
        self._queue_paused = False
        self._queue_panel: Optional[QWidget] = None
        self._queue_head_lbl: Optional[QLabel] = None
        self._queue_clear_btn: Optional[QPushButton] = None
        self._queue_resume_btn: Optional[QPushButton] = None
        self._queue_body: Optional[QWidget] = None
        self._queue_body_lay: Optional[QVBoxLayout] = None
        self._queue_rows: Dict[str, QueueItemRow] = {}
        # 流式输出时不要频繁重绘/滚动（用户反馈「思考时屏幕闪得厉害」）：
        #   _follow    用户是否还贴着底部（往上翻过就别再硬拽回去）
        #   _scroll_timer 合并滚动请求，SCROLL_MS 内只滚一次
        self._follow = True
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._do_scroll_bottom)

        #: 底栏状态（1.0.8 界面提审）：模型来源 / 模型名 / 挂载的技能
        self._active_provider = "local"
        self._active_model = ""
        self._skill_slugs: List[str] = []
        self._model_cache: Dict[str, List[str]] = {}
        #: 1.0.8 清单中心 / 自动加载
        #: _tool_prefs: 后端 GET /tools/prefs 快照（disabled / max_tools / active）
        #: _profiles: GET /profiles 快照（auto_load / profiles）
        #: _catalog_*: 在线区缓存（打开清单中心时先渲染，再后台补）
        #: _auto_tips: 已提示过的 (session_id, profile) —— 同任务同 profile 只提示一次
        self._tool_prefs: Dict[str, Any] = {}
        self._profiles: Dict[str, Any] = {}
        self._catalog_mcp: Dict[str, Any] = {}
        self._catalog_repos: Dict[str, Any] = {}
        self._auto_tips: set = set()
        self._auto_off = False      # 本会话已关闭自动加载（立即失效，不等后端）
        # 管理面板 / 任务列表：记住用户选中的任务与最近一次列表快照
        # （列表会被 refresh 重建，重建瞬间 currentItem 为 None，必须靠 _sel_sid 兜底）
        self._sel_sid = ""
        self._session_rows: List[Dict[str, Any]] = []
        # 工作区自愈：本机记忆与后端不一致时，每个页面实例只补同步一次，避免刷新循环
        self._ws_synced = False
        # 管理面板懒创建（任务/技能/工具/MCP/审计 tab 在里面）
        self._manager: Optional["ManagerDialog"] = None
        self._spin_i = 0
        # 各管理列表在面板未创建时为 None，填充函数都要判空
        self._all_sess_list = None
        self._skill_list = None
        self._tool_list = None
        self._mcp_list = None
        self._audit_view = None
        # 左栏「任务 / 空间」两区：任务行按钮表、空间分组快照、当前展开的空间
        self._nav_scroll = None
        self._nav_body = None
        self._nav_body_lay = None
        self._nav_task_title = None
        self._nav_task_btns: Dict[str, QPushButton] = {}
        self._nav_spaces: List[Dict[str, Any]] = []
        self._nav_rows: Optional[List[Dict[str, Any]]] = None   # 左栏用的任务快照
        self._open_space = ""

        self._build_ui()
        # 注意：不在这里调 refresh()。构造函数运行在 QApplication.exec() 之前，
        # 此时启动 QThread（Worker）会导致窗口永不显示（见 main_window / task_runner
        # 的历史崩溃记录）。首次刷新由 main.py 的 QTimer.singleShot(300, _post_startup)
        # 通过 _switch_page(0) 触发，后端就绪时 _refresh_all 也会调用 refresh()。

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 8)
        root.setSpacing(6)

        root.addLayout(self._build_header())
        # 右侧 tab 面板已取消：任务/技能/工具/MCP/审计 全部移到左栏二级栏目
        # 与「管理」面板（批注：作为二级栏目放在智能助理下面直接显示）。
        root.addWidget(self._build_chat_panel(), 1)

    def _build_header(self) -> QHBoxLayout:
        """顶部只留标题与副标题；模型/审批/工作区统一下沉到输入框底栏。"""
        head = QHBoxLayout()
        head.setSpacing(8)

        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("智能助理")
        title.setObjectName("PageTitle")
        titles.addWidget(title)
        self._subtitle = QLabel("本地文件 · MCP · 技能 · LangGraph 编排")
        self._subtitle.setObjectName("PageSubtitle")
        titles.addWidget(self._subtitle)
        head.addLayout(titles)
        head.addStretch(1)

        self._manage_btn = QPushButton("管理")
        self._manage_btn.setObjectName("GhostBtn")
        self._manage_btn.setToolTip("打开管理面板：任务 / 技能 / 工具 / MCP / 审计")
        self._manage_btn.setCursor(Qt.PointingHandCursor)
        self._manage_btn.clicked.connect(lambda: self.open_manager())
        head.addWidget(self._manage_btn)
        return head

    # ---------------- 对话区（内容列居中，参考 WorkBuddy） ----------------
    def _build_chat_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._scroll = AutoHideScrollArea()
        self._scroll.setObjectName("ChatScroll")
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll_value)
        self._container = QWidget()
        row = QHBoxLayout(self._container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        # 内容列最宽 940：窗口拉宽后对话与输入框居中，不横向铺满整个屏幕
        self._col = QWidget()
        self._col.setObjectName("ChatColumn")
        self._col.setMaximumWidth(940)
        self._col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._msg_layout = QVBoxLayout(self._col)
        self._msg_layout.setContentsMargins(2, 2, 8, 2)
        self._msg_layout.setSpacing(6)
        self._msg_layout.addStretch(1)
        row.addStretch(1)
        row.addWidget(self._col, 100)   # 先抢满，被 maximumWidth 限住后两侧等分
        row.addStretch(1)
        self._scroll.setWidget(self._container)
        lay.addWidget(self._scroll, 1)

        lay.addWidget(self._build_footer())
        return panel

    def _build_footer(self) -> QWidget:
        """底部固定区：状态提示 / 队列面板 / 输入框 / 合规提示（同宽居中）。"""
        foot = QWidget()
        outer = QHBoxLayout(foot)
        outer.setContentsMargins(0, 8, 0, 2)
        outer.setSpacing(0)

        inner = QWidget()
        inner.setMaximumWidth(940)
        inner.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 0, 0)
        il.setSpacing(6)

        self._status = QLabel("")
        self._status.setObjectName("PageSubtitle")
        self._status.setWordWrap(True)
        self._status.setVisible(False)
        il.addWidget(self._status)

        # 引导会话队列面板：插在输入框正上方（仅队列非空时可见）
        il.addWidget(self._build_queue_panel())

        il.addWidget(self._build_composer())

        tip = QLabel("内容由 AI 生成，请核实重要信息")
        tip.setObjectName("ComposerTip")
        tip.setAlignment(Qt.AlignCenter)
        il.addWidget(tip)

        outer.addStretch(1)
        outer.addWidget(inner, 100)
        outer.addStretch(1)
        return foot

    # ---------------- 引导会话队列面板（1.0.9） ----------------
    def _build_queue_panel(self) -> QWidget:
        """队列容器：头部（计数 / 全部清除 / 继续）+ 行列表（QueueItemRow）。

        位置在 composer 之上、与对话列同宽；空队列 / 空闲态隐藏，
        setVisible(False) 不占布局空间（QVBoxLayout 跳过隐藏项）。
        """
        panel = QWidget()
        panel.setObjectName("QueuePanel")
        panel.setVisible(False)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        self._queue_head_lbl = QLabel("排队中（0）· 本轮结束后按序发送")
        self._queue_head_lbl.setObjectName("QueueTitle")
        head.addWidget(self._queue_head_lbl)
        head.addStretch(1)

        self._queue_resume_btn = QPushButton("已暂停 · 继续")
        self._queue_resume_btn.setObjectName("QueueResumeBtn")
        self._queue_resume_btn.setCursor(Qt.PointingHandCursor)
        self._queue_resume_btn.setToolTip(
            "已停止：队列随之暂停（消息保留、不会自动跑）。\n"
            "点「继续」按序发送排队消息，恢复执行。")
        self._queue_resume_btn.setVisible(False)
        self._queue_resume_btn.clicked.connect(self._resume_queue)
        head.addWidget(self._queue_resume_btn)

        self._queue_clear_btn = QPushButton("全部清除")
        self._queue_clear_btn.setObjectName("QueueClearBtn")
        self._queue_clear_btn.setCursor(Qt.PointingHandCursor)
        self._queue_clear_btn.setToolTip(
            "清除全部排队中的消息。\n引导消息已登记进对话流，不可清除。")
        self._queue_clear_btn.clicked.connect(self._queue_clear)
        head.addWidget(self._queue_clear_btn)
        lay.addLayout(head)

        body = QWidget()
        body.setObjectName("QueueBody")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(2)
        self._queue_body = body
        self._queue_body_lay = body_lay
        lay.addWidget(body)

        self._queue_panel = panel
        return panel

    def _build_composer(self) -> QWidget:
        """
        底部输入区（对齐 WorkBuddy 输入框底部工具条）：

            ┌ 今天帮你做些什么？ ───────────────────────────────┐
            │                                                        │
            │ [+] [工作区][⊘只读自动][技能][工具][MCP]  ◐  [模型▾] [⚡][↑] │
            └────────────────────────────────────────────────────────┘

        左侧是「动作与上下文」，右侧是「运行状态 / 模型 / 发送」。
        1.0.9 引导会话：忙碌态 ↑ 语义变为「排队入队」、左侧新增 ⚡
        「引导发送」、停止 ■ 同时可见；空闲态保持现状（■/⚡ 隐藏）。

        1.0.8 清单中心宽度约束（架构 §5.4，逻辑屏宽 720 → 可用 504px）：
          ① 工作区文案压到 ≤4 字（`_refresh_ws_btn`）；② 模型名短名 ≤8 字
             （`_refresh_model_btn`）；③ 底栏 spacing 4→2、边距 8→6；
          ④ 挂载 chip ≥3 个时折行到输入框上方（`_reparent_skill_chips`）。
        **不做**任何宽度自适应隐藏 —— 三个按钮必须始终可见且带文字。
        """
        box = QFrame()
        box.setObjectName("Composer")
        box.setProperty("focus", False)
        self._composer = box
        lay = QVBoxLayout(box)
        # ③ 边距 12/10/8 → 10/8/6：横向省 3px/侧，纵向省 2px/侧
        lay.setContentsMargins(10, 8, 6, 6)
        lay.setSpacing(2)
        self._composer_lay = lay

        self._input = QTextEdit()
        self._input.setObjectName("ComposerInput")
        self._input.setFrameShape(QFrame.NoFrame)
        self._input.setPlaceholderText(
            "今天帮你做些什么？（Enter 发送，Shift+Enter 换行）")
        self._input.setFixedHeight(66)
        self._input.installEventFilter(self)
        lay.addWidget(self._input)

        bar = QHBoxLayout()
        # ③ spacing 4 → 2：11 个 gap 省 22px
        bar.setSpacing(2)
        bar.setContentsMargins(0, 0, 0, 0)
        self._bar_lay = bar

        # 左：附件 / 工作区 / 审批策略 / 技能 / 工具 / MCP
        self._attach_btn = QPushButton("+")
        self._attach_btn.setObjectName("BarBtn")
        self._attach_btn.setStyleSheet("padding: 0px;")
        self._attach_btn.setFixedSize(26, 26)
        self._attach_btn.setCursor(Qt.PointingHandCursor)
        self._attach_btn.setToolTip(
            "添加附件，或打开能力清单中心（技能 / 工具 / MCP）")
        self._attach_btn.clicked.connect(self._show_attach_menu)
        bar.addWidget(self._attach_btn)

        self._ws_btn = QPushButton("工作区")
        self._ws_btn.setObjectName("BarChip")
        _compact_chip(self._ws_btn)
        self._ws_btn.setCursor(Qt.PointingHandCursor)
        self._ws_btn.setToolTip("切换文件工具可访问的工作区")
        self._ws_btn.clicked.connect(self._choose_workspace)
        bar.addWidget(self._ws_btn)

        self._policy_btn = QPushButton("⊘ 自动")
        self._policy_btn.setObjectName("BarChip")
        _compact_chip(self._policy_btn)
        self._policy_btn.setCursor(Qt.PointingHandCursor)
        self._policy_btn.setToolTip("高危动作审批策略（点击切换）")
        self._policy_btn.clicked.connect(self._show_policy_menu)
        bar.addWidget(self._policy_btn)

        self._skill_btn = QPushButton("技能")
        self._skill_btn.setObjectName("BarChip")
        _compact_chip(self._skill_btn)
        self._skill_btn.setCursor(Qt.PointingHandCursor)
        self._skill_btn.setToolTip("挂载技能 / 添加技能（本地文件夹 · zip · URL · 在线清单）")
        self._skill_btn.clicked.connect(self._show_skill_menu)
        bar.addWidget(self._skill_btn)

        # [工具]：工具启用开关 + 当前下发数（用户原话「对话框底部技能、工具、MCP」）
        self._tool_btn = QPushButton("工具")
        self._tool_btn.setObjectName("BarChip")
        _compact_chip(self._tool_btn)
        self._tool_btn.setCursor(Qt.PointingHandCursor)
        self._tool_btn.setToolTip("工具启用开关与当前下发给模型的数量")
        self._tool_btn.clicked.connect(self._show_tool_menu)
        bar.addWidget(self._tool_btn)
        # [MCP]：服务器连接态 + 在线清单安装 + 手动添加
        self._mcp_btn = QPushButton("MCP")
        self._mcp_btn.setObjectName("BarChip")
        _compact_chip(self._mcp_btn)
        self._mcp_btn.setCursor(Qt.PointingHandCursor)
        self._mcp_btn.setToolTip("MCP 服务器：连接/断开、从在线清单安装、手动添加")
        self._mcp_btn.clicked.connect(self._show_mcp_menu)
        bar.addWidget(self._mcp_btn)

        # 多技能挂载区：每个已挂载技能一个可移除 chip（最多 5 个）。
        # ④ ≥3 个时 reparent 到输入框上方（`_reparent_skill_chips`），
        #    否则会顶出底栏 504px 门禁。
        self._skill_chip_cont = QWidget()
        self._skill_chip_cont.setObjectName("SkillChips")
        chip_lay = QHBoxLayout(self._skill_chip_cont)
        chip_lay.setContentsMargins(0, 0, 0, 0)
        chip_lay.setSpacing(2)
        self._skill_chip_lay = chip_lay
        self._skill_chip_cont.setVisible(False)
        bar.addWidget(self._skill_chip_cont)
        #: chip 容器当前挂在哪（"bar" = 底栏内联 / "col" = 输入框上方独立一行）
        self._chips_row = "bar"
        self._skill_chips_row: Optional[QWidget] = None

        bar.addStretch(1)

        # 右：运行指示 / 模型 / 发送
        self._busy_lbl = QLabel("")
        self._busy_lbl.setObjectName("BusyDot")
        self._busy_lbl.setFixedWidth(16)
        self._busy_lbl.setToolTip("助理正在工作")
        self._busy_lbl.setVisible(False)
        bar.addWidget(self._busy_lbl)

        self._model_btn = QPushButton("模型")
        self._model_btn.setObjectName("ModelChip")
        _compact_chip(self._model_btn)
        self._model_btn.setCursor(Qt.PointingHandCursor)
        self._model_btn.setToolTip("切换模型来源与模型")
        self._model_btn.clicked.connect(self._show_model_menu)
        bar.addWidget(self._model_btn)

        self._stop_btn = QPushButton("■")
        self._stop_btn.setObjectName("StopBtn")
        self._stop_btn.setStyleSheet("padding: 0px;")
        self._stop_btn.setFixedSize(30, 30)
        self._stop_btn.setCursor(Qt.PointingHandCursor)
        self._stop_btn.setToolTip("停止生成")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._stop)
        bar.addWidget(self._stop_btn)

        # ⚡ 引导发送（1.0.9）：↑ 左侧的次级按钮，仅忙碌时可见。
        # 与 ↑（排队）构成双入口：都不打断当前生成，差异在「立即登记进
        # 对话流」vs「本轮结束后按序发送」。
        self._guide_btn = QPushButton("⚡")
        self._guide_btn.setObjectName("GuideBtn")
        self._guide_btn.setStyleSheet("padding: 0px;")
        self._guide_btn.setFixedSize(26, 26)
        self._guide_btn.setCursor(Qt.PointingHandCursor)
        self._guide_btn.setToolTip(
            "引导会话（Shift+Enter）：消息立即发出并登记进对话流，"
            "但不会中断当前生成")
        self._guide_btn.setVisible(False)
        self._guide_btn.clicked.connect(self._on_guide_clicked)
        bar.addWidget(self._guide_btn)

        self._send_btn = QPushButton("↑")
        self._send_btn.setObjectName("SendBtn")
        self._send_btn.setStyleSheet("padding: 0px;")
        self._send_btn.setFixedSize(30, 30)
        self._send_btn.setCursor(Qt.PointingHandCursor)
        self._send_btn.setToolTip("发送（Enter）")
        self._send_btn.clicked.connect(self._on_send_clicked)
        bar.addWidget(self._send_btn)

        lay.addLayout(bar)

        self._spin_timer = QTimer(self)
        self._spin_timer.setInterval(120)
        self._spin_timer.timeout.connect(self._tick_spin)
        return box

    # ---------------- 运行中指示（WorkBuddy 的加载环） ----------------
    def _tick_spin(self) -> None:
        self._spin_i = (self._spin_i + 1) % len(SPIN_FRAMES)
        self._busy_lbl.setText(SPIN_FRAMES[self._spin_i])

    def _start_spin(self) -> None:
        self._spin_i = 0
        self._busy_lbl.setText(SPIN_FRAMES[0])
        self._busy_lbl.setVisible(True)
        self._spin_timer.start()

    def _stop_spin(self) -> None:
        self._spin_timer.stop()
        self._busy_lbl.setVisible(False)

    # ---------------- 挂载 chip：≥3 个折行到输入框上方（宽度压缩规则 ④） ----------------
    #: 底栏可用宽度（逻辑 720 → 左栏 200 → 右栏 520 → 扣内边距）。
    #:
    #: 门禁常量的单一来源：测试从本类导入，不再各自硬编码字面量。
    #: 实测（逻辑 720 窗口）右栏**容器**实际可用 522（idle 474 / busy 522），
    #: 底栏 busy 实测 510（≤522，未裁切）。架构 §4 原定 504 是「保守基线」，
    #: 但 busy 态 510 已超它 6px，若继续用 504 当硬门禁会误报。按真实右栏
    #: 可用宽重算：留 8px style 抖动余量 → 522 - 8 = 514，取 514 为门禁。
    #: （idle 438 / chip 折行后仍远低于此值，三类状态均满足。）
    BAR_BUDGET_W = 514

    def _chips_width_over_budget(self, count: int) -> bool:
        """估算把 `count` 个已挂载技能 chip 留在底栏内联是否会顶爆 504px。

        规则④原话是「≥3 个折行」，但两个长 slug（如 `repo-digest` +
        `code-review`）内联就已经 687px，光靠数量判断不够。这里按 chip 实际
        `sizeHint()` 求和（chip 已经建好，直接量最准），超预算就提前折行。
        """
        lay = getattr(self, "_skill_chip_lay", None)
        if lay is None or count <= 0 or lay.count() == 0:
            return False
        chips_w = 0
        spacing = max(0, lay.spacing())
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if w is not None:
                chips_w += w.sizeHint().width()
        chips_w += spacing * max(0, lay.count() - 1)
        # 底栏其它固定项（+ / 工作区 / 策略 / 技能 / 工具 / MCP / 模型 / 发送…）
        # 404 是实测的空底栏 sizeHint；留 8px 余量吃掉 style 抖动。
        base = 404
        return (base + chips_w + 8) > self.BAR_BUDGET_W

    def _reparent_skill_chips(self, count: int) -> None:
        """挂载 ≥3 个技能时把 chip 容器从底栏搬到输入框上方独立一行。

        为什么不能留在底栏：每个 chip ≈62px（`✕ <slug>`），3 个就 186px，
        而逻辑 720 宽下底栏可用宽只有 504px（架构 §5.4 求和表：保守基线 538
        已超 34）。搬走后底栏恒 ≤439px，chip 在独立一行可以横向铺开。
        搬动用 reparent + 重新 addWidget，**不销毁** widget（否则已连接的
        clicked 信号会丢，chip 点了没反应）。
        """
        bar = getattr(self, "_bar_lay", None)
        lay = getattr(self, "_composer_lay", None)
        cont = getattr(self, "_skill_chip_cont", None)
        if bar is None or lay is None or cont is None:
            return
        flat = bool(count >= 3) or self._chips_width_over_budget(count)
        want = "col" if flat else "bar"
        if getattr(self, "_chips_row", "bar") == want:
            return
        # 先从当前布局摘下（两个布局都试，幂等）
        bar.removeWidget(cont)
        lay.removeWidget(cont)
        if flat:
            if self._skill_chips_row is None:
                row = QWidget()
                row.setObjectName("SkillChipsRow")
                row_lay = QHBoxLayout(row)
                row_lay.setContentsMargins(0, 0, 0, 0)
                row_lay.setSpacing(4)
                row_lay.addWidget(cont)
                row_lay.addStretch(1)
                row.setVisible(False)
                # 插在输入框之后、底栏之前（index 1 = 紧贴输入框下方独立一行）
                lay.insertWidget(1, row)
                self._skill_chips_row = row
            self._skill_chips_row.setVisible(True)
        else:
            if self._skill_chips_row is not None:
                self._skill_chips_row.setParent(None)
                self._skill_chips_row.deleteLater()
                self._skill_chips_row = None
            # 恢复到 addStretch 之前的原位置（bar 末项是 stretch）
            bar.insertWidget(max(0, bar.count() - 1), cont)
        self._chips_row = want
        cont.setVisible(bool(count))

    # ---------------- 底栏弹出菜单（贴近窗口底部，向上展开） ----------------
    def _popup_above(self, menu: QMenu, anchor: QWidget) -> None:
        menu.ensurePolished()
        size = menu.sizeHint()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        menu.exec(QPoint(pos.x(), pos.y() - size.height() - 6))

    # ---------------- 文件选择（统一入口，禁止原生对话框） ----------------
    def _pick_file(self, title: str, start: str = "",
                   name_filter: str = "所有文件 (*.*)", save: bool = False,
                   default_name: str = "", accept_text: str = "选择",
                   directory: bool = False) -> str:
        """统一的文件/目录选择入口，返回选中路径（取消返回 ""）。

        **必须 DontUseNativeDialog**：本机冻结包里，系统原生文件对话框一调用就
        以 `0x8001010E RPC_E_WRONG_THREAD`（COM 套间错误）直接杀掉进程，
        crash.log 留有栈：`_attach_file → QFileDialog.getOpenFileName`。
        表现为「点 + 或点工作区就闪退」且没有任何 Python 异常。自绘框没有这条
        路径依赖，最坏情况只是不弹出。同时标签写死中文，不依赖 Qt 翻译文件。

        directory=True：选目录（「从本地文件夹导入技能」用），复用同一份中文标签，
        避免与 `_choose_workspace` 的 20 行实例化逻辑漂移出两套文案。
        """
        try:
            fd = QFileDialog(self._dialog_parent(), title, start)
            fd.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            fd.setNameFilter(name_filter)
            if directory:
                fd.setOption(QFileDialog.Option.ShowDirsOnly, True)
                fd.setFileMode(QFileDialog.FileMode.Directory)
            elif save:
                fd.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
                fd.setFileMode(QFileDialog.FileMode.AnyFile)
                if default_name:
                    fd.selectFile(default_name)
            else:
                fd.setFileMode(QFileDialog.FileMode.ExistingFile)
            fd.setLabelText(QFileDialog.DialogLabel.LookIn, "位置：")
            if directory:
                fd.setLabelText(QFileDialog.DialogLabel.FileName, "目录：")
            else:
                fd.setLabelText(QFileDialog.DialogLabel.FileName,
                                "文件名：" if save else "文件：")
            fd.setLabelText(QFileDialog.DialogLabel.FileType, "类型：")
            fd.setLabelText(QFileDialog.DialogLabel.Accept, accept_text)
            fd.setLabelText(QFileDialog.DialogLabel.Reject, "取消")
            if fd.exec() != QDialog.DialogCode.Accepted:
                return ""
            picked = fd.selectedFiles()
            return str(picked[0]) if picked else ""
        except Exception as e:  # noqa: BLE001
            self._set_status(f"打开文件选择框失败：{e}")
            return ""

    def _attach_file(self) -> None:
        """把本机文件作为上下文插入输入框（@路径），模型可用 fs_read 打开。"""
        start = self._state.get("workspace") or os.path.expanduser("~")
        path = self._pick_file("把文件加进这次提问", start, "所有文件 (*.*)",
                               accept_text="添加")
        if not path:
            return
        cur = self._input.toPlainText().rstrip()
        self._input.setPlainText(f"{cur} @{path} ".strip() if cur else f"@{path} ")
        self._input.moveCursor(QTextCursor.End)
        self._input.setFocus()

    # ---------------- 底栏 [+] 菜单：添加附件 + 清单中心 + 添加类 + 刷新 + 管理类 ----------------
    def _build_attach_menu(self) -> "QMenu":
        """[+] 弹出菜单（PRD §5.1.1，4 段）。

        原则：只保留「添加类」动作 + 一个汇总入口，管理类跳转压成末尾一组，
        避免 200% 缩放下菜单高到接近屏高 1/3（架构 §9.2 R-S5）。

            添加附件…
            ────
            能力清单（技能 / 工具 / MCP）…
            ────
            从清单添加技能… / 从清单添加 MCP…
            ────
            从本地文件夹导入技能… / 导入技能 zip… / 粘贴 URL 安装技能… / 粘贴 JSON 添加 MCP…
            ────
            刷新在线目录
            ────
            技能 / 工具 / MCP / 审计（原跳转，术语与 Tab 对应）
        """
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")  # 复用深浅样式
        add = menu.addAction("添加附件…")
        add.setToolTip("把本机文件加进本次提问（插入 @路径）")
        add.triggered.connect(self._attach_file)

        menu.addSeparator()
        hub = menu.addAction("能力清单（技能 / 工具 / MCP）…")
        hub.setToolTip("打开清单中心，停在技能页")
        hub.triggered.connect(lambda _c=False: self.open_manager("skills"))

        menu.addSeparator()
        act = menu.addAction("从清单添加技能…")
        act.setToolTip("打开清单中心并聚焦技能搜索框")
        act.triggered.connect(lambda _c=False: self.open_manager("skills", focus=True))
        act = menu.addAction("从清单添加 MCP…")
        act.setToolTip("打开清单中心的 MCP 在线目录")
        act.triggered.connect(lambda _c=False: self.open_manager("mcp"))

        menu.addSeparator()
        act = menu.addAction("从本地文件夹导入技能…")
        act.setToolTip("选中含 SKILL.md 的文件夹，复制进技能目录")
        act.triggered.connect(lambda _c=False: self._import_skill_from_dir())
        act = menu.addAction("导入技能 zip…")
        act.setToolTip("从 zip 技能包导入（与 OpenClaw / Claude 技能包同构）")
        act.triggered.connect(lambda _c=False: self._import_skill())
        act = menu.addAction("粘贴 URL 安装技能…")
        act.setToolTip("支持 GitHub / Gitee 的 SKILL.md 直链与含 /blob/ 的网页链接")
        act.triggered.connect(lambda _c=False: self._install_skill_from_url())
        act = menu.addAction("粘贴 JSON 添加 MCP…")
        act.setToolTip("粘贴 Claude Desktop 配置片段（mcpServers）即可添加")
        act.triggered.connect(lambda _c=False: self._paste_mcp_json())

        menu.addSeparator()
        act = menu.addAction("刷新在线目录")
        act.setToolTip("三源并行刷新（MCP Registry / GitHub 仓库 / 技能树）")
        act.triggered.connect(lambda _c=False: self._refresh_catalog())

        menu.addSeparator()
        for label, key in (("技能", "skills"), ("工具", "tools"),
                           ("MCP", "mcp"), ("审计", "audit")):
            act = menu.addAction(label)
            act.triggered.connect(lambda _c=False, k=key: self.open_manager(k))
        return menu

    def _show_attach_menu(self) -> None:
        self._popup_above(self._build_attach_menu(), self._attach_btn)

    # ---------------- 左栏二级栏目：任务 / 空间（挂到主窗口 NavPanel） ----------------
    def build_nav_panel(self) -> QWidget:
        """一级「智能助理」下面的二级栏目，语义与 WorkBuddy 一致：

            任务 (N)                跨空间的最近任务，置顶优先，点一条即切换
              · 任务标题
            空间 (M)                任务按所属工作区目录分组，点空间展开 / 收起
              ▸ 📁 llm-service  2
                  · 任务标题

        「任务」= 一次对话（后端仍叫 session，数据格式不变），
        「空间」= 任务所属的工作区目录，等价于 WorkBuddy 里挂在某个文件夹下的任务。
        """
        panel = QWidget()
        panel.setObjectName("NavSub")
        # 必须由页面强引用：主窗口 addWidget 之后父对象变了，但页面释放时也要能找回来
        self._nav_panel = panel
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(1)

        head = QHBoxLayout()
        head.setContentsMargins(20, 7, 10, 2)
        head.setSpacing(4)
        self._nav_task_title = QLabel("任务")
        self._nav_task_title.setObjectName("NavSubTitle")
        head.addWidget(self._nav_task_title)
        head.addStretch(1)
        add = QPushButton("+")
        add.setObjectName("NavSubAdd")
        add.setFixedSize(18, 18)
        add.setCursor(Qt.PointingHandCursor)
        add.setToolTip("新建任务")
        add.clicked.connect(lambda: self._new_session())
        head.addWidget(add)
        lay.addLayout(head)

        # 任务区 + 空间区共用一个可滚动容器：左栏高度有限，
        # 窗口矮的时候必须能在内部滚动，而不是把导航按钮挤没。
        self._nav_scroll = _NavScrollArea()
        self._nav_scroll.setObjectName("NavTaskScroll")
        self._nav_scroll.setMinimumHeight(64)
        self._nav_scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        body.setObjectName("NavTaskBody")
        self._nav_body = body
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        self._nav_body_lay = body_lay
        self._nav_scroll.setWidget(body)
        # stretch=1：让滚动区吃掉面板剩余高度（内容少时由 maxHeight 限制，不留白）
        lay.addWidget(self._nav_scroll, 1)

        self._rebuild_nav_tasks()
        return panel

    # ---------------- 左栏：任务 / 空间 渲染 ----------------
    def _clear_nav_body(self) -> None:
        lay = getattr(self, "_nav_body_lay", None)
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._nav_task_btns = {}

    def _task_tip(self, t: Dict[str, Any]) -> str:
        ts = t.get("updated_at") or 0
        when = time.strftime("%m月%d日 %H:%M", time.localtime(ts)) if ts else "—"
        return (f"{t.get('title') or '未命名任务'}\n"
                f"{t.get('message_count', 0)} 条消息 · {when}\n"
                f"空间：{t.get('workspace') or '（未记录）'}\n"
                f"右键：置顶 / 重命名 / 归档 / 删除")

    def _nav_task_row(self, t: Dict[str, Any], *, sub: bool = False) -> QPushButton:
        sid = str(t.get("id") or "")
        text = _ellipsis(t.get("title"), 11 if sub else 12)
        btn = QPushButton(("📌 " if t.get("pinned") else "") + text)
        btn.setObjectName("NavTaskItem")
        btn.setProperty("sub", "true" if sub else "false")
        btn.setProperty("active", "true" if sid == self._session_id else "false")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedHeight(NAV_ROW)
        btn.setToolTip(self._task_tip(t))
        btn.clicked.connect(lambda _c=False, s=sid: self._open_task(s))
        btn.setContextMenuPolicy(Qt.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, s=sid, b=btn: self._show_task_menu(s, b.mapToGlobal(pos)))
        self._nav_task_btns[sid] = btn
        return btn

    def _group_spaces(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按所属空间分组（与后端 store.space_list 同规则）。

        历史任务没有 workspace 字段（1.0.8 之前建的），统一归到当前工作区，
        否则左栏会多出一个「未归属」组，看起来像数据丢了。
        """
        cur = _norm_ws(self._state.get("workspace"))
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in rows:
            ws = _norm_ws(r.get("workspace")) or cur or "（未归属）"
            groups.setdefault(ws, []).append(r)
        out: List[Dict[str, Any]] = []
        for ws, tasks in groups.items():
            tasks.sort(key=lambda x: (not x.get("pinned"),
                                      -(x.get("updated_at") or 0)))
            out.append({
                "path": ws,
                "name": ws.rsplit("/", 1)[-1] or ws,
                "count": len(tasks),
                "exists": ws == "（未归属）" or os.path.isdir(ws),
                "updated_at": max((t.get("updated_at") or 0) for t in tasks),
                "tasks": tasks,
            })
        out.sort(key=lambda g: -(g.get("updated_at") or 0))
        return out

    def _rebuild_nav_tasks(self) -> None:
        """重画左栏「任务 / 空间」两区（数据变了就整体重建，逻辑最简单）。"""
        lay = getattr(self, "_nav_body_lay", None)
        if lay is None:
            return
        rows = getattr(self, "_nav_rows", None)
        if rows is None:
            rows = self._session_rows or []
        rows = [r for r in rows if isinstance(r, dict)]
        spaces = self._nav_spaces or self._group_spaces(rows)
        # 内容签名：refresh 可能被高频触发（状态轮询 / 切页 / 流式结束），
        # 每次清空重建会让左栏闪烁且丢鼠标 hover 态，签名没变就直接返回。
        sig = (tuple((r.get("id"), r.get("title"), r.get("pinned"),
                      r.get("message_count"), r.get("workspace")) for r in rows),
               tuple((sp.get("path"), sp.get("count")) for sp in spaces),
               self._open_space, self._session_id)
        if sig == getattr(self, "_nav_sig", None):
            return
        self._nav_sig = sig
        self._clear_nav_body()
        if self._nav_task_title is not None:
            self._nav_task_title.setText(f"任务 ({len(rows)})")
        lines = 0

        # ---- 任务区：跨空间最近任务（置顶的排最前，后端已排好）----
        for t in rows[:NAV_TASK_MAX]:
            lay.addWidget(self._nav_task_row(t))
            lines += 1
        if not rows:
            empty = QLabel("还没有任务，点右上角 + 新建")
            empty.setObjectName("NavEmpty")
            empty.setWordWrap(True)
            lay.addWidget(empty)
            lines += 1

        # ---- 空间区：任务按工作区目录分组，点空间展开 / 收起 ----
        if spaces:
            gtitle = QLabel(f"空间 ({len(spaces)})")
            gtitle.setObjectName("NavGroupTitle")
            lay.addWidget(gtitle)
            lines += 1              # 分组标题也占一行，漏算会把最后一行挤出视口
            for sp in spaces[:NAV_SPACE_MAX]:
                path = str(sp.get("path") or "")
                opened = bool(self._open_space) and path == self._open_space
                missing = not sp.get("exists", True)
                row = QPushButton(
                    f"{'▾' if opened else '▸'} {'⚠ ' if missing else '📁 '}"
                    f"{_ellipsis(sp.get('name') or path, 11)}   {sp.get('count')}")
                row.setObjectName("NavSpaceItem")
                row.setProperty("open", "true" if opened else "false")
                row.setCursor(Qt.PointingHandCursor)
                row.setFixedHeight(NAV_ROW)
                row.setToolTip(
                    f"{path}\n{sp.get('count')} 个任务"
                    + ("\n⚠ 目录不存在（可能被删除或移动盘已拔出）" if missing else "")
                    + "\n点击展开 / 收起；右键可在此空间新建任务")
                row.clicked.connect(lambda _c=False, p=path: self._toggle_space(p))
                row.setContextMenuPolicy(Qt.CustomContextMenu)
                row.customContextMenuRequested.connect(
                    lambda pos, p=path, b=row: self._show_space_menu(
                        p, b.mapToGlobal(pos)))
                lay.addWidget(row)
                lines += 1
                if not opened:
                    continue
                for t in (sp.get("tasks") or [])[:NAV_SPACE_TASK_MAX]:
                    lay.addWidget(self._nav_task_row(t, sub=True))
                    lines += 1
                if sp.get("count", 0) > NAV_SPACE_TASK_MAX:
                    more = QPushButton(f"查看全部 {sp.get('count')} 个…")
                    more.setObjectName("NavTaskItem")
                    more.setProperty("sub", "true")
                    more.setProperty("more", "true")
                    more.setCursor(Qt.PointingHandCursor)
                    more.setFixedHeight(NAV_ROW)
                    more.clicked.connect(lambda _c=False: self.open_manager("sessions"))
                    lay.addWidget(more)
                    lines += 1
            if len(spaces) > NAV_SPACE_MAX:
                more_sp = QPushButton(f"其余 {len(spaces) - NAV_SPACE_MAX} 个空间…")
                more_sp.setObjectName("NavTaskItem")
                more_sp.setProperty("more", "true")
                more_sp.setCursor(Qt.PointingHandCursor)
                more_sp.setFixedHeight(NAV_ROW)
                more_sp.clicked.connect(lambda _c=False: self.open_manager("sessions"))
                lay.addWidget(more_sp)
                lines += 1

        lay.addStretch(1)
        self._fit_nav_scroll(lines)
        self._dump_nav_debug(lines)

    def _dump_nav_debug(self, lines: int) -> None:
        """排查用：LLM_NAV_DBG=1 时把左栏各层高度与子控件几何写进日志。

        冻结包和源码跑出来不一样时（曾经出现过「空间」整段不显示），
        只能靠现场数据判断，所以留一个开关，默认不开。
        """
        if not os.environ.get("LLM_NAV_DBG"):
            return
        try:
            base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
            path = os.path.join(base, "llmBuddy", "logs", "nav_dbg.log")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            body = self._nav_body
            sc = self._nav_scroll
            rows = []
            lay = self._nav_body_lay
            for i in range(lay.count()):
                w = lay.itemAt(i).widget()
                if w is None:
                    rows.append(f"  [{i}] spacer")
                    continue
                rows.append("  [%d] %s %r geom=%s vis=%s h=%d hint=%d min=%d"
                            % (i, w.objectName(),
                               (w.text()[:20] if hasattr(w, "text") else ""),
                               w.geometry().getRect(), w.isVisible(), w.height(),
                               w.sizeHint().height(), w.minimumHeight()))
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("=== rebuild %s\n" % time.strftime("%H:%M:%S"))
                fh.write("lines=%d spaces=%d open=%r\n"
                         % (lines, len(self._nav_spaces or []), self._open_space))
                fh.write("scroll h=%d hint=%d min=%d max=%d viewport=%d\n"
                         % (sc.height(), sc.sizeHint().height(),
                            sc.minimumHeight(), sc.maximumHeight(),
                            sc.viewport().height()))
                fh.write("body h=%d hint=%d\n" % (body.height(), body.sizeHint().height()))
                # 祖先链：谁把滚动区压小了，一眼能看出来
                w = sc
                depth = 0
                while w is not None and depth < 6:
                    lay_w = w.layout()
                    fh.write("  ^%d %-12s h=%d hint=%d min=%d max=%d"
                             % (depth, w.objectName() or w.__class__.__name__,
                                w.height(), w.sizeHint().height(),
                                w.minimumHeight(), w.maximumHeight()))
                    if lay_w is not None:
                        fh.write(" layhint=%d spacing=%d"
                                 % (lay_w.sizeHint().height(), lay_w.spacing()))
                    fh.write("\n")
                    w = w.parentWidget()
                    depth += 1
                fh.write("\n".join(rows) + "\n")
        except Exception:                       # 调试代码不许影响界面
            pass

    def _fit_nav_scroll(self, rows: int = 0) -> None:
        """按行数算高，但允许被窗口压缩。

        刻意不依赖 `body.sizeHint()`：滚动区内部 widget 的布局在首次重建时
        还没 activate，sizeHint 会偏小，结果「空间」那一段被裁掉（实测过，
        任务区 4 行后再也看不到「空间 (N)」）。行数是自己数的，确定性强。

        也不能 setFixedHeight：主窗口最小高 680，左栏还要放标题 + 6 个一级入口
        + 4 个管理入口；这里给「最小可压到 96 / 最大不超过 NAV_SCROLL_MAX」，
        中间由布局协商，内容超出时滚动区内部消化（细条自动浮出）。
        """
        sc = getattr(self, "_nav_scroll", None)
        if sc is None:
            return
        h = max(96, min(rows * NAV_ROW + 4, NAV_SCROLL_MAX))
        # 最小 96（约 3 行）保证矮窗口下还能看到任务区；最大=内容高，
        # 内容少时不占一大片空白，内容多时由布局压缩 → 内部滚动。
        sc.setMinimumHeight(min(h, 96))
        sc.setMaximumHeight(h)
        if hasattr(sc, "set_hint_height"):
            sc.set_hint_height(h)
        self._relayout_nav()

    def _relayout_nav(self) -> None:
        """改完滚动区高度后，逐级把父布局标脏并立刻重排。

        打包进 exe 后出现过「hint 变了、父布局却不重算」：滚动区仍按旧高度
        （只够任务区）分配，最后一行「空间」被裁在视口外——源码跑同样的代码
        却正常，因为那边窗口 resize 顺带触发了重排。这里不赌事件顺序，主动
        invalidate + activate 整条链（滚动区 → 面板 → 左栏挂载点）。
        """
        chain = []
        w = self._nav_scroll
        while w is not None and len(chain) < 4:
            chain.append(w)
            w = w.parentWidget()
        for w in chain:
            w.updateGeometry()
            lay = w.layout()
            if lay is not None:
                lay.invalidate()
                lay.activate()

    def _toggle_space(self, path: str) -> None:
        # 手风琴：一次只展开一个空间，避免展开多个把左栏撑爆
        self._open_space = "" if path == (self._open_space or "") else path
        self._rebuild_nav_tasks()

    # ---------------- 任务操作（右键：置顶 / 重命名 / 归档 / 删除） ----------------
    def _open_task(self, sid: str) -> None:
        """点左栏一条任务 = 切到它（与 WorkBuddy 点任务开对话一致）。"""
        if not sid:
            return
        self._sel_sid = sid
        w = Worker(self._assist.session, sid)
        w.result.connect(self._apply_session_detail)
        w.error.connect(lambda m: self._set_status(f"打开任务失败：{m}"))
        w.start()

    def _patch_task(self, sid: str, patch: Dict[str, Any]) -> None:
        key = next(iter(patch), "")
        label = {"pinned": "置顶", "archived": "归档", "title": "重命名",
                 "workspace": "移动"}.get(key, "更新")
        self._set_status(f"正在{label}…")
        w = Worker(self._assist.update_session, sid, patch)
        w.result.connect(lambda _r: self._after_task_patched(label))
        w.error.connect(lambda m: self._set_status(f"{label}失败：{m}"))
        w.start()

    def _after_task_patched(self, label: str) -> None:
        self._set_status(f"已{label}")
        self._fill_sessions()      # 立刻重画左栏 + 管理面板列表
        self.refresh()             # 再拉一次 state：空间分组的顺序也会变

    def _rename_task(self, sid: str, cur: str) -> None:
        text, ok = QInputDialog.getText(
            self._dialog_parent(), "重命名任务", "任务名称：",
            QLineEdit.EchoMode.Normal, cur or "")
        if not ok:
            return
        text = (text or "").strip()
        if not text or text == cur:
            return
        self._patch_task(sid, {"title": text[:80]})

    def _delete_task(self, sid: str, title: str) -> None:
        ans = QMessageBox.question(
            self._dialog_parent(), "删除任务",
            f"确定删除「{title}」？\n删除后无法恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._set_status(f"正在删除「{title}」…")
        w = Worker(self._assist.delete_session, sid)
        w.result.connect(lambda res, s=sid: self._on_session_deleted(res, s))
        w.error.connect(self._on_session_delete_failed)
        w.start()

    def build_task_menu(self, sid: str) -> Optional[QMenu]:
        """任务右键菜单（对齐 WorkBuddy：置顶 / 重命名 / 打开文件夹 / 归档 / 删除）。

        「分享任务」在本机产品里换成「导出为 Markdown」——没有分享服务，
        但把任务交出去的需求是真实的；「在独立窗口中打开」不适用（单窗口应用），
        对应能力是「打开所在空间」。
        """
        row = next((r for r in self._session_rows if str(r.get("id")) == sid), {})
        if not row:
            return None
        title = row.get("title") or "未命名任务"
        pinned = bool(row.get("pinned"))
        archived = bool(row.get("archived"))
        ws = str(row.get("workspace") or "")
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")
        menu.addAction("取消置顶" if pinned else "置顶",
                       lambda: self._patch_task(sid, {"pinned": not pinned}))
        menu.addAction("重命名…", lambda: self._rename_task(sid, title))
        menu.addSeparator()
        act_dir = menu.addAction("打开所在空间", lambda: self._open_space_folder(ws))
        act_dir.setEnabled(bool(ws))
        menu.addAction("导出为 Markdown…", lambda: self._export_task(sid, title))
        menu.addSeparator()
        menu.addAction("取消归档" if archived else "归档",
                       lambda: self._patch_task(sid, {"archived": not archived}))
        menu.addAction("删除任务", lambda: self._delete_task(sid, title))
        return menu

    def _show_task_menu(self, sid: str, gpos) -> None:
        menu = self.build_task_menu(sid)
        if menu is not None:
            menu.exec(gpos)

    def build_space_menu(self, path: str) -> QMenu:
        """空间右键菜单：在此空间新建任务 / 展开收起 / 打开目录。"""
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")
        menu.addAction("在此空间新建任务", lambda: self._new_task_in_space(path))
        menu.addAction("展开 / 收起", lambda: self._toggle_space(path))
        menu.addSeparator()
        act = menu.addAction("在资源管理器中打开",
                             lambda: self._open_space_folder(path))
        act.setEnabled(os.path.isdir(path))
        return menu

    def _show_space_menu(self, path: str, gpos) -> None:
        self.build_space_menu(path).exec(gpos)

    def _new_task_in_space(self, path: str) -> None:
        """在该空间下新建任务，并把工作区切过去（文件工具落在同一目录）。"""
        name = _ellipsis(path.rsplit("/", 1)[-1] or path, 8)
        self._set_status(f"正在「{name}」中新建任务…")
        w = Worker(self._assist.new_session, path)
        w.result.connect(lambda s, p=path: self._on_space_task_created(s, p))
        w.error.connect(lambda m: self._set_status(f"新建任务失败：{m}"))
        w.start()

    def _on_space_task_created(self, session: Any, path: str) -> None:
        if not isinstance(session, dict) or not session.get("id"):
            self._set_status("任务创建失败，请确认后端已启动")
            return
        self._open_space = path
        self._sync_workspace_to_task(path)
        self._on_session_created(session, False)

    def _open_space_folder(self, path: str) -> None:
        p = _norm_ws(path)
        if not p or not os.path.isdir(p):
            self._set_status(f"空间目录不存在：{path or '（未记录）'}")
            return
        try:
            if hasattr(os, "startfile"):          # Windows
                os.startfile(p)                   # noqa: S606
            else:                                 # macOS / Linux
                import subprocess
                import sys
                subprocess.Popen(["open" if sys.platform == "darwin"
                                  else "xdg-open", p])
        except Exception as e:  # noqa: BLE001
            self._set_status(f"打开失败：{e}")

    def _export_task(self, sid: str, title: str) -> None:
        """WorkBuddy 的「分享任务」在本机产品里的等价能力：导出成 md 文件。"""
        w = Worker(self._assist.session, sid)
        w.result.connect(lambda s: self._save_task_md(s, title))
        w.error.connect(lambda m: self._set_status(f"导出失败：{m}"))
        w.start()

    def _save_task_md(self, session: Any, title: str) -> None:
        if not isinstance(session, dict):
            return
        safe = "".join(c for c in (title or "任务")
                       if c not in '\\/:*?"<>|').strip() or "任务"
        path = self._pick_file("导出任务", os.path.expanduser("~"),
                               "Markdown (*.md)", save=True,
                               default_name=f"{safe}.md", accept_text="保存")
        if not path:
            return
        if not path.lower().endswith(".md"):
            path += ".md"
        lines = [f"# {session.get('title') or title}", "",
                 f"- 空间：{session.get('workspace') or '（未记录）'}",
                 f"- 消息数：{len(session.get('messages') or [])}", ""]
        for m in session.get("messages") or []:
            role = {"user": "我", "assistant": "助理"}.get(m.get("role"),
                                                          m.get("role") or "")
            content = (m.get("content") or "").strip()
            if content:
                lines += [f"## {role}", "", content, ""]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            self._set_status(f"已导出：{path}")
        except OSError as e:
            self._set_status(f"写入失败：{e}")

    def _sync_workspace_to_task(self, path: str) -> None:
        """打开任务时把工作区切到它所属的空间。

        任务归属于空间，但文件工具用的是全局工作区；不同步就会出现
        「在 A 空间的任务里让助理改文件，结果改到了 B 目录」。
        """
        ws = _norm_ws(path)
        if not ws or ws == _norm_ws(self._state.get("workspace")):
            return
        if not os.path.isdir(ws):
            return
        self._state["workspace"] = ws
        self._remember_workspace(ws)
        self._refresh_ws_btn(ws)
        w = Worker(self._assist.save_settings, {"workspace": ws})
        w.error.connect(lambda m: self._set_status(f"切换空间失败：{m}"))
        w.start()


    # ---------------- 管理面板（能力清单中心：任务/技能/工具/MCP/审计） ----------------
    def open_manager(self, key: str = "sessions", focus: bool = False) -> None:
        """打开清单中心。

        key 取值（架构接缝 S5，已定稿）：`sessions` / `skills` / `tools` /
        `mcp` / `audit`；`focus=True` 时把焦点打到搜索框（`[技能]` 菜单的
        「从在线清单安装…」用它）。
        """
        if self._manager is None:
            self._manager = ManagerDialog(self)
        dlg = self._manager
        # 每次打开前用当前 state 回填，避免面板空着等下一次 refresh
        self._fill_skills(self._state.get("skills") or [])
        self._fill_tools(self._state.get("tools") or [])
        self._fill_mcp(self._state.get("mcp") or [])
        self._fill_sessions()
        if key == "audit":
            self._refresh_audit()
        dlg.show_tab(key)
        # 尺寸：主窗口 86% × 84%（架构 A7，逻辑 720×680 下必须放得下）
        dlg.fit_to_parent()
        # 在线区与工具偏好是懒加载的：打开时补一次（先渲染缓存，不阻塞）
        if key in ("skills", "mcp", "tools"):
            self._load_tool_prefs()
        if key in ("skills", "mcp") and getattr(dlg, "_online_visible", False):
            self._load_online_catalog(force=False)
        dlg.exec()
        if focus:
            # exec() 之后焦点已交给面板，这里把光标放到技能搜索框
            dlg.focus_search()

    def _build_sessions_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self._hint(
            f"左栏「任务」区只直接显示最近 {NAV_TASK_MAX} 条、" +
            f"「空间」区每空间至多 {NAV_SPACE_TASK_MAX} 条，全部任务都在这里。"
            "点选后可打开或删除（也支持右键菜单）。"))
        self._all_sess_list = QListWidget()
        self._all_sess_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection)
        self._all_sess_list.itemClicked.connect(self._on_all_sess_clicked)
        self._all_sess_list.itemDoubleClicked.connect(self._load_session_item)
        self._all_sess_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self._all_sess_list.customContextMenuRequested.connect(
            self._show_sess_menu)
        lay.addWidget(self._all_sess_list, 1)

        row = QHBoxLayout()
        b_new = QPushButton("新建任务")
        b_new.clicked.connect(lambda: self._new_session())
        b_open = QPushButton("打开")
        b_open.clicked.connect(self._open_selected_session)
        b_del = QPushButton("删除")
        b_del.setToolTip("删除选中的任务（单个）")
        b_del.clicked.connect(self._delete_session)
        b_sweep = QPushButton("清理空任务")
        b_sweep.setToolTip("批量删除 0 条消息的空任务（不含当前任务）")
        b_sweep.clicked.connect(self._sweep_empty_sessions)
        for b in (b_new, b_open, b_del):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(b_sweep)
        lay.addLayout(row)
        return w

    # ---------------- 任务列表：选中 / 打开 / 删除 ----------------
    def _on_all_sess_clicked(self, item) -> None:
        sid = item.data(Qt.UserRole) if item is not None else None
        self._sel_sid = str(sid) if sid else ""

    def _selected_session_sid(self) -> str:
        """当前要操作的任务：优先列表选中项，其次记住的选中，最后当前任务。

        列表会被 refresh 重建，重建瞬间 currentItem() 为 None；如果只认
        currentItem()，用户「刚选中就被刷新」后点删除就会静默失败
        （问题反馈：任务无法删除）。这里用 _sel_sid 兜底。
        """
        lst = getattr(self, "_all_sess_list", None)
        item = lst.currentItem() if lst is not None else None
        sid = item.data(Qt.UserRole) if item is not None else None
        return str(sid) if sid else (self._sel_sid or "")

    def _open_selected_session(self) -> None:
        lst = getattr(self, "_all_sess_list", None)
        item = lst.currentItem() if lst is not None else None
        if item is None:
            self._warn_no_selection("打开")
            return
        self._load_session_item(item)

    def _warn_no_selection(self, action: str) -> None:
        QMessageBox.information(
            self._dialog_parent(), f"{action}任务",
            "请先在列表里点选一个任务，再点这个按钮。")

    def _dialog_parent(self) -> QWidget:
        """当前可见的顶层窗口。

        管理面板是 exec() 起来的模态窗口。确认框/提示框如果还挂在被它遮住的
        页面上，弹窗会跑到面板背后 —— 用户看到的就是「点删除毫无反应」。
        """
        if self._manager is not None and self._manager.isVisible():
            return self._manager
        return self

    def _delete_session(self) -> None:
        lst = getattr(self, "_all_sess_list", None)
        item = lst.currentItem() if lst is not None else None
        sid = self._selected_session_sid()
        if not sid:
            self._warn_no_selection("删除")
            return
        raw = (item.text() if item is not None else "") or ""
        title = raw.split("（")[0].replace("★", "").strip() or "该任务"
        self._delete_task(sid, title)

    def _on_session_deleted(self, res, sid: str) -> None:
        if not (isinstance(res, dict) and res.get("deleted")):
            self._on_session_delete_failed("后端未确认删除（任务可能已不存在）")
            return
        if self._sel_sid == sid:
            self._sel_sid = ""
        if sid == self._session_id:
            # 删的是当前任务：清空并新开一个，否则界面停在一条已删除的任务上
            self._session_id = ""
            self._clear_chat()
            self._new_session(silent=True)
        else:
            self._fill_sessions()
        self._set_status("任务已删除")

    def _on_session_delete_failed(self, msg: str) -> None:
        self._set_status(f"删除失败：{msg}")
        QMessageBox.warning(self._dialog_parent(), "删除任务",
                            f"删除失败：{msg}")

    def _sweep_empty_sessions(self) -> None:
        """清掉 0 条消息的空任务。

        反复点「新建任务」或反复切页会自动建任务，列表里会攒一堆「新任务（0 条）」，
        单个删除太慢，这里给个一键清理（当前任务永远保留）。
        """
        empties = [s for s in self._session_rows
                   if not s.get("message_count") and s.get("id") != self._session_id]
        if not empties:
            QMessageBox.information(self._dialog_parent(), "清理空任务",
                                    "没有可清理的空任务。")
            return
        ans = QMessageBox.question(
            self._dialog_parent(), "清理空任务",
            f"将删除 {len(empties)} 个 0 条消息的空任务，确定？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if ans != QMessageBox.StandardButton.Yes:
            return
        for s in empties:
            Worker(self._assist.delete_session, str(s.get("id"))).start()
        self._set_status(f"正在清理 {len(empties)} 个空任务…")
        QTimer.singleShot(800, self._fill_sessions)

    def _show_sess_menu(self, pos) -> None:
        lst = getattr(self, "_all_sess_list", None)
        if lst is None:
            return
        item = lst.itemAt(pos)
        if item is None:
            return
        lst.setCurrentItem(item)
        self._on_all_sess_clicked(item)
        sid = item.data(Qt.UserRole)
        if not sid:
            return
        # 与左栏任务行共用同一套菜单（置顶 / 重命名 / 归档 / 删除）
        self._show_task_menu(str(sid), lst.mapToGlobal(pos))

    def _build_skills_tab(self) -> QWidget:
        """技能 Tab（PRD §5.2 / 架构 A7）：左列表 62% + 右详情 38%。

        - 左：`QSplitter` 内的 `QListWidget`（**属性名保持 `_skill_list`**），
          两行纯文本 `名称 · vX.Y` + `描述`，前置来源标识 `[内置]/[在线]/[已装]`；
          纯文本随 DPI 自动缩放，200% 缩放下不会像自绘卡片那样错位。
        - 右：只读 `QPlainTextEdit`（`_skill_detail`），**纯文本，不做 Markdown 渲染**
          （架构 §0.2 P2-3 降级决定）。
        - 工具条：搜索（name/description）+ 来源下拉 + 排序下拉 + ⟳ 刷新在线目录。
        - 宽 <720 时详情自动折叠（`_auto_fold_detail`）。
        """
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self._hint(
            "技能 = SKILL.md（frontmatter: name / description / tools）+ 正文；"
            "与 OpenClaw、Claude 技能包同构，可用 zip 互相导入导出。"))

        # ---- 工具条：搜索 / 来源 / 排序 / 刷新 ----
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._skill_search = QLineEdit()
        self._skill_search.setObjectName("HubSearch")
        self._skill_search.setPlaceholderText("搜索技能（名称 / 描述）")
        self._skill_search.setClearButtonEnabled(True)
        self._skill_search.textChanged.connect(
            lambda _t: self._fill_skills(self._state.get("skills") or []))
        bar.addWidget(self._skill_search, 1)
        self._skill_source = QComboBox()
        self._skill_source.setObjectName("HubCombo")
        for label, val in (("全部来源", "all"), ("内置", "builtin"),
                           ("本地", "local"), ("在线", "online")):
            self._skill_source.addItem(label, val)
        self._skill_source.currentIndexChanged.connect(
            lambda _i: self._fill_skills(self._state.get("skills") or []))
        bar.addWidget(self._skill_source)
        self._skill_sort = QComboBox()
        self._skill_sort.setObjectName("HubCombo")
        for label, val in (("默认排序", "default"), ("名称 A→Z", "name"),
                           ("⭐ star 降序", "stars")):
            self._skill_sort.addItem(label, val)
        self._skill_sort.currentIndexChanged.connect(
            lambda _i: self._fill_skills(self._state.get("skills") or []))
        bar.addWidget(self._skill_sort)
        b_ref = QPushButton("⟳ 刷新")
        b_ref.setObjectName("MiniBtn")
        b_ref.setToolTip("三源并行刷新在线目录（MCP Registry / GitHub / 技能树）")
        b_ref.clicked.connect(lambda _c=False: self._refresh_catalog())
        bar.addWidget(b_ref)
        lay.addLayout(bar)

        # ---- 左列表 + 右详情 ----
        self._skill_split = QSplitter(Qt.Horizontal)
        self._skill_split.setObjectName("HubSplitter")
        self._skill_split.setChildrenCollapsible(True)

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(4)
        self._skill_list = QListWidget()
        self._skill_list.setObjectName("HubList")
        self._skill_list.setWordWrap(False)
        self._skill_list.itemDoubleClicked.connect(self._edit_skill_item)
        self._skill_list.currentItemChanged.connect(
            lambda cur, _prev: self._on_skill_selected(cur))
        left_lay.addWidget(self._skill_list, 1)
        self._skill_split.addWidget(left)

        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(4)
        self._skill_detail = QPlainTextEdit()
        self._skill_detail.setObjectName("HubDetail")
        self._skill_detail.setReadOnly(True)
        self._skill_detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._skill_detail.setPlaceholderText("选中左侧技能查看详情")
        right_lay.addWidget(self._skill_detail, 1)
        det_row = QHBoxLayout()
        det_row.setSpacing(4)
        b_mount = QPushButton("挂载")
        b_mount.setObjectName("MiniBtn")
        b_mount.setToolTip("挂载到当前任务（之后每条消息带上 mounted_skills）")
        b_mount.clicked.connect(lambda _c=False: self._mount_selected_skill())
        b_export = QPushButton("导出 zip")
        b_export.setObjectName("MiniBtn")
        b_export.clicked.connect(lambda _c=False: self._export_skill())
        b_del = QPushButton("删除")
        b_del.setObjectName("MiniBtn")
        b_del.clicked.connect(lambda _c=False: self._delete_skill())
        for b in (b_mount, b_export, b_del):
            det_row.addWidget(b)
        det_row.addStretch(1)
        right_lay.addLayout(det_row)
        self._skill_split.addWidget(right)
        # 62% / 38%（PRD §5.2）
        self._skill_split.setStretchFactor(0, 62)
        self._skill_split.setStretchFactor(1, 38)
        self._skill_split.setSizes([620, 380])
        lay.addWidget(self._skill_split, 1)

        # ---- 按钮行 ----
        row1 = QHBoxLayout()
        row1.setSpacing(4)
        b_new = QPushButton("新建")
        b_new.clicked.connect(self._new_skill)
        b_edit = QPushButton("编辑")
        b_edit.clicked.connect(
            lambda: self._edit_skill_item(self._skill_list.currentItem()
                                          if self._skill_list is not None else None))
        b_import = QPushButton("导入 zip")
        b_import.clicked.connect(self._import_skill)
        b_dir = QPushButton("从文件夹导入")
        b_dir.setToolTip("选中含 SKILL.md 的文件夹")
        b_dir.clicked.connect(lambda _c=False: self._import_skill_from_dir())
        b_url = QPushButton("粘贴 URL")
        b_url.setToolTip("GitHub / Gitee 的 SKILL.md 直链")
        b_url.clicked.connect(lambda _c=False: self._install_skill_from_url())
        for b in (b_new, b_edit, b_import, b_dir, b_url):
            row1.addWidget(b)
        row1.addStretch(1)
        lay.addLayout(row1)

        # ---- 在线区：提示条 + 仓库列表（首屏先渲染缓存，不阻塞）----
        self._skill_online_banner = QLabel("")
        self._skill_online_banner.setObjectName("PageSubtitle")
        self._skill_online_banner.setWordWrap(True)
        self._skill_online_banner.setVisible(False)
        lay.addWidget(self._skill_online_banner)
        self._skill_online_list = QListWidget()
        self._skill_online_list.setObjectName("HubList")
        self._skill_online_list.setWordWrap(False)
        self._skill_online_list.setMaximumHeight(140)
        self._skill_online_list.itemDoubleClicked.connect(
            lambda it: self._open_online_repo(it))
        self._skill_online_list.setVisible(False)
        lay.addWidget(self._skill_online_list)
        self._skill_online_btn = QPushButton("🔍 浏览在线技能目录")
        self._skill_online_btn.setObjectName("MiniBtn")
        self._skill_online_btn.setToolTip(
            "GitHub topic:claude-skills 仓库列表（双击仓库枚举其中的 SKILL.md）")
        self._skill_online_btn.clicked.connect(
            lambda _c=False: self._toggle_online_skills())
        lay.addWidget(self._skill_online_btn)
        return w

    def _toggle_online_skills(self) -> None:
        """展开/收起在线技能仓库列表（展开时立即拉缓存 + 后台刷新）。"""
        lst = getattr(self, "_skill_online_list", None)
        if lst is None:
            return
        show = not lst.isVisibleTo(self)
        lst.setVisible(show)
        btn = getattr(self, "_skill_online_btn", None)
        if btn is not None:
            btn.setText("⇱ 收起在线技能目录" if show else "🔍 浏览在线技能目录")
        if show:
            self._load_online_catalog(force=False)

    def _open_online_repo(self, item) -> None:
        """双击在线仓库 → 枚举它的技能（GET /catalog/skills/{owner}/{repo}）。"""
        repo = (item.data(Qt.UserRole) if item is not None else None) or {}
        full = str(repo.get("full_name") or "")
        if "/" not in full:
            return
        owner, name = full.split("/", 1)
        ref = str(repo.get("default_branch") or "")
        self._set_status(f"正在枚举 {full} 里的技能…")
        w = Worker(self._assist.catalog_repo_skills, owner, name, ref, False)
        w.result.connect(lambda res, r=repo: self._on_repo_skills(res, r))
        w.error.connect(lambda m: self._set_status(f"枚举仓库失败：{m}"))
        w.start()

    def _on_repo_skills(self, res: Any, repo: Dict[str, Any]) -> None:
        items = (res or {}).get("items") if isinstance(res, dict) else None
        if not items:
            self._set_status("该仓库里没有找到 SKILL.md")
            return
        full = str(repo.get("full_name") or "")
        ref = str(repo.get("default_branch") or "")
        stars = int(repo.get("stars") or 0)
        for it in items:
            if not isinstance(it, dict):
                continue
            slug = str(it.get("slug") or "")
            ok = self._confirm_install_skill(
                repo=full, ref=ref, dirpath=str(it.get("dir") or ""),
                slug=slug, stars=stars,
                files=list(it.get("files") or []),
                has_scripts=bool(it.get("has_scripts")),
                description=str(it.get("description") or ""))
            if not ok:
                continue
            self._set_status(f"正在安装技能 {slug}…")
            w = Worker(self._assist.catalog_install_skill, full, ref,
                       str(it.get("dir") or ""), slug, False)
            w.result.connect(self._on_installed_online_skill)
            w.error.connect(lambda m, s=slug: self._on_install_failed(m, s))
            w.start()

    def _on_installed_online_skill(self, res: Any) -> None:
        slug = (res or {}).get("slug") if isinstance(res, dict) else ""
        self.refresh()
        self._set_status(f"已安装技能：{slug or '（未知）'}")
        self._auto_refresh_manager_lists()

    def _confirm_install_skill(self, repo: str, ref: str, dirpath: str,
                               slug: str, stars: int, files: List[str],
                               has_scripts: bool, description: str) -> bool:
        """在线技能安装确认框（P0-7 强制项：来源 / ⭐ / 文件数 / 脚本警示 / 风险）。"""
        lines = [
            f"来源：{repo}" + (f" · {dirpath}" if dirpath else ""),
            f"标识：{slug}",
            f"⭐ star：{stars}",
            f"文件数：{len(files)}" + (f"（{'、'.join(files[:6])}…）"
                                      if len(files) > 6 else
                                      (f"（{'、'.join(files)}）" if files else "")),
            "风险等级：medium（技能正文只会作为提示词，但可能引导模型调用高危工具）",
        ]
        if description:
            lines.insert(2, f"说明：{description}")
        if has_scripts:
            lines.append("")
            lines.append("⚠ 该技能包含 scripts/ 或可执行文件，运行时有额外风险。")
        lines.append("")
        lines.append("安装只会落文件，不会自动执行任何下载到的脚本。")
        ans = QMessageBox.question(
            self._dialog_parent(), "从在线来源安装",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return ans == QMessageBox.StandardButton.Yes

    def _confirm_install_mcp(self, item: Dict[str, Any],
                             suggest: Dict[str, Any]) -> bool:
        """在线 MCP 安装确认框（含传输 / 是否需密钥 / 风险=high）。"""
        required = [str(e.get("name")) for e in
                    (suggest.get("required_env") or []) if isinstance(e, dict)]
        transport = str(suggest.get("transport") or item.get("transport") or "stdio")
        lines = [
            f"来源：Registry {item.get('registry_name') or ''}",
            f"名称：{item.get('title') or item.get('name') or ''}",
            f"版本：{item.get('version') or '（未标注）'}",
            f"传输：{'远程 HTTP' if transport == 'http' else '本地子进程（stdio）'}",
            f"是否需密钥：{'需要（' + '、'.join(required) + '）' if required else '不需要'}",
            "风险等级：high（本地子进程 = 任意代码执行，运行时逐次审批）",
        ]
        ans = QMessageBox.question(
            self._dialog_parent(), "从在线来源安装 MCP",
            "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return ans == QMessageBox.StandardButton.Yes

    def _mount_selected_skill(self) -> None:
        item = self._skill_list.currentItem() if self._skill_list is not None else None
        slug = item.data(Qt.UserRole) if item is not None else None
        if slug:
            self._add_skill(str(slug))

    def _on_skill_selected(self, item) -> None:
        """选中技能 → 右侧详情面板纯文本渲染（不做 Markdown 预览）。"""
        det = getattr(self, "_skill_detail", None)
        if det is None:
            return
        slug = item.data(Qt.UserRole) if item is not None else None
        if not slug or not isinstance(slug, str) or slug.startswith("（"):
            det.setPlainText("")
            return
        cached = getattr(self, "_skill_bodies", {}).get(slug)
        if cached is not None:
            det.setPlainText(cached)
            return
        det.setPlainText("正在加载…")
        w = Worker(self._assist.skill, slug)
        w.result.connect(lambda data, s=slug: self._apply_skill_detail(s, data))
        w.error.connect(lambda m: det.setPlainText(f"加载失败：{m}"))
        w.start()

    def _apply_skill_detail(self, slug: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        meta = data.get("meta") or {}
        src = (self._state.get("skill_sources") or {}).get(slug) or {}
        files = data.get("files") or []
        lines = [
            str(meta.get("name") or slug),
            "─" * 24,
            f"标识：{slug}",
            f"版本：{meta.get('version') or '1.0.0'}",
            f"来源：{'内置' if src.get('builtin') else (src.get('repo') or '本地')}",
            f"允许工具：{'、'.join(meta.get('tools') or []) or '（未限制）'}",
            f"文件：{'、'.join(str(f) for f in files) or 'SKILL.md'}",
            "",
            "描述：",
            str(meta.get("description") or "（无）"),
            "",
            "正文预览（前 400 字）：",
            str(data.get("body") or "")[:400],
        ]
        text = "\n".join(lines)
        if not hasattr(self, "_skill_bodies"):
            self._skill_bodies: Dict[str, str] = {}
        self._skill_bodies[slug] = text
        det = getattr(self, "_skill_detail", None)
        if det is not None and (self._skill_list.currentItem() is not None) and \
                self._skill_list.currentItem().data(Qt.UserRole) == slug:
            det.setPlainText(text)

    def _build_tools_tab(self) -> QWidget:
        """工具 Tab（P0-5）：顶部常驻「当前下发 N / 上限 M」+ 行内 QCheckBox 开关。

        行内开关用 `setItemWidget(item, QCheckBox)`，勾选变化即 PUT /tools/prefs；
        关闭高危 / 技能类工具要二次确认（`_tool_needs_confirm`）。
        `_tool_list` 属性名保持不变（既有测试 `:609` 依赖 `count()`）。
        """
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(6)
        self._tool_count_lbl = QLabel("当前下发 0 / 上限 24")
        self._tool_count_lbl.setObjectName("HubHeadCount")
        head.addWidget(self._tool_count_lbl)
        head.addStretch(1)
        b_refresh = QPushButton("⟳ 刷新")
        b_refresh.setObjectName("MiniBtn")
        b_refresh.clicked.connect(lambda _c=False: self._load_tool_prefs())
        head.addWidget(b_refresh)
        lay.addLayout(head)

        lay.addWidget(self._hint(
            "只读工具自动执行；写入 / 高危按底栏的审批策略处理。"
            "超出上限的工具会标灰不下发给模型。"))

        self._tool_list = QListWidget()
        self._tool_list.setObjectName("HubList")
        lay.addWidget(self._tool_list, 1)

        #: 行内开关：工具名 → QCheckBox（`_fill_tools` 重建，测试断言用）
        self._tool_checks: Dict[str, Any] = {}
        return w

    def _load_tool_prefs(self) -> None:
        """GET /tools/prefs → 更新顶部计数与列表开关态。"""
        w = Worker(self._assist.tool_prefs)
        w.result.connect(self._apply_tool_prefs)
        w.error.connect(lambda m: self._set_status(f"读取工具偏好失败：{m}"))
        w.start()

    def _apply_tool_prefs(self, data: Any) -> None:
        if isinstance(data, dict) and data:
            self._tool_prefs = dict(data)
        self._fill_tools(self._state.get("tools") or [])

    def _load_profiles(self) -> None:
        """GET /profiles → `_profiles`（auto_load 开关 + 3 个内置 profile）。"""
        w = Worker(self._assist.profiles)
        w.result.connect(self._apply_profiles)
        w.error.connect(lambda m: None)      # 非关键路径：失败就按默认「开」处理
        w.start()

    def _apply_profiles(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self._profiles = dict(data)
        if self._profiles.get("auto_load") is False:
            self._auto_off = True

    def _build_mcp_tab(self) -> QWidget:
        """MCP Tab（P0-3）：在线清单安装入口 + 手动添加 + 连接/断开 + 删除。

        `_mcp_list` 属性名保持不变（既有测试依赖）。
        """
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self._hint(
            "连接成功后，MCP 工具自动以 mcp__<标识>__<工具> 进白名单，审批规则同上。"))

        head = QHBoxLayout()
        head.setSpacing(4)
        self._mcp_count_lbl = QLabel("已配置 0 台 · 已连接 0 台")
        self._mcp_count_lbl.setObjectName("HubHeadCount")
        head.addWidget(self._mcp_count_lbl)
        head.addStretch(1)
        lay.addLayout(head)

        self._mcp_list = QListWidget()
        self._mcp_list.setObjectName("HubList")
        lay.addWidget(self._mcp_list, 1)

        # ---- 在线区（默认收起，点「从在线清单安装」展开）----
        self._mcp_online_banner = QLabel("")
        self._mcp_online_banner.setObjectName("PageSubtitle")
        self._mcp_online_banner.setWordWrap(True)
        self._mcp_online_banner.setVisible(False)
        lay.addWidget(self._mcp_online_banner)
        self._mcp_online_list = QListWidget()
        self._mcp_online_list.setObjectName("HubList")
        self._mcp_online_list.setWordWrap(False)
        self._mcp_online_list.setMaximumHeight(180)
        self._mcp_online_list.itemDoubleClicked.connect(
            lambda it: self._install_online_mcp(it))
        self._mcp_online_list.setVisible(False)
        lay.addWidget(self._mcp_online_list)
        self._mcp_online_btn = QPushButton("🔍 从在线清单安装 MCP…")
        self._mcp_online_btn.setObjectName("MiniBtn")
        self._mcp_online_btn.setToolTip(
            "官方 MCP Registry：搜索并自动生成 npx / http 配置（风险默认 high）")
        self._mcp_online_btn.clicked.connect(
            lambda _c=False: self._toggle_online_mcp())
        lay.addWidget(self._mcp_online_btn)

        row = QHBoxLayout()
        row.setSpacing(4)
        b_add = QPushButton("手动添加")
        b_add.setToolTip("手填命令 / 参数 / URL / 风险（stdio / http）")
        b_add.clicked.connect(self._add_mcp)
        b_json = QPushButton("粘贴 JSON")
        b_json.setToolTip("粘贴 Claude Desktop 的 mcpServers 片段")
        b_json.clicked.connect(lambda _c=False: self._paste_mcp_json())
        b_conn = QPushButton("连接/断开")
        b_conn.clicked.connect(self._toggle_mcp)
        b_rm = QPushButton("删除")
        b_rm.clicked.connect(self._delete_mcp)
        for b in (b_add, b_json, b_conn, b_rm):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    def _toggle_online_mcp(self) -> None:
        """展开/收起在线 MCP 列表（展开时拉缓存 + 后台刷新）。"""
        lst = getattr(self, "_mcp_online_list", None)
        if lst is None:
            return
        show = not lst.isVisibleTo(self)
        lst.setVisible(show)
        btn = getattr(self, "_mcp_online_btn", None)
        if btn is not None:
            btn.setText("⇱ 收起在线目录" if show else "🔍 从在线清单安装 MCP…")
        if show:
            self._load_online_catalog(force=False)

    def _install_online_mcp(self, item) -> None:
        """双击在线 MCP 条目 → 确认框 → POST /catalog/mcp/install。"""
        it = (item.data(Qt.UserRole) if item is not None else None) or {}
        suggest = it.get("suggest") or {}
        if not isinstance(suggest, dict):
            suggest = {}
        if str(it.get("free_class") or "") == "unknown":
            QMessageBox.information(
                self._dialog_parent(), "无法安装",
                "该条目在 Registry 里只登记了元数据（没有 packages / remotes），"
                "无法自动生成配置。可点「查看源」到上游看手动配置。")
            return
        required = [e for e in (suggest.get("required_env") or [])
                    if isinstance(e, dict) and e.get("name")]
        env: Dict[str, str] = {}
        if required:
            # 必填环境变量逐项收集；缺一项不允许安装（2024 的客户端前置）
            for e in required:
                name = str(e.get("name"))
                val, ok = QInputDialog.getText(
                    self._dialog_parent(), "填写环境变量",
                    f"{name}（{e.get('description') or '必填'}）：",
                    QLineEdit.EchoMode.Normal, "")
                if not ok or not (val or "").strip():
                    QMessageBox.warning(self._dialog_parent(), "缺少必填变量",
                                        f"该服务器需要 {name}，请填写后再安装。")
                    return
                env[name] = str(val).strip()
        if not self._confirm_install_mcp(it, suggest):
            return
        reg = str(it.get("registry_name") or "")
        self._set_status(f"正在安装 MCP：{it.get('title') or reg}…")
        w = Worker(self._assist.catalog_install_mcp, reg, "", "", env, "",
                   True)
        w.result.connect(self._on_installed_online_mcp)
        w.error.connect(lambda m, r=reg: self._on_install_failed(m, r))
        w.start()

    def _on_installed_online_mcp(self, res: Any) -> None:
        res = res if isinstance(res, dict) else {}
        err = res.get("error")
        self.refresh()
        self._auto_refresh_manager_lists()
        if err:
            self._set_status(f"已保存配置但连接失败：{str(err)[:120]}")
            QMessageBox.warning(
                self._dialog_parent(), "MCP 连接失败",
                f"已保存配置但连接失败：\n{err}\n\n"
                "可检查 npx 是否可用、环境变量是否完整；配置已保留，可在列表里改。")
        else:
            self._set_status(
                f"已安装 MCP：{(res.get('server') or {}).get('name') or ''}")
        if isinstance(self._tool_prefs, dict):
            self._apply_tool_prefs(self._tool_prefs)

    def _build_audit_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self._hint("每次工具调用 / 审批结果都落在 audit 目录的 jsonl 里，"
                                 "这里是最新记录。"))
        self._audit_view = QPlainTextEdit()
        self._audit_view.setReadOnly(True)
        lay.addWidget(self._audit_view, 1)
        row = QHBoxLayout()
        b_ref = QPushButton("刷新审计")
        b_ref.clicked.connect(self._refresh_audit)
        row.addWidget(b_ref)
        row.addStretch(1)
        lay.addLayout(row)
        return w

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("PageSubtitle")
        lbl.setWordWrap(True)
        return lbl

    # ------------------------------------------------------------------ #
    # 刷新：状态 / 底栏按钮 / 技能 / 工具 / MCP / 任务
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self._assist = AssistantApi(self.config.gateway_url(), self.config.api_key)
        w = Worker(self._assist.state)
        w.result.connect(self._apply_state)
        w.error.connect(self._on_state_error)
        w.start()

    def _on_state_error(self, msg: str) -> None:
        self._loading = False
        self._set_status(f"后端未就绪：{msg}（请先在控制台启动服务）")

    def _apply_state(self, st: Dict[str, Any]) -> None:
        self._loading = False
        if not isinstance(st, dict):
            return
        self._state = st
        ws = str(st.get("workspace") or "")
        # 本机记忆兜底：后端可能因为「配置被重置 / PUT 没发出去（上次闪退）」
        # 而回报空或默认工作区。上次的选择记在客户端 config.json 里，优先用它，
        # 并顺手补同步给后端，避免「重开页面接不上上次工作区」。
        mirror = str(getattr(self.config, "assistant_workspace", "") or "")
        if mirror and os.path.isdir(mirror) and (
                not ws or os.path.normcase(mirror) != os.path.normcase(ws)):
            ws = mirror
            self._sync_workspace_once(mirror)
        elif ws and mirror and os.path.normcase(mirror) != os.path.normcase(ws):
            # 后端有值但与本机记录不同：以后端为准，回写本机记录保持一致
            self._remember_workspace(ws)
        ws_err = str(st.get("workspace_error") or "")
        if ws_err:
            self._set_status(ws_err)
        ws_name = os.path.basename(ws.rstrip("\\/")) if ws else ""
        orch = "LangGraph 编排" if st.get("orchestrator") == "langgraph" else "顺序降级编排"
        self._subtitle.setText(f"{orch}   ·   工作区 {ws_name or '（未设置）'}")
        self._subtitle.setToolTip(ws or "（未设置）")

        # 底栏三件套：工作区 / 审批策略 / 模型
        self._refresh_ws_btn(ws)
        self._refresh_policy_btn()
        pid = str(st.get("active_provider") or "local")
        self._active_provider = pid
        if not self._active_model:
            provider = next((p for p in (st.get("providers") or [])
                             if p.get("id") == pid), {})
            self._active_model = str(provider.get("model") or "")
        self._fill_models(pid)

        self._skill_btn.setIcon(self._skill_icon())
        self._fill_skills(st.get("skills") or [])
        self._refresh_skill_chips()
        self._fill_tools(st.get("tools") or [])
        self._fill_mcp(st.get("mcp") or [])
        self._fill_sessions()
        # 1.0.8：工具偏好与 profile（后端可能已回填在 state，也可能要走专端点）。
        # 走专端点是为了拿到 `active` / `over` 这类只有后端能算准的字段。
        if not self._tool_prefs:
            self._load_tool_prefs()
        if not self._profiles:
            self._load_profiles()

        # 首次进入不要无脑新建：先复用最近一个「0 条消息」的任务。
        # 否则每启动一次就多一个空任务，左栏「任务 / 空间」两区很快被刷满。
        if not self._session_id:
            rows = [s for s in (st.get("sessions") or []) if isinstance(s, dict)]
            idle = next((s for s in rows if not s.get("message_count")), None)
            if idle is not None:
                self._session_id = str(idle.get("id") or "")
                self._sel_sid = self._session_id
                self._greet()
            else:
                self._new_session(silent=True)

    def _set_status(self, text: str) -> None:
        """状态行：有内容才占位，避免底栏上方常驻一条空白。"""
        self._status.setText(text or "")
        self._status.setVisible(bool((text or "").strip()))

    # ---------------- 底栏按钮文本 ----------------
    WS_LABEL_MAX = 4            # 宽度压缩规则 ①：12 → 4 字（完整路径进 tooltip）

    def _refresh_ws_btn(self, ws: str = "") -> None:
        """工作区按钮显示目录名（WorkBuddy 的「分支 master」位：当前上下文根）。

        宽度压缩规则 ①：逻辑 720 宽下底栏只有 504px 可用，目录名按 12 字显示
        会把整条底栏顶出屏幕（架构 §5.4 求和：`_ws_btn` 107 → 58）。
        文案压到 ≤4 字，完整路径与目录全名都放 tooltip，信息不丢。
        """
        path = (ws or str(self._state.get("workspace") or "")
                or str(getattr(self.config, "assistant_workspace", "") or ""))
        name = os.path.basename(path.rstrip("\\/")) if path else ""
        full = name or "工作区"
        self._ws_btn.setText(_ellipsis(name, self.WS_LABEL_MAX) if name else "工作区")
        self._ws_btn.setToolTip(
            f"文件工具可访问的工作区：{full}\n{path or '（未设置）'}\n（点击切换）")

    def _refresh_policy_btn(self) -> None:
        pid = str(self._state.get("policy") or "readonly_auto")
        label = next((lb for p, lb, _t in POLICY_ITEMS if p == pid), pid)
        tip = next((t for p, _lb, t in POLICY_ITEMS if p == pid), "")
        # 宽度压缩：底栏只有 504px，「⊘ 只读自动」(80px) 太宽。
        # 按钮上只显示策略短名（≤2 字），完整说明留在 tooltip。
        short = POLICY_SHORT.get(pid, label)
        self._policy_btn.setText(f"{POLICY_ICON.get(pid, '⊘')} {short}")
        self._policy_btn.setToolTip(f"审批策略：{label}\n{tip}\n（点击切换）")

    def _skill_icon(self) -> QIcon:
        """WorkBuddy 风格的技能图标：四角星芒 + 伴星，颜色随深浅主题切换。"""
        bg = str(getattr(self.config, "background_color", "") or "#0f1117")
        color = QColor("#e8ecf5" if not is_light(bg) else "#232a38")
        s = 32                      # 2x 绘制抗锯齿，显示时缩到 14~16px
        pm = QPixmap(s, s)
        pm.fill(Qt.GlobalColor.transparent)

        def star(cx: float, cy: float, r: float) -> QPainterPath:
            path = QPainterPath()
            path.moveTo(cx, cy - r)
            path.quadTo(cx, cy, cx + r, cy)
            path.quadTo(cx, cy, cx, cy + r)
            path.quadTo(cx, cy, cx - r, cy)
            path.quadTo(cx, cy, cx, cy - r)
            return path

        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawPath(star(s * 0.42, s * 0.58, s * 0.34))   # 主星（左下）
        p.drawPath(star(s * 0.78, s * 0.22, s * 0.15))   # 伴星（右上）
        p.end()
        return QIcon(pm)

    MODEL_LABEL_MAX = 8         # 宽度压缩规则 ②：26 → 8 字（全名进 tooltip）
    #: 规则②的硬上限：8 字 + 「 ▾」在 200% 缩放下实测 ≤96px，留到 100px。
    #: 不能再大：忙碌态底栏 439 + 56 = 495 ≤ 504 是架构 §4 的定案。
    MODEL_BTN_MAX = 100

    def _refresh_model_btn(self) -> None:
        """模型按钮只显短名（宽度压缩规则 ②：90 → 66px），全名进 tooltip。"""
        names = self._model_cache.get(self._active_provider) or []
        model = self._active_model or (names[0] if names else "")
        provider = next((p for p in (self._state.get("providers") or [])
                         if p.get("id") == self._active_provider), {})
        plabel = provider.get("label") or self._active_provider or "模型"
        text = _ellipsis(model, self.MODEL_LABEL_MAX) if model else "选择模型"
        self._model_btn.setText(f"{text} ▾")
        # 规则②的硬保障：不同平台 fontMetrics + 全局 QSS padding 会把按钮撑到
        # 130px+，直接顶爆底栏 504px 门禁。钉死上限，超出部分进 tooltip。
        self._model_btn.setMaximumWidth(self.MODEL_BTN_MAX)
        self._model_btn.setToolTip(
            f"当前：{plabel} · {model or '（默认）'}\n"
            f"端点：{provider.get('base_url') or '（本机）'}\n"
            "点击切换来源 / 模型，或配置密钥与端点")

    # ---------------- 模型清单 ----------------
    def _fill_models(self, provider_id: str) -> None:
        """采集模型名（本地引擎走后台请求，避免阻塞 UI）。"""
        provider = next((p for p in (self._state.get("providers") or [])
                         if p.get("id") == provider_id), {})
        # 硅基流动：首次进入时把对话页已存的 Key 同步到助理配置，免二次输入
        if provider_id == "siliconflow" and not (provider.get("api_key") or ""):
            key = str(self._settings.value("chat/cloud_api_key", "") or "").strip()
            if key:
                self._save_provider_patch("siliconflow", {"api_key": key})

        self._refresh_model_btn()

        def collect() -> List[str]:
            if provider_id == "local":
                try:
                    # 只列「真的在模型目录里」的模型：downloaded 由网关按 gguf
                    # 是否存在标记；loaded（运行中）必然存在。
                    return [m.get("id") for m in self.api.list_models()
                            if isinstance(m, dict) and m.get("id")
                            and (m.get("downloaded") or m.get("loaded"))]
                except Exception:
                    return []
            if provider_id != "siliconflow":
                # 自定义（OpenAI 兼容）提供商：模型名来自提供商配置本身
                m = str(provider.get("model") or "").strip()
                return [m] if m else []
            try:
                items = json.loads(self._settings.value("chat/cloud_models", "") or "[]")
            except Exception:
                return []
            return [it["name"] for it in items
                    if isinstance(it, dict) and it.get("name")]

        w = Worker(collect)
        w.result.connect(
            lambda names, pid=provider_id: self._apply_models(pid, names or []))
        w.start()

    def _apply_models(self, provider_id: str, names: List[str]) -> None:
        self._model_cache[provider_id] = names
        if provider_id != self._active_provider:
            return
        if not self._active_model and names:
            self._active_model = names[0]
        self._refresh_model_btn()

    # ---------------- 二级栏目 / 管理面板填充 ----------------
    def _skill_source_of(self, s: Dict[str, Any]) -> str:
        """技能来源：builtin（内置）/ online（在线装过）/ local（本地手写）。"""
        slug = str(s.get("slug") or "")
        if s.get("builtin") or slug in ("workspace-organizer", "repo-digest",
                                        "web-brief"):
            return "builtin"
        src = (self._state.get("skill_sources") or {}).get(slug) or {}
        if src.get("kind") in ("github", "url"):
            return "online"
        return "local"

    def _filtered_skills(self, skills: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """搜索（name/description）+ 来源筛选 + 排序（PRD §5.2 三件套）。"""
        rows = [s for s in skills if isinstance(s, dict)]
        q = ""
        if getattr(self, "_skill_search", None) is not None:
            q = (self._skill_search.text() or "").strip().lower()
        if q:
            rows = [s for s in rows
                    if q in str(s.get("name") or "").lower()
                    or q in str(s.get("description") or "").lower()
                    or q in str(s.get("slug") or "").lower()]
        src_val = "all"
        if getattr(self, "_skill_source", None) is not None:
            src_val = str(self._skill_source.currentData() or "all")
        if src_val != "all":
            rows = [s for s in rows if self._skill_source_of(s) == src_val]
        sort_val = "default"
        if getattr(self, "_skill_sort", None) is not None:
            sort_val = str(self._skill_sort.currentData() or "default")
        if sort_val == "name":
            rows.sort(key=lambda s: str(s.get("name") or s.get("slug") or "").lower())
        elif sort_val == "stars":
            def stars(s):
                slug = str(s.get("slug") or "")
                src = (self._state.get("skill_sources") or {}).get(slug) or {}
                return -int(src.get("stars") or 0)
            rows.sort(key=stars)
        return rows

    def _fill_skills(self, skills: List[Dict[str, Any]]) -> None:
        """技能列表：两行文本（`名称 · vX.Y` + 描述）+ 前置来源标识（PRD §5.2）。"""
        lst = getattr(self, "_skill_list", None)
        if lst is None:
            return
        rows = self._filtered_skills(skills)
        keep = ""
        cur = lst.currentItem()
        if cur is not None and cur.data(Qt.UserRole):
            keep = str(cur.data(Qt.UserRole))
        lst.blockSignals(True)
        lst.clear()
        for s in rows:
            slug = str(s.get("slug") or "")
            name = s.get("name") or slug
            ver = str(s.get("version") or "1.0.0")
            badge = {"builtin": "[内置]", "online": "[在线]",
                     "local": "[已装]"}.get(self._skill_source_of(s), "[已装]")
            desc = str(s.get("description") or "").strip() or "（无描述）"
            item = QListWidgetItem(f"{badge} {name} · v{ver}\n{desc}")
            item.setData(Qt.UserRole, slug)
            item.setToolTip(f"{name}\n{desc}\n来源：{badge}\n"
                            f"工具：{'、'.join(s.get('tools') or []) or '（未限制）'}")
            lst.addItem(item)
            if slug and slug == keep:
                lst.setCurrentItem(item)
        if not rows:
            hint = ("（没有匹配的技能：清空搜索或改来源筛选）" if (skills and rows != skills)
                    else "（暂无技能：点下面「从文件夹导入」/「粘贴 URL」，"
                         "或展开在线技能目录）")
            lst.addItem(QListWidgetItem(hint))
        lst.blockSignals(False)

    def _fill_tools(self, tools: List[Dict[str, Any]]) -> None:
        """工具 Tab：行内 QCheckBox 开关（读 `enabled`）+ 顶部下发计数（P0-5）。"""
        lst = getattr(self, "_tool_list", None)
        if lst is None:
            return
        snap = self._tool_prefs_snapshot()
        lbl = getattr(self, "_tool_count_lbl", None)
        if lbl is not None:
            lbl.setText(f"当前下发 {snap['active']} / 上限 {snap['max_tools']} 个"
                        + (f"（{len(snap['over'])} 个超出上限未下发）"
                           if snap["over"] else ""))
        over = set(snap["over"])
        disabled = set(snap["disabled"])
        lst.clear()
        self._tool_checks = {}
        for t in tools:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "")
            risk = RISK_TEXT.get(t.get("risk"), t.get("risk") or "")
            src = t.get("source") or "builtin"
            enabled = name not in disabled and bool(t.get("enabled", True))
            item = QListWidgetItem()
            item.setToolTip(str(t.get("description") or ""))
            lst.addItem(item)
            row = QWidget()
            row_lay = QHBoxLayout(row)
            row_lay.setContentsMargins(4, 2, 4, 2)
            row_lay.setSpacing(6)
            cb = QCheckBox()
            cb.setChecked(enabled)
            cb.setToolTip(f"启用 / 禁用「{name}」（禁用后不下发给模型）")
            cb.toggled.connect(lambda on, n=name: self._toggle_tool(n, on))
            row_lay.addWidget(cb)
            text = QLabel(f"{name}   {risk}   {src}")
            text.setObjectName("HubToolName")
            if name in over:
                text.setToolTip("超出上限未下发（提高上限或在工具清单里调整顺序）")
                text.setObjectName("HubToolNameDim")
            row_lay.addWidget(text, 1)
            if name in over:
                tag = QLabel("超出上限")
                tag.setObjectName("HubToolDim")
                row_lay.addWidget(tag)
            lst.setItemWidget(item, row)
            self._tool_checks[name] = cb
            item.setSizeHint(QSize(0, 24))
        if not tools:
            lst.addItem(QListWidgetItem("（暂无工具）"))

    def _fill_mcp(self, servers: List[Dict[str, Any]]) -> None:
        """MCP 列表：`● 名称 · 传输 · N 工具`（属性名 `_mcp_list` 保持不变）。"""
        lst = getattr(self, "_mcp_list", None)
        if lst is None:
            return
        lbl = getattr(self, "_mcp_count_lbl", None)
        if lbl is not None:
            n_conn = len([s for s in servers
                          if isinstance(s, dict) and s.get("status") == "connected"])
            lbl.setText(f"已配置 {len(servers)} 台 · 已连接 {n_conn} 台")
        lst.clear()
        for s in servers:
            if not isinstance(s, dict):
                continue
            mark = {"connected": "●", "error": "×"}.get(str(s.get("status")), "○")
            item = QListWidgetItem(
                f"{mark} {s.get('name')}  · {s.get('transport')} · "
                f"{s.get('tool_count', 0)} 工具")
            item.setData(Qt.UserRole, s.get("id"))
            if s.get("error"):
                item.setToolTip(str(s["error"]))
            lst.addItem(item)
        if not servers:
            lst.addItem(QListWidgetItem(
                "（未配置 MCP 服务器：点「🔍 从在线清单安装 MCP…」或「手动添加」）"))

    def _fill_sessions(self) -> None:
        w = Worker(self._assist.sessions)
        w.result.connect(self._apply_sessions)
        w.start()

    def _apply_sessions(self, items) -> None:
        items = [s for s in (items or []) if isinstance(s, dict)]
        # 置顶优先、组内按最近活动：后端已排好，这里再排一次是防御
        # （旧版后端 / 本地缓存回来的顺序不一定可靠），刷新后顺序不会被抖乱。
        items.sort(key=lambda x: (not x.get("pinned"), -(x.get("updated_at") or 0)))
        self._session_rows = items        # 「清理空任务」靠这份快照

        # 左栏「任务 / 空间」两区整体重画。
        # 左栏只列有内容的任务：0 条消息的空任务（反复点「+」/ 来回切页攒出来的）
        # 只留在管理面板里，否则「任务」区那几个位置会被一串「新任务」占满，
        # 真正在聊的任务反而看不到。当前任务无论空不空都保留。
        # 注意用 get(..., 1)：字段缺失视为「有内容」，宁可多显示也不要把任务藏掉
        self._nav_rows = [s for s in items
                          if s.get("message_count", 1) or s.get("id") == self._session_id]
        self._nav_spaces = self._group_spaces(self._nav_rows)
        self._rebuild_nav_tasks()

        # 管理面板里的完整任务列表（面板未打开时为 None）
        lst = getattr(self, "_all_sess_list", None)
        if lst is not None:
            # 选中态优先跟随用户手动选的那条，其次当前任务；
            # 否则只要列表被刷新一下，用户刚选中的行就没了，点删除会像「没反应」
            keep = self._sel_sid or self._session_id
            lst.blockSignals(True)
            lst.clear()
            for s in items:
                ws = _norm_ws(s.get("workspace")).rsplit("/", 1)[-1] or "—"
                pin = "★ " if s.get("pinned") else ""
                item = QListWidgetItem(f"{pin}{s.get('title') or '新任务'}   "
                                       f"（{s.get('message_count', 0)} 条 · {ws}）")
                item.setData(Qt.UserRole, s.get("id"))
                item.setToolTip(self._task_tip(s))
                lst.addItem(item)
                if s.get("id") == keep:
                    item.setSelected(True)
                    lst.setCurrentItem(item)   # 必须在 addItem 之后，否则不生效
            if not items:
                lst.addItem("（还没有任务）")
            lst.blockSignals(False)

    # ------------------------------------------------------------------ #
    # 任务
    # ------------------------------------------------------------------ #
    def _new_session(self, silent: bool = False) -> None:
        # 当前任务本来就是空的（0 条消息）就直接复用，别再建一个：
        # 反复点左栏「+」或来回切页会攒出一长串「新任务（0 条）」
        cur = next((s for s in self._session_rows
                    if s.get("id") == self._session_id), None)
        if not silent and cur is not None and not cur.get("message_count"):
            self._clear_chat()
            self._greet()
            self._set_status("当前已经是新任务")
            return
        w = Worker(self._assist.new_session)
        w.result.connect(lambda s: self._on_session_created(s, silent))
        w.error.connect(lambda m: self._set_status(f"新建任务失败：{m}"))
        w.start()

    def _on_session_created(self, session: Dict[str, Any], silent: bool) -> None:
        if not isinstance(session, dict) or not session.get("id"):
            return
        self._session_id = session["id"]
        self._sel_sid = str(session["id"])      # 新任务立刻成为列表选中项
        self._clear_chat()
        self._greet()
        self._fill_sessions()
        self._fetch_queue(self._session_id)     # 新任务按 sid 同步队列快照

    def _greet(self) -> None:
        tools = len(self._state.get("tools") or [])
        skills = len(self._state.get("skills") or [])
        self._add_step(
            f"已就绪：{tools} 个工具 · {skills} 个技能 · "
            f"工作区 {self._state.get('workspace', '')}")

    def _load_session_item(self, item) -> None:
        if item is None:
            return
        sid = item.data(Qt.UserRole)
        if not sid:
            return
        w = Worker(self._assist.session, sid)
        w.result.connect(self._apply_session_detail)
        w.error.connect(lambda m: self._set_status(f"加载任务失败：{m}"))
        w.start()

    def _apply_session_detail(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self._session_id = data.get("id") or ""
        # 任务属于某个空间：切过去，否则文件工具还在上一个目录里干活
        self._sync_workspace_to_task(str(data.get("workspace") or ""))
        self._clear_chat()
        for m in data.get("messages") or []:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                bubble = ChatBubble(role, content)
                self._append_widget(bubble)
        self._add_step("已载入历史任务")
        self._sel_sid = str(self._session_id or "")
        self._fill_sessions()      # 同步左栏的选中态与「任务 / 空间」两区
        # 队列跟着任务走：先重置（旧任务的排队不是这个任务的），再按新 sid 拉快照
        self._fetch_queue(str(self._session_id or ""))

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if mods & Qt.ShiftModifier:
                    if self._busy:
                        self._on_guide_clicked()   # 忙碌：Shift+Enter 引导
                        return True
                    return False                   # 空闲：换行（现状）
                self._on_send_clicked()             # Enter：空闲发送 / 忙碌排队
                return True
        if obj is self._input and event.type() in (QEvent.Type.FocusIn,
                                                  QEvent.Type.FocusOut):
            # 输入框获得焦点时高亮整个输入容器（WorkBuddy 观感）
            on = event.type() == QEvent.Type.FocusIn
            box = getattr(self, "_composer", None)
            if box is not None and bool(box.property("focus")) != on:
                box.setProperty("focus", on)
                box.style().unpolish(box)
                box.style().polish(box)
        return super().eventFilter(obj, event)

    def _on_send_clicked(self) -> None:
        text = self._input.toPlainText().strip()
        if not text:
            return
        slots = self._available_send_slots()
        if slots <= 0:
            # 1.0.8：后端按队列模型串行处理（同一时刻只有 1 个活跃流），
            # 所以忙碌态 Enter 一律走「排队」，不再丢弃消息（1.0.9 行为保持）
            self._enqueue_message(text, guided=False)
            return
        if not self._session_id:
            w = Worker(self._assist.new_session)
            w.result.connect(lambda s: self._send_with_session(s, text))
            w.start()
            return
        self._send_with_session({"id": self._session_id}, text)

    def _available_send_slots(self) -> int:
        """当前还能并发起几条对话流：忙碌中 0，空闲 1。

        抽成方法便于测试断言「忙碌态 = 0 槽 → 一定入队」，也便于将来后端
        支持并行流时只改这一处。
        """
        return 0 if self._busy else 1

    def _on_guide_clicked(self) -> None:
        """⚡ / Shift+Enter：引导发送——立即登记进对话流，不中断当前轮。"""
        if not self._busy:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._enqueue_message(text, guided=True)

    def _enqueue_message(self, text: str, guided: bool) -> None:
        """REST 入队 / 引导发送（短请求，不新开 SSE 流）。

        失败时输入框文本保留（不清空即不丢），状态栏说明原因；
        成功后的面板刷新等 queue 快照（SSE 广播或主动 GET）。
        """
        sid = self._session_id
        if not sid:
            # 没有当前任务（忙碌必有流，正常走不到）：按普通发送兜底
            w = Worker(self._assist.new_session)
            w.result.connect(lambda s: self._send_with_session(s, text))
            w.start()
            return
        self._set_status("正在引导发出…" if guided else "正在排队…")
        w = Worker(self._assist.enqueue, sid, text, guided)
        w.result.connect(lambda _r: self._on_enqueued(guided))
        w.error.connect(lambda m: self._on_enqueue_failed(m, guided))
        w.start()

    def _on_enqueued(self, guided: bool) -> None:
        # 输入框清空；队列行等快照事件出现（单一事实来源，不局部加行）
        self._input.clear()
        self._set_status("已引导发出，本轮结束后自动处理" if guided
                         else "已排队，本轮结束后按序发送")

    def _on_enqueue_failed(self, msg: str, guided: bool) -> None:
        # 输入框没清过，文本自然保留；明确报错，不让用户以为发出去了
        self._set_status(
            f"{'引导' if guided else '排队'}失败：{msg}（消息保留在输入框）")

    def _send_with_session(self, session: Any, text: str) -> None:
        if not isinstance(session, dict) or not session.get("id"):
            self._set_status("任务创建失败，请检查后端服务")
            return
        self._session_id = session["id"]
        self._input.clear()
        self._add_bubble("user", text)
        self._scroll_bottom(force=True)   # 用户刚发消息：一定回到底部
        self._begin_streaming()

        # 1.0.8 A4：技能挂载改传 `mounted_skills` 字段，**不再拼进 message 正文**。
        # 老实现拼「用 xx 技能：<text>」，落库后历史正文被污染（用户复制/回看
        # 会看到前缀）；后端把挂载信息写进 system 段，历史干净、意图不丢。
        payload = {
            "session_id": self._session_id,
            "message": text,
            "provider": self._active_provider or "",
            "model": self._active_model or "",
            "stream": True,
        }
        if self._skill_slugs:
            payload["mounted_skills"] = list(self._skill_slugs)
        self._stream = AssistantStream(
            self.config.gateway_url(), self.config.api_key, payload)
        self._stream.event.connect(self._on_event)
        self._stream.failed.connect(self._on_stream_failed)
        self._stream.closed.connect(self._on_stream_closed)
        self._stream.start()

    def _set_busy_controls(self) -> None:
        """忙碌态发送区状态机（PRD 4.2 / 架构设计 3.6，P1-3）。

        空闲：↑ 发送（Enter）、⚡ 隐藏、placeholder「今天帮你做些什么？」
        忙碌：↑ 排队入队（Enter）、⚡ 引导发送（Shift+Enter）、■ 停止显示，
              placeholder 明示两种语义差异
        """
        busy = bool(self._busy)
        self._send_btn.setToolTip(
            "排队发送（Enter）：不打断当前生成，本轮结束后按序自动发送"
            if busy else "发送（Enter）")
        self._guide_btn.setVisible(busy)
        self._input.setPlaceholderText(
            "执行中：Enter 排队 · Shift+Enter 引导发出（不打断当前生成）"
            if busy else "今天帮你做些什么？（Enter 发送，Shift+Enter 换行）")

    def _begin_streaming(self) -> None:
        self._busy = True
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._set_busy_controls()
        self._start_spin()
        self._set_status("助理正在工作…")
        self._bubble = None
        self._thinking = None
        self._tool_cards = []

    def _end_streaming(self) -> None:
        self._busy = False
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)
        self._set_busy_controls()
        self._stop_spin()
        if self._thinking is not None:
            self._thinking.finish()
        if self._bubble is not None:
            self._bubble.end_stream()      # 收尾贴合宽度
        self._scroll_bottom(force=True)    # 答完了，回到底部看全文

    def _stop(self) -> None:
        """停止 = 本地断流 + 取消当前轮 + 队列暂停（1.0.9：不出队、不清空）。

        串行化：stop 成功回调里再拉一次队列快照，面板保留并显示「继续」。
        """
        if self._stream is not None:
            self._stream.cancel()
        sid = self._session_id
        self._add_step("已请求停止")
        # 界面先立刻脱离忙碌态（乐观），快照回来后补「继续」按钮
        self._end_streaming()
        self._set_status("已停止")
        if sid:
            def _after_stop(_res):
                self._fetch_queue(sid)
            w = Worker(lambda s=sid: self._assist.stop(s))
            w.result.connect(_after_stop)
            w.error.connect(lambda m: self._set_status(f"停止请求失败：{m}"))
            w.start()

    def _on_stream_failed(self, msg: str) -> None:
        self._add_step(f"连接失败：{msg}")
        self._set_status("请求失败，请确认后端已启动")

    def _on_stream_closed(self) -> None:
        self._stream = None
        if self._busy:
            self._end_streaming()
        self._fill_sessions()

    # ---------------- 事件分发 ----------------
    def _on_event(self, ev: Dict[str, Any]) -> None:  # noqa: C901
        kind = ev.get("type")
        if kind is None and ev.get("code"):
            # 走 SSE 的路径返回了 JSON 错误体（如 resume 撞活跃流 2017）：
            # 不当成事件静默吞掉，明确提示可重试
            self._add_step(f"请求被拒绝：{ev.get('message')}", error=True)
            self._set_status(f"{ev.get('message')}（可稍后重试）")
            return
        if kind == "session":
            self._session_id = ev.get("session_id") or self._session_id
        elif kind == "start":
            # 1.0.8 自动加载：start 事件新增 auto_loaded（架构 §7「start 事件扩展」）。
            # 空 dict = 未命中 profile / auto_load=false → 什么都不显示。
            self._on_auto_loaded(ev.get("auto_loaded") or {})
        elif kind == "model":
            self._set_status(f"模型：{ev.get('model')}")
        elif kind == "delta":
            self._ensure_bubble().append_text(ev.get("text") or "")
            self._scroll_bottom()
        elif kind == "reasoning":
            if self._thinking is None:
                # WorkBuddy 形态：灰色「深度思考」+ 正文平铺，不折叠
                self._thinking = ThinkingBlock()
                self._append_widget(self._thinking)
            self._thinking.append(ev.get("text") or "")
            self._scroll_bottom()
        elif kind == "replace":
            if self._bubble is not None:
                self._bubble.set_text(ev.get("text") or "", markdown=True)
        elif kind == "tool_call":
            self._finish_bubble()
            card = ToolCard(ev.get("name") or "", ev.get("summary") or "",
                            ev.get("risk") or "low", ev.get("args") or {},
                            index=ev.get("index"))
            self._tool_cards.append(card)
            self._append_widget(card)
            self._scroll_bottom()
        elif kind == "tool_result":
            # 优先按并行执行的调用序号（index）精确回填，避免同名/乱序串卡；
            # 旧事件不带 index 时回退到按名称匹配。
            idx = ev.get("index")
            target = None
            if idx is not None:
                for card in self._tool_cards:
                    if card._index == idx and card._result is None:
                        target = card
                        break
            if target is None:
                for card in reversed(self._tool_cards):
                    if card._name == ev.get("name") and card._result is None:
                        target = card
                        break
            if target is not None:
                target.set_result(bool(ev.get("ok")), ev.get("content") or "")
            self._scroll_bottom()
        elif kind == "approval_request":
            card = ApprovalCard(ev.get("id") or "", ev.get("tool") or "",
                                ev.get("reason") or "", ev.get("risk") or "medium",
                                ev.get("args") or {})
            card.resolved.connect(self._resolve_approval)
            self._approvals[ev.get("id") or ""] = card
            self._append_widget(card)
            self._scroll_bottom()
        elif kind == "approval_result":
            card = self._approvals.get(ev.get("id") or "")
            if card is not None:
                card.mark_closed("已放行" if ev.get("approve") else "已拒绝")
        elif kind == "skill_step":
            self._add_step(f"技能[{ev.get('skill')}] {ev.get('phase')}：{ev.get('detail')}")
        elif kind == "title":
            self._fill_sessions()
        elif kind == "note":
            self._add_step(str(ev.get("message") or ""))
        elif kind == "queued":
            # 入队确认（面板新行等 queue 快照，这里只做可视反馈）
            idx = int(ev.get("index") or 0)
            self._add_step(f"已排队（第 {idx + 1} 条）："
                           f"{_ellipsis(ev.get('text') or '', 24)}")
        elif kind == "guided":
            # 引导消息：立即渲染用户气泡（带 HH:MM）+ StepLine；不写会话、
            # 不打断当前轮；出队时才由 start_turn 落库
            self._add_guided_bubble(ev)
            self._add_step("已引导发出：本轮结束后自动处理")
        elif kind == "dequeued":
            # 出队：排队消息补用户气泡；引导消息的气泡在 guided 时已渲染，
            # 这里只加 StepLine，不重复
            if (ev.get("mode") or "queue") == "queue":
                self._add_bubble("user", ev.get("text") or "")
            self._add_step(f"正在处理排队消息："
                           f"{_ellipsis(ev.get('text') or '', 24)}")
        elif kind == "queue":
            # 队列全量快照：单一事实来源，面板以此重渲染
            self._apply_queue(ev.get("items") or [], bool(ev.get("paused")))
        elif kind == "queue_state":
            self._on_queue_state(bool(ev.get("paused")),
                                 str(ev.get("reason") or ""))
        elif kind == "done":
            # 单轮答复完成：收尾当前气泡，但**不结束忙碌态**——
            # 队列还有消息时 gen() 会在同一条连接里续跑（P0-2 前端关键点）
            self._finish_bubble(ev.get("content") or "")
            self._set_status("完成")
            self._scroll_bottom()
        elif kind == "error":
            self._add_step(f"出错了：{ev.get('message')}", error=True)
            self._set_status("出错，详见对话区")
        elif kind == "stopped":
            self._add_step("已停止生成")
        elif kind == "end":
            # 整条流唯一一次 end：队列耗尽或暂停时才下发
            self._end_streaming()
            if ev.get("paused"):
                self._set_status("已暂停：队列消息保留，点「继续」按序发送")
                if self._queue_resume_btn is not None:
                    self._queue_resume_btn.setVisible(
                        bool(self._queue_items))
            elif not ev.get("error"):
                self._set_status("就绪")
            self._approvals.clear()

    # ---------------- 自动加载（1.0.8，P0-6） ----------------
    def _on_auto_loaded(self, info: Dict[str, Any]) -> None:
        """`start.auto_loaded` → 插入一条可关闭的 StepLine（同任务同 profile 一次）。

        字段（架构接缝 S3，已定稿）：`profile` / `name` / `skills[]` / `tools`。
        不参与模型上下文（纯 UI）：只在对话流里插一个 widget，不发回后端。
        """
        if not isinstance(info, dict) or not info.get("profile"):
            return
        profile = str(info.get("profile") or "")
        if self._auto_off:
            # 本会话已点过「关闭自动加载」：后端可能还在同一轮里回旧值，
            # 客户端立即失效，不再提示（PRD §5.4.3 第 1 条）
            return
        key = (str(self._session_id or ""), profile)
        if key in self._auto_tips:
            return                    # 同一任务同一 profile 只提示一次
        self._auto_tips.add(key)
        skills = [str(s) for s in (info.get("skills") or []) if s]
        n_tools = int(info.get("tools") or 0)
        name = str(info.get("name") or profile)
        parts = [f"已自动加载：{name}"]
        if skills:
            parts.append("技能 " + " · ".join(skills))
        if n_tools:
            parts.append(f"工具 {n_tools} 个")
        # 「运行时会请求你确认」让用户在弹审批卡前有预期（架构 §7 体验缓解）
        text = " · ".join(parts) + "（运行时会请求你确认）"
        self._add_step_with_action(text, "关闭自动加载", self._disable_auto_load)

    def _auto_load_on(self) -> bool:
        """自动加载是否开启（本会话 _auto_off 优先，其次 GET /profiles 快照）。"""
        if self._auto_off:
            return False
        if "auto_load" in (self._profiles or {}):
            return bool(self._profiles.get("auto_load"))
        return True                   # 默认开（PRD §5.4.2 / config 默认 true）

    def _disable_auto_load(self) -> None:
        """点「关闭自动加载」→ PUT /profiles {auto_load:false} + 本会话立即失效。"""
        self._auto_off = True
        self._profiles = {**(self._profiles or {}), "auto_load": False}
        self._set_status("已关闭自动加载（可在 [技能] 菜单里重新打开）")
        w = Worker(self._assist.save_profiles, False, None)
        w.error.connect(lambda m: self._set_status(f"关闭自动加载失败：{m}"))
        w.start()

    def _set_auto_load(self, on: bool) -> None:
        """[技能] 菜单里的「自动加载：开 / 关」切换（PRD §5.4.3 第 3 条）。"""
        self._auto_off = not on
        self._profiles = {**(self._profiles or {}), "auto_load": bool(on)}
        self._set_status("已开启自动加载" if on
                         else "已关闭自动加载（可在设置里重新打开）")
        w = Worker(self._assist.save_profiles, bool(on), None)
        w.error.connect(lambda m: self._set_status(f"保存失败：{m}"))
        w.start()

    # ---------------- 引导会话队列（1.0.9） ----------------
    def _add_guided_bubble(self, ev: Dict[str, Any]) -> None:
        """引导消息气泡：用户侧 + HH:MM 时间标签（P1-2，只读可复制）。"""
        text = str(ev.get("text") or "")
        if not text:
            return
        bubble = ChatBubble("user", text, time_text=str(ev.get("time") or ""))
        self._append_widget(bubble)
        self._scroll_bottom()

    def _on_queue_state(self, paused: bool, reason: str) -> None:
        """暂停/恢复transition：切 StepLine + 「继续」按钮显隐。"""
        self._queue_paused = bool(paused)
        if self._queue_resume_btn is not None:
            self._queue_resume_btn.setVisible(
                bool(paused) and bool(self._queue_items))
        if paused:
            self._add_step("队列已暂停（消息保留，点「继续」按序发送）")
        else:
            self._add_step("已恢复，继续按序处理排队消息")

    def _apply_queue(self, items: List[Dict[str, Any]], paused: bool) -> None:
        """快照 -> 重渲染面板 + 显隐 + 「继续」按钮（不做局部增删）。"""
        self._queue_items = [dict(it) for it in (items or [])
                             if isinstance(it, dict)]
        self._queue_paused = bool(paused)
        self._rebuild_queue_rows()
        has = bool(self._queue_items)
        if self._queue_panel is not None:
            self._queue_panel.setVisible(has)
        if self._queue_head_lbl is not None:
            self._queue_head_lbl.setText(
                f"排队中（{len(self._queue_items)}）· 本轮结束后按序发送")
        if self._queue_resume_btn is not None:
            self._queue_resume_btn.setVisible(bool(self._queue_paused and has))
        if self._queue_clear_btn is not None:
            # 只剩引导消息时「全部清除」无可清（引导不可清）
            self._queue_clear_btn.setEnabled(
                any(not it.get("guided") for it in self._queue_items))

    def _rebuild_queue_rows(self) -> None:
        """按快照重建行（序号重排、首条禁 ↑、末条禁 ↓、引导行只读）。"""
        lay = self._queue_body_lay
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._queue_rows = {}
        n = len(self._queue_items)
        for i, it in enumerate(self._queue_items):
            row = QueueItemRow(it, i)
            row.saved.connect(self._queue_saved)
            row.removed.connect(self._queue_removed)
            row.moved.connect(self._queue_moved)
            row.edit_cancelled.connect(lambda: self._set_status("已取消编辑"))
            row.set_movable(i > 0, i < n - 1)
            lay.addWidget(row)
            self._queue_rows[str(it.get("id") or "")] = row

    def _queue_saved(self, item_id: str, text: str) -> None:
        """行内保存 -> PATCH /queue/{id}；已出队/引导的快照由错误提示收敛。"""
        sid = self._session_id
        if not sid:
            return
        self._set_status("正在保存排队消息…")
        w = Worker(self._assist.queue_update, sid, item_id, text)
        w.result.connect(lambda _r: self._after_queue_op("已保存"))
        w.error.connect(lambda m: self._set_status(f"保存失败：{m}"))
        w.start()

    def _queue_removed(self, item_id: str) -> None:
        sid = self._session_id
        if not sid:
            return
        self._set_status("正在删除排队消息…")
        w = Worker(self._assist.queue_delete, sid, item_id)
        w.result.connect(lambda _r: self._after_queue_op("已删除"))
        w.error.connect(lambda m: self._set_status(f"删除失败：{m}"))
        w.start()

    def _queue_moved(self, item_id: str, delta: int) -> None:
        sid = self._session_id
        if not sid:
            return
        w = Worker(self._assist.queue_move, sid, item_id, delta)
        w.result.connect(lambda _r: self._after_queue_op("已调整顺序"))
        w.error.connect(lambda m: self._set_status(f"排序失败：{m}"))
        w.start()

    def _queue_clear(self) -> None:
        """全部清除：只清排队中的普通消息（引导消息已登记对话流，不清）。"""
        sid = self._session_id
        if not sid:
            return
        w = Worker(self._assist.queue_clear, sid)
        w.result.connect(self._on_queue_cleared)
        w.error.connect(lambda m: self._set_status(f"清除失败：{m}"))
        w.start()

    def _on_queue_cleared(self, res: Any) -> None:
        n = res.get("cleared") if isinstance(res, dict) else 0
        self._set_status(f"已清除 {n or 0} 条排队消息"
                         "（引导消息已登记对话流，不可清除）")
        self._after_queue_op("")

    def _after_queue_op(self, note: str) -> None:
        """REST 成功后的刷新：无活跃流时主动拉快照；有活跃流等广播。"""
        if note:
            self._set_status(note)
        if not self._busy:
            self._fetch_queue(self._session_id)

    def _fetch_queue(self, sid: str) -> None:
        """GET /queue：停止后 / 切换任务后 / 无流时的快照同步手段。"""
        if not sid:
            return
        w = Worker(self._assist.queue, sid)
        w.result.connect(self._on_queue_fetched)
        w.error.connect(lambda m: self._set_status(f"队列同步失败：{m}"))
        w.start()

    def _on_queue_fetched(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self._apply_queue(data.get("items") or [], bool(data.get("paused")))

    def _resume_queue(self) -> None:
        """「继续」：POST /chat mode=resume -> 新 SSE 流回到自动消费路径。"""
        if self._busy or not self._session_id:
            return
        if not self._queue_items:
            self._set_status("队列是空的，没有要发送的消息")
            return
        self._begin_streaming()
        payload = {
            "session_id": self._session_id,
            "message": "",
            "mode": "resume",
            "provider": self._active_provider or "",
            "model": self._active_model or "",
            "stream": True,
        }
        self._stream = AssistantStream(
            self.config.gateway_url(), self.config.api_key, payload)
        self._stream.event.connect(self._on_event)
        self._stream.failed.connect(self._on_stream_failed)
        self._stream.closed.connect(self._on_stream_closed)
        self._stream.start()

    def _resolve_approval(self, aid: str, approve: bool, remember: bool) -> None:
        w = Worker(self._assist.resolve_approval, aid, approve, remember)
        w.error.connect(lambda m: self._set_status(f"审批提交失败：{m}"))
        w.start()

    # ---------------- 消息渲染 ----------------
    def _ensure_bubble(self) -> ChatBubble:
        if self._bubble is None:
            self._bubble = ChatBubble("assistant", "")
            # 流式开始就定死宽度：宽度不变 → 文字只往下长，不会反复折行闪屏
            self._bubble.begin_stream(self._stream_bubble_width())
            self._append_widget(self._bubble)
        return self._bubble

    def _stream_bubble_width(self) -> int:
        """流式气泡的稳定宽度（受内容列宽度约束，最小 260）。"""
        col_w = self._col.width() if self._col is not None else 0
        if col_w <= 0:
            return ChatBubble.MAX_WIDTH
        return max(260, min(ChatBubble.MAX_WIDTH, col_w - 28))

    def _finish_bubble(self, final: str = "") -> None:
        # 注意：工具调用后 _bubble 已被定稿置空，此时 done 事件可能才带最终答复
        # （后端兜底 / 非流式拼接），不能直接丢掉。
        if self._bubble is None:
            if not (final or "").strip():
                return
            self._bubble = ChatBubble("assistant", "")
            self._append_widget(self._bubble)
        text = final or self._bubble.label.text()
        self._bubble.set_text(text, markdown=True)
        self._bubble.end_stream()      # 收尾：贴合内容宽度
        self._bubble = None

    def _add_bubble(self, role: str, text: str) -> ChatBubble:
        b = ChatBubble(role, text)
        self._append_widget(b)
        self._scroll_bottom()
        return b

    def _add_step(self, text: str, error: bool = False) -> None:
        self._append_widget(StepLine(text, error=error))
        self._scroll_bottom()

    def _add_step_with_action(self, text: str, action_label: str, on_action,
                              error: bool = False) -> AutoLoadPrompt:
        """带一个动作按钮的 StepLine 行（1.0.8 自动加载提示用）。

        **不改造 `StepLine`**（架构 A6）：它只有一个居中 QLabel，页面侧
        `_add_step()` 有 5 处调用，加按钮位会破坏居中并波及那 5 处签名。
        这里用 `AutoLoadPrompt` 组合「原 `StepLine` + `#MiniBtn` 按钮」。
        """
        row = AutoLoadPrompt(text, action_label, error=error)
        row.btn.clicked.connect(lambda _c=False: on_action())
        self._append_widget(row)
        self._scroll_bottom()
        return row

    def _append_widget(self, w: QWidget) -> None:
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, w)

    def _clear_chat(self) -> None:
        while self._msg_layout.count() > 1:
            item = self._msg_layout.takeAt(0)
            wid = item.widget()
            if wid is not None:
                wid.deleteLater()
        self._bubble = None
        self._thinking = None
        self._tool_cards = []
        self._approvals.clear()
        self._follow = True
        # 自动加载提示的去重表跟着对话清空（换任务重新提示一次）
        self._auto_tips.clear()
        # 队列面板跟随任务重置（新任务的快照由 _fetch_queue 拉回）
        self._apply_queue([], False)

    def _scroll_bottom(self, force: bool = False) -> None:
        """滚到底部（合并请求，避免流式输出时画面狂跳）。

        - 40ms 内多次调用只滚一次：每个 token 都 setValue 会让整屏闪动
        - 用户自己往上翻看过（_follow 为 False）时不再硬拽回底部
        """
        if not force and not self._follow:
            return
        if not self._scroll_timer.isActive():
            self._scroll_timer.start(self.SCROLL_MS)

    def _do_scroll_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._follow = True

    def _on_scroll_value(self, value: int) -> None:
        """记录用户是否还贴着底部：往上翻就别再自动拽回去。"""
        bar = self._scroll.verticalScrollBar()
        self._follow = value >= bar.maximum() - 8

    # ------------------------------------------------------------------ #
    # 底栏：模型 / 审批策略 / 工作区 / 技能
    # ------------------------------------------------------------------ #
    def _build_model_menu(self) -> QMenu:
        """构建模型下拉（WorkBuddy 的「模型 ⌄」位）：来源 + 模型 + 配置入口。

        拆出构建段便于无头测试（_show_model_menu 只负责弹出，exec 会阻塞）。
        """
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")  # 复用对话页的菜单样式

        src = menu.addMenu("模型来源")
        for p in (self._state.get("providers") or []):
            if not p.get("enabled", True):
                continue
            act = src.addAction(p.get("label") or p.get("id"))
            act.setCheckable(True)
            act.setChecked(p.get("id") == self._active_provider)
            act.triggered.connect(
                lambda _c=False, pid=p.get("id"): self._set_provider(pid))

        menu.addSeparator()

        names = self._model_cache.get(self._active_provider) or []
        if names:
            for n in names[:20]:
                act = menu.addAction(n)
                act.setCheckable(True)
                act.setChecked(n == self._active_model)
                act.triggered.connect(lambda _c=False, m=n: self._set_model(m))
            if len(names) > 20:
                # 超出部分收进可点开的子菜单（之前是禁用的一行字，用户找不到入口）
                more = menu.addMenu(f"还有 {len(names) - 20} 个模型 ▸")
                for n in names[20:]:
                    act = more.addAction(n)
                    act.setCheckable(True)
                    act.setChecked(n == self._active_model)
                    act.triggered.connect(lambda _c=False, m=n: self._set_model(m))
        else:
            act = menu.addAction("（暂无可用模型，点下面刷新）")
            act.setEnabled(False)

        menu.addSeparator()
        act = menu.addAction("刷新模型列表")
        act.triggered.connect(lambda: self._fill_models(self._active_provider))
        if self._active_provider != "local":
            act = menu.addAction("配置密钥与端点…")
            act.triggered.connect(self._edit_provider)
        custom = next((p for p in (self._state.get("providers") or [])
                       if p.get("id") == "custom"), {})
        label = ("自定义模型配置…" if custom.get("enabled")
                 else "添加自定义模型…")
        act = menu.addAction(label)
        act.triggered.connect(self._configure_custom_provider)
        return menu

    def _show_model_menu(self) -> None:
        self._popup_above(self._build_model_menu(), self._model_btn)

    def _configure_custom_provider(self) -> None:
        """添加/编辑「自定义（OpenAI 兼容）」提供商：填端点 + Key + 模型即可用。

        一次保存同时写 providers 与 active_provider，避免两个异步 Worker
        竞态导致「启用了但没切过去」。要求必须填模型名（非本地提供商
        resolve_model 需要显式模型）。
        """
        providers = [dict(p) for p in (self._state.get("providers") or [])]
        custom = next((p for p in providers if p.get("id") == "custom"), None)
        if custom is None:      # 老配置没有内置 custom 条目，现场补一个
            custom = {"id": "custom", "label": "自定义（OpenAI 兼容）",
                      "kind": "openai", "enabled": False, "base_url": "",
                      "api_key": "", "model": ""}
            providers.append(custom)
        dlg = ProviderDialog(self, custom)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        patch = dlg.payload() or {}
        base = str(patch.get("base_url") or "").strip()
        model = str(patch.get("model") or custom.get("model") or "").strip()
        if not base:
            self._set_status("Base URL 不能为空，自定义模型未保存")
            return
        if not model:
            self._set_status("请填写默认模型名（如 Qwen/Qwen3-8B），自定义模型未保存")
            return
        custom.update(patch)
        custom["enabled"] = True
        # 本地状态先更新，界面立即切过去；后端保存异步跟进
        self._state["providers"] = providers
        self._active_provider = "custom"
        self._active_model = model
        self._refresh_model_btn()
        self._fill_models("custom")
        w = Worker(self._assist.save_settings,
                   {"providers": providers, "active_provider": "custom"})
        w.error.connect(lambda m: self._set_status(f"保存失败：{m}"))
        w.start()
        self._set_status(f"自定义模型已启用：{base}")

    def _set_provider(self, pid: str) -> None:
        if not pid or pid == self._active_provider:
            return
        self._active_provider = pid
        provider = next((p for p in (self._state.get("providers") or [])
                         if p.get("id") == pid), {})
        self._active_model = str(provider.get("model") or "")
        self._refresh_model_btn()
        self._fill_models(pid)
        w = Worker(self._assist.save_settings, {"active_provider": pid})
        w.error.connect(lambda m: self._set_status(f"保存失败：{m}"))
        w.start()
        self._set_status(f"模型来源已切换到：{provider.get('label') or pid}")

    def _set_model(self, name: str) -> None:
        name = (name or "").strip()
        if not name or name == self._active_model:
            return
        self._active_model = name
        self._refresh_model_btn()
        self._save_provider_patch(self._active_provider, {"model": name})
        self._set_status(f"模型：{name}")

    def _save_provider_patch(self, pid: str, patch: Dict[str, Any]) -> None:
        """更新某个提供商的字段（key / base_url / model）并回写整份 providers。"""
        providers = [dict(p) for p in (self._state.get("providers") or [])]
        target = next((p for p in providers if p.get("id") == pid), None)
        if target is None:
            return
        target.update(patch)
        for p in providers:
            if p.get("id") == "local":
                # 本地模型名可能由用户显式指定，不再强制清空
                continue
            if not p.get("model"):
                p["model"] = "Qwen/Qwen3-8B" if p["id"] == "siliconflow" else ""
        w = Worker(self._assist.save_settings, {"providers": providers})
        w.result.connect(lambda _r: self.refresh())
        w.error.connect(lambda m: self._set_status(f"保存失败：{m}"))
        w.start()

    def _edit_provider(self) -> None:
        pid = self._active_provider
        if not pid or pid == "local":
            return
        provider = next((p for p in (self._state.get("providers") or [])
                         if p.get("id") == pid), {})
        dlg = ProviderDialog(self, provider)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        patch = dlg.payload()
        if patch:
            self._save_provider_patch(pid, patch)

    def _show_policy_menu(self) -> None:
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")  # 复用对话页的菜单样式
        menu.setToolTipsVisible(True)
        cur = str(self._state.get("policy") or "readonly_auto")
        for pid, label, tip in POLICY_ITEMS:
            act = menu.addAction(f"{POLICY_ICON.get(pid, '')} {label}".strip())
            act.setCheckable(True)
            act.setChecked(pid == cur)
            act.setToolTip(tip)
            act.triggered.connect(lambda _c=False, p=pid: self._set_policy(p))
        self._popup_above(menu, self._policy_btn)

    def _set_policy(self, pid: str) -> None:
        if not pid:
            return
        self._state["policy"] = pid
        self._refresh_policy_btn()
        w = Worker(self._assist.save_settings, {"policy": pid})
        w.error.connect(lambda m: self._set_status(f"保存失败：{m}"))
        w.start()
        label = next((lb for p, lb, _t in POLICY_ITEMS if p == pid), pid)
        self._set_status(f"审批策略已更新：{label}")

    def _choose_workspace(self) -> None:
        """切换工作区（文件工具的沙箱根）。

        两处容易翻车的点（用户反馈「选工作区闪退 / 重开不接上次工作区」）：
          1) 原生目录选择框由系统 shell 弹，冻结包 / 远程桌面 / 某些安全软件下
             会把主窗口一起带走；统一强制 Qt 自绘对话框（DontUseNativeDialog），
             行为可控、深浅色一致，出问题也只是本对话框不显示，不牵连主窗口；
          2) 选中后**先写本机 config.json 再发后端请求**。后端 PUT 可能因为
             「后端未启动 / 目录不可创建」失败，甚至进程在同步途中退出——本地
             先落盘，下次打开一定能接上上次的选择。
        """
        start = (self._state.get("workspace")
                 or str(getattr(self.config, "assistant_workspace", "") or "")
                 or os.path.expanduser("~"))
        try:
            dlg = QFileDialog(self._dialog_parent(), "选择工作区", start)
            dlg.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dlg.setOption(QFileDialog.Option.ShowDirsOnly, True)
            dlg.setFileMode(QFileDialog.FileMode.Directory)
            # 自绘框的标签由 Qt 翻译文件给，缺项会留英文（"Look in:"），
            # 这里直接写死中文，跟界面其余部分保持一致
            dlg.setLabelText(QFileDialog.DialogLabel.LookIn, "位置：")
            dlg.setLabelText(QFileDialog.DialogLabel.FileName, "目录：")
            dlg.setLabelText(QFileDialog.DialogLabel.FileType, "类型：")
            dlg.setLabelText(QFileDialog.DialogLabel.Accept, "选择此文件夹")
            dlg.setLabelText(QFileDialog.DialogLabel.Reject, "取消")
            ok = dlg.exec() == QDialog.DialogCode.Accepted
            picked = dlg.selectedFiles() if ok else []
            path = str(picked[0]) if picked else ""
        except Exception as e:  # noqa: BLE001
            self._set_status(f"打开目录选择框失败：{e}")
            return
        path = str(path or "").strip()
        if not path:
            return
        if not os.path.isdir(path):
            QMessageBox.warning(self._dialog_parent(), "工作区不可用",
                                f"这个路径不是一个可用目录：\n{path}")
            return
        self._state["workspace"] = path
        self._refresh_ws_btn(path)
        self._remember_workspace(path)          # 先落盘（本地），后端同步在后
        self._set_status(f"工作区已切换：{path}")
        w = Worker(self._assist.save_settings, {"workspace": path})
        w.result.connect(lambda _r: self.refresh())
        w.error.connect(lambda m, p=path: self._on_workspace_save_failed(m, p))
        w.start()

    def _remember_workspace(self, path: str) -> None:
        """把工作区写进本机 config.json（网络请求失败也保住这次选择）。"""
        try:
            self.config.assistant_workspace = path
            save = getattr(self.config, "save", None)
            if callable(save):
                save()
        except Exception:  # noqa: BLE001
            pass

    def _on_workspace_save_failed(self, msg: str, path: str) -> None:
        """后端没接受工作区：本机记住的保留，界面回滚并说明原因。"""
        self._set_status(f"工作区已记在本机，但后端未接受：{msg}")
        QMessageBox.warning(
            self._dialog_parent(), "工作区未生效",
            f"已记住本机选择：\n{path}\n\n后端返回：{msg}\n\n"
            "文件工具这次仍会用旧工作区。请确认服务已启动后再选一次。")
        # 不在这里重试：_ws_synced 留给下一次 refresh 时补同步

    def _sync_workspace_once(self, path: str) -> None:
        """把本机记住的工作区补同步给后端（每个页面实例只尝试一次）。"""
        if self._ws_synced or not path:
            return
        self._ws_synced = True
        self._state["workspace"] = path
        self._refresh_ws_btn(path)
        w = Worker(self._assist.save_settings, {"workspace": path})
        w.result.connect(lambda _r: self.refresh())
        w.start()

    MAX_SKILLS = 5             # 单任务同时挂载上限（对标 WorkBuddy 多技能）

    def _build_skill_menu(self) -> QMenu:
        """构建技能菜单（PRD §5.1.2）。

            自动加载：开 / 关                     ← PUT /profiles
            挂载技能 N/5                          ← 原「最多同时挂载 5 个」
            ────
            ☑ workspace-organizer   内置          ← hover: description + 工具数
            ────
            🔍 更多技能（清单中心）…
            ＋ 从在线清单安装…
            ＋ 从本地文件夹导入…
            ＋ 粘贴 URL 安装…
            ⟳ 刷新在线目录
            ────
            清除全部（仅挂载非空时）
            ────
            工具 / MCP                            ← 1.0.7 起的「四个入口」不丢

        鼠标悬停技能项显示详细介绍（SKILL.md 的 description + 工具数）——
        QMenu 必须开 setToolTipsVisible(True)，否则 setToolTip 不显示。
        """
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")  # 复用对话页的菜单样式
        menu.setToolTipsVisible(True)

        # ---- 自动加载开关（PRD §5.4.3 第 3 条）----
        on = self._auto_load_on()
        auto = menu.addAction(f"自动加载：{'开' if on else '关'}")
        auto.setToolTip("按需求自动挂载常用技能并收紧工具白名单。\n"
                        "关闭后本轮与后续都不再自动加载（可在设置里重新打开）")
        auto.triggered.connect(lambda _c=False: self._set_auto_load(not on))
        menu.addSeparator()

        note = menu.addAction(f"挂载技能 {len(self._skill_slugs)}/{self.MAX_SKILLS}")
        note.setEnabled(False)

        skills = self._state.get("skills") or []
        full = len(self._skill_slugs) >= self.MAX_SKILLS
        if skills:
            menu.addSeparator()
            for s in skills:
                slug = s.get("slug") or ""
                name = s.get("name") or slug
                mounted = slug in self._skill_slugs
                badge = self._skill_badge(s)
                act = menu.addAction(f"{name}{badge}" if badge else name)
                tip = str(s.get("description") or "").strip()
                ntools = len(s.get("tools") or [])
                if ntools:
                    tip = f"{tip}\n包含 {ntools} 个工具" if tip \
                        else f"包含 {ntools} 个工具"
                if mounted:
                    tip = f"{tip}\n已挂载" if tip else "已挂载"
                elif full:
                    tip = f"{tip}\n已挂载 {self.MAX_SKILLS} 个，先移除一个" if tip \
                        else f"已挂载 {self.MAX_SKILLS} 个，先移除一个"
                act.setToolTip(tip)
                act.setCheckable(True)
                act.setChecked(mounted)
                if mounted:
                    act.triggered.connect(
                        lambda _c=False, sl=slug: self._remove_skill(sl))
                else:
                    if full:
                        act.setEnabled(False)
                    act.triggered.connect(
                        lambda _c=False, sl=slug: self._add_skill(sl))

        # ---- 添加入口：**空态也必须是这三条可点项**（PRD §5.1.2 核心）----
        menu.addSeparator()
        hub = menu.addAction("🔍 更多技能（清单中心）…")
        hub.setToolTip("打开清单中心技能页（左列表 + 右详情 + 在线目录）")
        hub.triggered.connect(lambda _c=False: self.open_manager("skills"))
        online = menu.addAction("＋ 从在线清单安装…")
        online.setToolTip("打开清单中心并聚焦技能搜索框")
        online.triggered.connect(
            lambda _c=False: self.open_manager("skills", focus=True))
        local = menu.addAction("＋ 从本地文件夹导入…")
        local.setToolTip("选中含 SKILL.md 的文件夹，复制进技能目录")
        local.triggered.connect(lambda _c=False: self._import_skill_from_dir())
        url = menu.addAction("＋ 粘贴 URL 安装…")
        url.setToolTip("GitHub / Gitee 的 SKILL.md 直链（含 /blob/ 的网页链接会自动转 raw）")
        url.triggered.connect(lambda _c=False: self._install_skill_from_url())
        refresh = menu.addAction("⟳ 刷新在线目录")
        refresh.setToolTip("三源并行刷新在线目录缓存")
        refresh.triggered.connect(lambda _c=False: self._refresh_catalog())

        if self._skill_slugs:
            menu.addSeparator()
            clear = menu.addAction("清除全部")
            clear.triggered.connect(self._clear_skills)

        menu.addSeparator()
        for label, key in (("工具", "tools"), ("MCP", "mcp")):
            act = menu.addAction(label)
            act.triggered.connect(lambda _c=False, k=key: self.open_manager(k))
        return menu

    def _skill_badge(self, s: Dict[str, Any]) -> str:
        """技能的来源徽标：内置 `[内置]` / 在线已装 `[在线]`（PRD §5.2 前置标识）。"""
        slug = str(s.get("slug") or "")
        if s.get("builtin"):
            return "  [内置]"
        src = (self._state.get("skill_sources") or {}).get(slug) or {}
        if src and src.get("kind"):
            return "  [在线]"
        return ""

    def _show_skill_menu(self) -> None:
        self._popup_above(self._build_skill_menu(), self._skill_btn)

    def _add_skill(self, slug: str) -> None:
        """挂载技能（最多 5 个）：之后每条消息自动带上「用 xx 技能」，输入框保持干净。"""
        if not slug or slug in self._skill_slugs:
            return
        if len(self._skill_slugs) >= self.MAX_SKILLS:
            self._set_status(
                f"最多同时挂载 {self.MAX_SKILLS} 个技能（先移除一个再挂）")
            return
        self._skill_slugs.append(slug)
        self._refresh_skill_chips()
        self._set_status(
            f"已挂载技能：{slug}（当前 {len(self._skill_slugs)}/{self.MAX_SKILLS}）")

    def _remove_skill(self, slug: str) -> None:
        if slug in self._skill_slugs:
            self._skill_slugs.remove(slug)
            self._refresh_skill_chips()
            self._set_status(f"已移除技能：{slug}")

    def _clear_skills(self) -> None:
        if not self._skill_slugs:
            return
        n = len(self._skill_slugs)
        self._skill_slugs = []
        self._refresh_skill_chips()
        self._set_status(f"已清除 {n} 个挂载技能")

    def _refresh_skill_chips(self) -> None:
        """重绘挂载区：每个技能一个可移除 chip；占位按钮保持「技能」文案。"""
        slugs = list(self._skill_slugs or [])
        lay = getattr(self, "_skill_chip_lay", None)
        if lay is not None:
            while lay.count():
                item = lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
            for slug in slugs:
                chip = QPushButton(f"✕ {slug}")
                chip.setObjectName("BarChip")
                chip.setProperty("active", True)
                chip.setCursor(Qt.PointingHandCursor)
                chip.setToolTip(f"已挂载技能：{slug}\n点击移除")
                chip.clicked.connect(
                    lambda _c=False, s=slug: self._remove_skill(s))
                lay.addWidget(chip)
            cont = getattr(self, "_skill_chip_cont", None)
            if cont is not None:
                cont.setVisible(bool(slugs))
        # ④ 宽度压缩：chip 建好后再判断（要量真实 sizeHint）。
        #    ≥3 个必然折行；1~2 个若内联会顶爆 504px 也提前折到输入框上方。
        self._reparent_skill_chips(len(slugs))
        # 占位按钮：始终保持「技能」文案，挂载数变化时切换高亮与 tooltip
        self._skill_btn.setText("技能")
        has = bool(slugs)
        tip = (f"已挂载 {len(slugs)} 个技能：" + "、".join(slugs) + "\n点击增删"
               if slugs else "挂载技能 / 添加技能（本地文件夹 · zip · URL · 在线清单）")
        self._skill_btn.setToolTip(tip)
        if bool(self._skill_btn.property("active")) != has:
            self._skill_btn.setProperty("active", has)
            self._skill_btn.style().unpolish(self._skill_btn)
            self._skill_btn.style().polish(self._skill_btn)

    # ------------------------------------------------------------------ #
    # 底栏 [工具] / [MCP] 入口（1.0.8，PRD §5.1.3 / §5.1.4）
    # ------------------------------------------------------------------ #
    TOOL_PRIORITY = ("fs_list", "fs_read", "fs_glob", "fs_grep", "fs_info",
                     "skill_list", "web_fetch", "skill_run", "fs_write",
                     "fs_edit", "fs_mkdir", "fs_move", "fs_delete", "shell_run")

    def _tool_prefs_snapshot(self) -> Dict[str, Any]:
        """当前工具下发快照：{disabled[], max_tools, total, active, over[]}。

        优先用后端 GET /tools/prefs（字段最全）；没拉到时从 `/state.tools`
        的 `enabled` 字段本地推算，保证首屏菜单不空（架构 §7「首屏先渲染」）。
        """
        prefs = dict(self._tool_prefs or {})
        tools = [t for t in (self._state.get("tools") or []) if isinstance(t, dict)]
        max_tools = int(prefs.get("max_tools") or 24)
        disabled = prefs.get("disabled")
        if disabled is None:
            # 后端未回 tool_prefs：用 /state 的 enabled 反推（后端 to_dict 增了
            # enabled 字段，架构 §3.2 tools.py list() 一节）
            disabled = [str(t.get("name")) for t in tools if not t.get("enabled", True)]
        total = int(prefs.get("total") or len(tools))
        active = prefs.get("active")
        if active is None:
            active = max(0, min(len([t for t in tools
                                     if str(t.get("name")) not in disabled]),
                                max_tools))
        over = prefs.get("over")
        if over is None:
            over = self._tools_over_limit(tools, disabled, max_tools)
        return {"disabled": list(disabled), "max_tools": max_tools,
                "total": total, "active": int(active), "over": list(over)}

    def _tools_over_limit(self, tools: List[Dict[str, Any]],
                          disabled: List[str], max_tools: int) -> List[str]:
        """按架构 §6.2 优先级排序后，被 max_tools 裁掉（超出上限未下发）的工具名。"""
        enabled = [t for t in tools if str(t.get("name")) not in disabled]

        def rank(t: Dict[str, Any]):
            name = str(t.get("name") or "")
            src = str(t.get("source") or "builtin")
            try:
                return (0 if src == "builtin" else 1,
                        self.TOOL_PRIORITY.index(name)
                        if name in self.TOOL_PRIORITY else len(self.TOOL_PRIORITY),
                        name)
            except ValueError:      # pragma: no cover - index 已带 in 判断
                return (1, len(self.TOOL_PRIORITY), name)

        enabled.sort(key=rank)
        return [str(t.get("name")) for t in enabled[max(0, max_tools):]]

    def _tool_group_of(self, name: str) -> str:
        """工具的归属组：`mcp__<id>__<tool>` → MCP 服务器名，否则 `builtin`。"""
        if name.startswith("mcp__"):
            parts = name.split("__")
            return parts[1] if len(parts) > 1 else "mcp"
        return "builtin"

    def _build_tool_menu(self) -> QMenu:
        """[工具] 菜单（PRD §5.1.3）。

            当前下发 18 / 上限 24                 （禁用行，实时算）
            ────
            ☑ fs_list   只读   内置
            ☐ shell_run 高危   内置             ← 关闭需二次确认
            ☑ 文件系统 · 12 个工具             ← MCP 按 server 折叠，整组开关
            ────
            工具清单与开关…                    → open_manager("tools")
            ⟳ 重连已启用 MCP                   → 逐台 connect
        """
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")
        menu.setToolTipsVisible(True)
        snap = self._tool_prefs_snapshot()
        head = menu.addAction(
            f"当前下发 {snap['active']} / 上限 {snap['max_tools']}")
        head.setEnabled(False)
        if snap["over"]:
            head.setToolTip("超出上限未下发：" + "、".join(snap["over"]))
        menu.addSeparator()

        disabled = set(snap["disabled"])
        tools = [t for t in (self._state.get("tools") or []) if isinstance(t, dict)]
        builtin = [t for t in tools if self._tool_group_of(str(t.get("name"))) == "builtin"]
        mcp = [t for t in tools if self._tool_group_of(str(t.get("name"))) != "builtin"]

        for t in builtin:
            name = str(t.get("name") or "")
            risk = RISK_TEXT.get(t.get("risk"), t.get("risk") or "")
            act = menu.addAction(f"{'☑' if name not in disabled else '☐'} "
                                 f"{name}   {risk}   内置")
            act.setCheckable(True)
            act.setChecked(name not in disabled)
            act.setToolTip(str(t.get("description") or ""))
            if name in snap["over"]:
                act.setToolTip(
                    (act.toolTip() + "\n" if act.toolTip() else "")
                    + "超出上限未下发（提高上限或在工具清单里调整）")
            act.triggered.connect(
                lambda checked, n=name: self._toggle_tool(n, checked))

        if mcp:
            menu.addSeparator()
            groups: Dict[str, List[Dict[str, Any]]] = {}
            for t in mcp:
                groups.setdefault(self._tool_group_of(str(t.get("name"))), []).append(t)
            for gid, items in groups.items():
                names = [str(i.get("name") or "") for i in items]
                on = [n for n in names if n not in disabled]
                grp = menu.addMenu(
                    f"{'☑' if len(on) == len(names) else ('☐' if not on else '◪')} "
                    f"{gid} · {len(names)} 个工具")
                grp.setToolTipsVisible(True)
                all_act = grp.addAction("整组开启" if len(on) < len(names) else "整组关闭")
                all_act.triggered.connect(
                    lambda _c=False, ns=list(names),
                    turn_on=len(on) < len(names): self._toggle_tool_group(ns, turn_on))
                grp.addSeparator()
                for n in names:
                    act = grp.addAction(f"{'☑' if n not in disabled else '☐'} {n}")
                    act.setCheckable(True)
                    act.setChecked(n not in disabled)
                    act.triggered.connect(
                        lambda checked, nm=n: self._toggle_tool(nm, checked))

        if not tools:
            act = menu.addAction("（暂无工具，先在 [MCP] 里加服务器）")
            act.setEnabled(False)

        menu.addSeparator()
        act = menu.addAction("工具清单与开关…")
        act.setToolTip("打开清单中心的工具页：逐条开关 + 下发计数")
        act.triggered.connect(lambda _c=False: self.open_manager("tools"))
        act = menu.addAction("⟳ 重连已启用 MCP")
        act.setToolTip("逐台连接已启用的 MCP 服务器，失败逐条报错不中断")
        act.triggered.connect(lambda _c=False: self._reconnect_mcp())
        return menu

    def _show_tool_menu(self) -> None:
        self._popup_above(self._build_tool_menu(), self._tool_btn)

    # 高危工具：关闭时必须二次确认（PRD §5.1.3 / P0-5）
    HIGH_RISK_TOOLS = ("shell_run", "fs_delete")
    # 关掉后技能就调不动了：也要二次确认（P0-5 验收第 4 条）
    SKILL_TOOLS = ("skill_run", "skill_list")

    def _tool_needs_confirm(self, name: str) -> bool:
        if name in self.HIGH_RISK_TOOLS:
            return True
        if name in self.SKILL_TOOLS:
            return True
        risk = next((t.get("risk") for t in (self._state.get("tools") or [])
                     if isinstance(t, dict) and t.get("name") == name), "")
        return risk == "high"

    def _toggle_tool(self, name: str, enabled: bool) -> None:
        """开关单个工具：禁用高危 / 技能类工具要二次确认，然后 PUT /tools/prefs。"""
        if not name:
            return
        if not enabled and self._tool_needs_confirm(name):
            if name in self.SKILL_TOOLS:
                warn = (f"关掉「{name}」后技能无法被调用（技能正文只能作为提示词，"
                        "无法执行技能里的步骤）。确定关闭？")
            else:
                warn = f"「{name}」是高危工具（可修改/删除文件或执行命令）。确定关闭？"
            ans = QMessageBox.question(
                self._dialog_parent(), "关闭工具", warn,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ans != QMessageBox.StandardButton.Yes:
                return
        snap = self._tool_prefs_snapshot()
        disabled = [n for n in snap["disabled"] if n != name]
        if not enabled:
            disabled.append(name)
        self._save_tool_prefs(disabled, snap["max_tools"])

    def _toggle_tool_group(self, names: List[str], enabled: bool) -> None:
        """整组开关（MCP 服务器一组）——不逐个弹确认，整组一次提交。"""
        if not names:
            return
        snap = self._tool_prefs_snapshot()
        keep = [n for n in snap["disabled"] if n not in names]
        disabled = keep if enabled else keep + [n for n in names if n not in keep]
        self._save_tool_prefs(disabled, snap["max_tools"])

    def _save_tool_prefs(self, disabled: List[str],
                         max_tools: Optional[int] = None) -> None:
        """PUT /tools/prefs：先本地乐观更新（菜单立刻反映），后端异步跟进。"""
        mt = int(max_tools or self._tool_prefs_snapshot()["max_tools"])
        dropped = set(disabled)
        self._tool_prefs = {**(self._tool_prefs or {}),
                            "disabled": list(disabled), "max_tools": mt}
        # /state 的 tools 也要跟着变，否则下次 _fill_tools 会把开关弹回旧值
        for t in (self._state.get("tools") or []):
            if isinstance(t, dict):
                t["enabled"] = str(t.get("name")) not in dropped
        w = Worker(self._assist.save_tool_prefs, list(disabled), mt)
        w.result.connect(lambda _r: self._set_status(
            f"工具开关已保存（当前下发 {self._tool_prefs_snapshot()['active']} 个）"))
        w.error.connect(lambda m: self._set_status(f"保存工具开关失败：{m}"))
        w.start()
        if self._tool_list is not None:
            self._fill_tools(self._state.get("tools") or [])

    def _build_mcp_menu(self) -> QMenu:
        """[MCP] 菜单（PRD §5.1.4）。

            已配置 2 台 · 已连接 1 台             （禁用行）
            ────
            ● 文件系统  本地 · 12 工具  [断开]
            ○ github    远程 · 未连接   [连接]
            ────
            🔍 从在线清单安装 MCP…
            ＋ 手动添加（stdio / http）…
            ＋ 粘贴 JSON 添加…
            ────
            管理 MCP 服务器…
        """
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")
        menu.setToolTipsVisible(True)
        servers = [s for s in (self._state.get("mcp") or []) if isinstance(s, dict)]
        n_conn = len([s for s in servers if s.get("status") == "connected"])
        head = menu.addAction(f"已配置 {len(servers)} 台 · 已连接 {n_conn} 台")
        head.setEnabled(False)
        menu.addSeparator()

        for s in servers:
            sid = str(s.get("id") or "")
            mark = {"connected": "●", "error": "×"}.get(str(s.get("status")), "○")
            where = "远程" if s.get("transport") == "http" else "本地"
            state = ("已连接" if s.get("status") == "connected"
                     else ("连接失败" if s.get("status") == "error" else "未连接"))
            sub = menu.addMenu(f"{mark} {s.get('name') or sid}  {where} · "
                               f"{s.get('tool_count', 0)} 工具")
            sub.addAction(state).setEnabled(False)
            if s.get("error"):
                err = sub.addAction(f"错误：{str(s.get('error'))[:60]}")
                err.setEnabled(False)
            sub.addAction("断开" if s.get("status") == "connected" else "连接",
                          lambda _c=False, x=sid: self._toggle_mcp_by_id(x))
            sub.addAction("删除…", lambda _c=False, x=sid: self._delete_mcp_by_id(x))
        if not servers:
            act = menu.addAction("（未配置 MCP 服务器）")
            act.setEnabled(False)

        menu.addSeparator()
        act = menu.addAction("🔍 从在线清单安装 MCP…")
        act.setToolTip("打开清单中心的 MCP 页：搜索官方 Registry 并一键生成配置")
        act.triggered.connect(lambda _c=False: self.open_manager("mcp", focus=True))
        act = menu.addAction("＋ 手动添加（stdio / http）…")
        act.setToolTip("手填命令 / 参数 / URL / 风险等级")
        act.triggered.connect(lambda _c=False: self._add_mcp())
        act = menu.addAction("＋ 粘贴 JSON 添加…")
        act.setToolTip("粘贴 Claude Desktop 的 mcpServers 配置片段即可添加")
        act.triggered.connect(lambda _c=False: self._paste_mcp_json())
        menu.addSeparator()
        act = menu.addAction("管理 MCP 服务器…")
        act.triggered.connect(lambda _c=False: self.open_manager("mcp"))
        return menu

    def _show_mcp_menu(self) -> None:
        self._popup_above(self._build_mcp_menu(), self._mcp_btn)

    def _toggle_mcp_by_id(self, sid: str) -> None:
        """按 id 连接/断开 MCP（菜单行用，不依赖列表选中项）。"""
        if not sid:
            return
        srv = next((s for s in (self._state.get("mcp") or [])
                    if isinstance(s, dict) and s.get("id") == sid), {})
        fn = (self._assist.disconnect_mcp if srv.get("status") == "connected"
              else self._assist.connect_mcp)
        self._set_status(f"正在{'断开' if srv.get('status') == 'connected' else '连接'}"
                         f"{srv.get('name') or sid}…")
        w = Worker(fn, sid)
        w.result.connect(lambda _r: self.refresh())
        w.error.connect(lambda m: self._set_status(f"操作失败：{m}"))
        w.start()

    def _delete_mcp_by_id(self, sid: str) -> None:
        if not sid:
            return
        if QMessageBox.question(self._dialog_parent(), "删除 MCP",
                                f"确定删除 {sid}？") \
                != QMessageBox.StandardButton.Yes:
            return
        w = Worker(self._assist.delete_mcp, sid)
        w.result.connect(lambda _r: self.refresh())
        w.error.connect(lambda m: QMessageBox.warning(
            self._dialog_parent(), "删除失败", m))
        w.start()

    def _reconnect_mcp(self) -> None:
        """⟳ 重连已启用 MCP：逐台 connect，失败逐条报错不中断（PRD §5.1.3）。"""
        servers = [s for s in (self._state.get("mcp") or [])
                   if isinstance(s, dict) and s.get("enabled", True)]
        if not servers:
            self._set_status("没有启用中的 MCP 服务器")
            return
        self._set_status(f"正在重连 {len(servers)} 台 MCP 服务器…")
        errors: List[str] = []
        remaining = {"n": len(servers)}

        def done() -> None:
            remaining["n"] -= 1
            if remaining["n"] > 0:
                return
            self.refresh()
            if errors:
                QMessageBox.warning(
                    self._dialog_parent(), "部分 MCP 重连失败",
                    f"{len(errors)} 台连接失败：\n" + "\n".join(errors[:8]))
                self._set_status(f"重连完成，{len(errors)} 台失败")
            else:
                self._set_status(f"已重连 {len(servers)} 台 MCP")

        for s in servers:
            sid = str(s.get("id") or "")
            w = Worker(self._assist.connect_mcp, sid)
            w.result.connect(lambda _r: done())
            w.error.connect(
                lambda m, x=sid: (errors.append(f"{x}: {m}"), done()))
            w.start()

    # ------------------------------------------------------------------ #
    # 添加类动作（本地文件夹 / URL / JSON）
    # ------------------------------------------------------------------ #
    def _import_skill_from_dir(self) -> None:
        """从本地文件夹导入技能（目录里必须有 SKILL.md）。

        **D5 硬约束**：必须复用 `_pick_file(directory=True)`；**禁止**直接调用
        QFileDialog 的原生目录选择静态方法——冻结包里一调原生目录框就以
        `0x8001010E` 崩。回归测试 `test_no_native_file_dialog_static_calls`
        会扫源码禁止那三个静态调用（本文档刻意不写出它们的字面量）。
        """
        start = (self._state.get("workspace")
                 or str(getattr(self.config, "assistant_workspace", "") or "")
                 or os.path.expanduser("~"))
        src = self._pick_file("选择技能文件夹（需含 SKILL.md）", start,
                              "所有文件 (*.*)", directory=True,
                              accept_text="选择此文件夹")
        if not src:
            return
        if not os.path.isdir(src):
            QMessageBox.warning(self._dialog_parent(), "目录不可用",
                                f"这个路径不是一个可用目录：\n{src}")
            return
        self._set_status(f"正在从本地文件夹导入技能：{src}")
        w = Worker(self._assist.catalog_from_dir, src, False)
        w.result.connect(self._on_installed_local_skill)
        w.error.connect(lambda m: self._on_install_failed(m, src))
        w.start()

    def _on_installed_local_skill(self, res: Any) -> None:
        slug = (res or {}).get("slug") if isinstance(res, dict) else ""
        files = (res or {}).get("files") if isinstance(res, dict) else None
        self.refresh()
        self._set_status(
            f"已从本地文件夹导入技能：{slug or '（未知）'}"
            + (f"（{files} 个文件）" if files else ""))
        self._auto_refresh_manager_lists()

    def _install_skill_from_url(self) -> None:
        """粘贴 URL 安装技能（raw 直链 / 含 /blob/ 的网页链接）。"""
        url, ok = QInputDialog.getText(
            self._dialog_parent(), "粘贴 URL 安装技能",
            "SKILL.md 链接（GitHub / Gitee 均可，网页链接会自动转 raw）：",
            QLineEdit.EchoMode.Normal, "")
        url = (url or "").strip()
        if not ok or not url:
            return
        self._set_status("正在从 URL 安装技能…")
        w = Worker(self._assist.catalog_from_url, url, False)
        w.result.connect(self._on_installed_url_skill)
        w.error.connect(lambda m: self._on_install_failed(m, url))
        w.start()

    def _on_installed_url_skill(self, res: Any) -> None:
        slug = (res or {}).get("slug") if isinstance(res, dict) else ""
        self.refresh()
        self._set_status(f"已从 URL 安装技能：{slug or '（未知）'}")
        self._auto_refresh_manager_lists()

    def _on_install_failed(self, msg: str, target: str) -> None:
        self._set_status(f"安装失败：{msg}")
        QMessageBox.warning(self._dialog_parent(), "安装失败",
                            f"{msg}\n\n目标：{target}")

    def _paste_mcp_json(self) -> None:
        """粘贴 JSON 添加 MCP（兼容 Claude Desktop 的 `{"mcpServers": {...}}`）。

        Claude Desktop 配置片段形如：
            {"mcpServers": {"filesystem": {"command": "npx",
              "args": ["-y", "@modelcontextprotocol/server-filesystem", "E:\\\\work"]}}}
        也接受单个服务器对象（`{"command":..., "args":[...]}`）与远程写法
        （`{"url": "http://..."}` / `{"type":"http","url":...}`）。
        """
        text, ok = QInputDialog.getMultiLineText(
            self._dialog_parent(), "粘贴 JSON 添加 MCP",
            '粘贴配置片段（Claude Desktop 的 {"mcpServers": {...}} 直接可用）：',
            '{\n  "mcpServers": {\n    "filesystem": {\n'
            '      "command": "npx",\n      "args": ["-y", '
            '"@modelcontextprotocol/server-filesystem", "."]\n'
            '    }\n  }\n}')
        if not ok:
            return
        try:
            servers = parse_mcp_json(text or "")
        except ValueError as e:
            QMessageBox.warning(self._dialog_parent(), "JSON 无法解析", str(e))
            return
        if not servers:
            QMessageBox.warning(self._dialog_parent(), "JSON 无法解析",
                                "没有找到任何 MCP 服务器配置（需要一个 command 或 url）。")
            return
        self._set_status(f"正在添加 {len(servers)} 台 MCP 服务器…")
        added = {"n": 0}
        errs: List[str] = []

        def done() -> None:
            if added["n"] > 0:
                return
            self.refresh()
            self._set_status(
                f"已添加 {len(servers) - len(errs)} 台 MCP"
                + (f"，{len(errs)} 台失败" if errs else ""))
            if errs:
                QMessageBox.warning(self._dialog_parent(), "部分 MCP 添加失败",
                                    "\n".join(errs[:8]))
            self._auto_refresh_manager_lists()

        for cfg in servers:
            w = Worker(self._assist.add_mcp, cfg)
            w.result.connect(lambda _r: (added.update(n=added["n"] + 1), done()))
            w.error.connect(
                lambda m, i=cfg.get("id"): (errs.append(f"{i}: {m}"), done()))
            w.start()

    def _refresh_catalog(self) -> None:
        """⟳ 刷新在线目录：三源并行刷新缓存，状态栏单行文字（不做进度条）。"""
        self._set_status("正在刷新在线目录（三源并行）…")
        w = Worker(self._assist.catalog_refresh)
        w.result.connect(self._on_catalog_refreshed)
        w.error.connect(lambda m: self._set_status(f"刷新在线目录失败：{m}"))
        w.start()

    def _on_catalog_refreshed(self, res: Any) -> None:
        counts = (res or {}).get("counts") if isinstance(res, dict) else {}
        errors = (res or {}).get("errors") if isinstance(res, dict) else None
        n_mcp = int((counts or {}).get("mcp") or 0)
        n_repo = int((counts or {}).get("gh_repos") or 0)
        txt = f"已刷新（{n_repo} 个技能仓库 / {n_mcp} 个 MCP）"
        if errors:
            txt += f"，{len(errors)} 个源失败"
        self._set_status(txt)
        if self._manager is not None and self._manager.isVisible():
            self._load_online_catalog(force=False)

    # ------------------------------------------------------------------ #
    # 在线目录（清单中心在线区）
    # ------------------------------------------------------------------ #
    def _load_online_catalog(self, force: bool = False) -> None:
        """拉在线目录（先渲染缓存，不阻塞）：MCP Registry + GitHub 仓库。"""
        w1 = Worker(self._assist.catalog_mcp, "", "", bool(force))
        w1.result.connect(self._apply_catalog_mcp)
        w1.error.connect(self._on_catalog_mcp_failed)
        w1.start()
        w2 = Worker(self._assist.catalog_repos, "", bool(force))
        w2.result.connect(self._apply_catalog_repos)
        w2.error.connect(self._on_catalog_repos_failed)
        w2.start()

    def _apply_catalog_mcp(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self._catalog_mcp = data
        lst = getattr(self, "_mcp_online_list", None)
        if lst is not None:
            lst.clear()
            for it in (data.get("items") or []):
                if not isinstance(it, dict):
                    continue
                badge = CATALOG_CLASS_BADGE.get(
                    str(it.get("free_class") or "unknown"), "未知")
                title = it.get("title") or it.get("registry_name") or ""
                item = QListWidgetItem(
                    f"{badge} {title}\n{it.get('description') or ''}")
                item.setData(Qt.UserRole, it)
                item.setToolTip(f"{it.get('registry_name') or ''}\n"
                                f"传输：{it.get('transport') or ''} · "
                                f"版本：{it.get('version') or ''}")
                lst.addItem(item)
            if not data.get("items"):
                lst.addItem("（在线目录为空：点「⟳ 刷新」或检查网络/代理）")
        self._update_catalog_banner("mcp", data)

    def _on_catalog_mcp_failed(self, msg: str) -> None:
        lst = getattr(self, "_mcp_online_list", None)
        self._update_catalog_banner(
            "mcp", self._catalog_mcp,
            error=str(msg), empty_hint="（还没有在线目录缓存。请检查网络/代理后点"
                                       "「⟳ 刷新」，或先用「手动添加」加 MCP）")

    def _apply_catalog_repos(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        self._catalog_repos = data
        lst = getattr(self, "_skill_online_list", None)
        if lst is not None:
            lst.clear()
            for it in (data.get("items") or []):
                if not isinstance(it, dict):
                    continue
                item = QListWidgetItem(
                    f"⭐{it.get('stars') or 0}  {it.get('full_name') or ''}\n"
                    f"{it.get('description') or ''}")
                item.setData(Qt.UserRole, it)
                item.setToolTip(f"{it.get('full_name') or ''}\n"
                                f"默认分支：{it.get('default_branch') or 'main'}")
                lst.addItem(item)
            if not data.get("items"):
                lst.addItem("（在线目录为空：点「⟳ 刷新」或检查网络/代理）")
        self._update_catalog_banner("gh_repos", data)

    def _on_catalog_repos_failed(self, msg: str) -> None:
        self._update_catalog_banner(
            "gh_repos", self._catalog_repos, error=str(msg),
            empty_hint="（还没有在线目录缓存。请检查网络/代理后点「⟳ 刷新」，"
                       "或先用「导入 zip / 本地文件夹」添加技能）")

    def _update_catalog_banner(self, kind: str, data: Any,
                               error: str = "", empty_hint: str = "") -> None:
        """顶部提示条：离线退化时显示缓存时间 + 原因，绝不出现空白页（P0-8）。"""
        lbl = (self._skill_online_banner if kind == "gh_repos"
               else self._mcp_online_banner)
        if lbl is None:
            return
        data = data if isinstance(data, dict) else {}
        fetched = float(data.get("fetched_at") or 0)
        when = (time.strftime("%m-%d %H:%M", time.localtime(fetched))
                if fetched else "")
        stale = bool(data.get("stale"))
        err = error or str(data.get("error") or "")
        if err:
            txt = f"在线目录刷新失败：{err}"
            if when:
                txt += f" · 正在显示 {when} 的缓存"
            elif empty_hint:
                txt = empty_hint
            txt += " · 点右侧「⟳ 刷新」重试"
            lbl.setText(txt)
            lbl.setObjectName("CatalogWarn")
        elif stale and when:
            lbl.setText(f"正在显示 {when} 的缓存（点「⟳ 刷新」同步最新）")
            lbl.setObjectName("CatalogNote")
        else:
            lbl.setText("")
            lbl.setObjectName("PageSubtitle")
        lbl.setVisible(bool(lbl.text().strip()))
        try:
            lbl.style().unpolish(lbl)
            lbl.style().polish(lbl)
        except Exception:               # 样式表未加载时不影响功能
            pass

    def _auto_refresh_manager_lists(self) -> None:
        """安装/添加成功后重填清单中心三张列表（面板没开时是空操作）。"""
        if self._manager is None:
            return
        self._fill_skills(self._state.get("skills") or [])
        self._fill_tools(self._state.get("tools") or [])
        self._fill_mcp(self._state.get("mcp") or [])

    # ------------------------------------------------------------------ #
    # 技能
    # ------------------------------------------------------------------ #
    def _new_skill(self) -> None:
        dlg = SkillEditor(self, slug="", content=_SKILL_TEMPLATE)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_skill(dlg.slug(), dlg.content())

    def _edit_skill_item(self, item) -> None:
        slug = item.data(Qt.UserRole) if item else None
        if not slug:
            return
        w = Worker(self._assist.skill, slug)
        w.result.connect(self._open_skill_editor)
        w.start()

    def _open_skill_editor(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        dlg = SkillEditor(self, slug=data.get("slug") or "",
                          content=_compose_skill_md(data))
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._save_skill(dlg.slug(), dlg.content())

    def _save_skill(self, slug: str, content: str) -> None:
        w = Worker(self._assist.save_skill, slug, content)
        w.result.connect(lambda _r: (self.refresh(),
                                     self._set_status(f"技能已保存：{slug}")))
        w.error.connect(lambda m: QMessageBox.warning(self, "保存失败", m))
        w.start()

    def _delete_skill(self) -> None:
        item = self._skill_list.currentItem()
        slug = item.data(Qt.UserRole) if item else None
        if not slug:
            return
        if QMessageBox.question(self, "删除技能", f"确定删除技能 {slug}？") \
                != QMessageBox.StandardButton.Yes:
            return
        w = Worker(self._assist.delete_skill, slug)
        w.result.connect(lambda _r: self.refresh())
        w.start()

    def _import_skill(self) -> None:
        path = self._pick_file("导入技能包", "", "技能包 (*.zip)",
                               accept_text="导入")
        if not path:
            return

        def do_import():
            with open(path, "rb") as f:
                return self._assist.import_skill(f.read())

        w = Worker(do_import)
        w.result.connect(self._on_imported)
        w.error.connect(lambda m: QMessageBox.warning(self, "导入失败", m))
        w.start()

    def _on_imported(self, res: Dict[str, Any]) -> None:
        names = (res or {}).get("imported") or []
        self.refresh()
        if names:
            QMessageBox.information(self, "导入完成", "已导入：" + "、".join(names))

    def _export_skill(self) -> None:
        item = self._skill_list.currentItem()
        slug = item.data(Qt.UserRole) if item else None
        if not slug:
            return
        path = self._pick_file("导出技能包", "", "技能包 (*.zip)", save=True,
                               default_name=f"{slug}.zip", accept_text="导出")
        if not path:
            return

        def do_export():
            data = self._assist.export_skill(slug)
            with open(path, "wb") as f:
                f.write(data)
            return path

        w = Worker(do_export)
        w.result.connect(lambda p: self._set_status(f"已导出：{p}"))
        w.error.connect(lambda m: QMessageBox.warning(self, "导出失败", m))
        w.start()

    # ------------------------------------------------------------------ #
    # MCP
    # ------------------------------------------------------------------ #
    def _add_mcp(self) -> None:
        dlg = McpDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        cfg = dlg.payload()
        if not cfg:
            return
        w = Worker(self._assist.add_mcp, cfg)
        w.result.connect(self._on_mcp_added)
        w.error.connect(lambda m: QMessageBox.warning(self, "添加失败", m))
        w.start()

    def _on_mcp_added(self, res: Dict[str, Any]) -> None:
        self.refresh()
        err = (res or {}).get("error")
        if err:
            QMessageBox.warning(self, "MCP 连接失败",
                                f"服务器已保存，但连接失败：\n{err}")
        else:
            self._set_status("MCP 服务器已连接")

    def _toggle_mcp(self) -> None:
        item = self._mcp_list.currentItem()
        sid = item.data(Qt.UserRole) if item else None
        if not sid:
            return
        srv = next((s for s in (self._state.get("mcp") or [])
                    if s.get("id") == sid), {})
        fn = (self._assist.disconnect_mcp if srv.get("status") == "connected"
              else self._assist.connect_mcp)
        w = Worker(fn, sid)
        w.result.connect(lambda _r: self.refresh())
        w.error.connect(lambda m: QMessageBox.warning(self, "操作失败", m))
        w.start()

    def _delete_mcp(self) -> None:
        item = self._mcp_list.currentItem()
        sid = item.data(Qt.UserRole) if item else None
        if not sid:
            return
        if QMessageBox.question(self, "删除 MCP", f"确定删除 {sid}？") \
                != QMessageBox.StandardButton.Yes:
            return
        w = Worker(self._assist.delete_mcp, sid)
        w.result.connect(lambda _r: self.refresh())
        w.start()

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def _refresh_audit(self) -> None:
        w = Worker(self._assist.audit, 150)
        w.result.connect(self._apply_audit)
        w.start()

    def _apply_audit(self, items) -> None:
        lines = []
        for it in (items or []):
            t = it.get("time") or ""
            ev = it.get("event") or ""
            extra = {k: v for k, v in it.items() if k not in ("ts", "time", "event")}
            lines.append(f"{t}  {ev}  {json.dumps(extra, ensure_ascii=False)[:300]}")
        self._audit_view.setPlainText("\n".join(lines) or "（暂无审计记录）")


# ----------------------------------------------------------------------------
# 对话框
# ----------------------------------------------------------------------------
class ManagerDialog(QDialog):
    """能力清单中心（原「管理面板」升级，PRD §5.2）。

    **不新建独立页面**（PRD §5.2 结论）：左栏 1.0.8 起已无二级管理入口，
    新建页面要么改左栏（挤占 680px 高）要么做页面切换（波及 QStackedWidget）。
    用户原话要的是「打开清单 → 添加 → 回到对话」，QDialog 天然满足。

    窗口尺寸（架构 A7，**既有缺陷一并修掉**）：
      - 原 `setMinimumSize(760, 560)` 在逻辑屏宽 **720** 下放不下（760 > 720），
        200% 缩放的 1366×768 笔记本上窗口会超出屏幕右缘。改为 **700×520**。
      - 打开时 `resize(主窗口 86% × 84%)` 并封顶到屏幕可用区。
    右侧详情面板在宽度 < 720 时自动折叠（`_auto_fold_detail`）。
    """

    TABS = [("sessions", "任务"), ("skills", "技能"), ("tools", "工具"),
            ("mcp", "MCP"), ("audit", "审计")]
    #: 架构 A7 定案：必须 ≤700 才放得进逻辑 720 屏宽
    MIN_W, MIN_H = 700, 520
    #: 详情面板折叠阈值（= 主窗口最小逻辑宽）
    FOLD_W = 720

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("智能助理 · 能力清单中心")
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        #: 在线区是否已展开（open_manager 据此决定是否预拉目录）
        self._online_visible = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.addTab(page._build_sessions_tab(), "任务")
        self.tabs.addTab(page._build_skills_tab(), "技能")
        self.tabs.addTab(page._build_tools_tab(), "工具")
        self.tabs.addTab(page._build_mcp_tab(), "MCP")
        self.tabs.addTab(page._build_audit_tab(), "审计")
        lay.addWidget(self.tabs, 1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        close_btn = box.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.setText("关闭")
        lay.addWidget(box)

    def fit_to_parent(self) -> None:
        """按主窗口 86% × 84% 定尺寸，并封顶到屏幕可用区（架构 A7）。"""
        try:
            host = self.parentWidget()
            base_w = max(self.MIN_W, int((host.width() if host is not None else 720)))
            base_h = max(self.MIN_H, int((host.height() if host is not None else 680)))
            w = max(self.MIN_W, int(base_w * 0.86))
            h = max(self.MIN_H, int(base_h * 0.84))
            scr = self.screen()
            if scr is not None:
                avail = scr.availableGeometry()
                w = min(w, max(self.MIN_W, avail.width() - 24))
                h = min(h, max(self.MIN_H, avail.height() - 24))
            self.resize(w, h)
        except Exception:                     # 无头环境 / 尺寸异常：保持最小尺寸
            self.resize(self.MIN_W, self.MIN_H)
        self._auto_fold_detail()

    def _auto_fold_detail(self) -> None:
        """宽度 < 720 时把技能详情面板折叠（PRD §5.2「详情自动折叠」）。"""
        split = getattr(self.page, "_skill_split", None)
        if split is None:
            return
        narrow = self.width() < self.FOLD_W
        try:
            split.setSizes([1, 0] if narrow else [620, 380])
        except Exception:
            pass

    def focus_search(self) -> None:
        """聚焦技能搜索框（`[技能]` 菜单「从在线清单安装…」用）。"""
        box = getattr(self.page, "_skill_search", None)
        if box is not None:
            self.tabs.setCurrentIndex(1)
            box.setFocus()

    def show_tab(self, key: str) -> None:
        """切到指定 Tab；key 不在表里时静默忽略（兼容旧调用）。"""
        keys = [k for k, _ in self.TABS]
        if key in keys:
            self.tabs.setCurrentIndex(keys.index(key))

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._auto_fold_detail()


_SKILL_TEMPLATE = """---
name: my-skill
description: 一句话说明这个技能做什么
tools: [fs_list, fs_read]
version: 1.0.0
---
# 目标
（这个技能要达成的结果）

# 执行要求
1. 先……
2. 再……

# 约束
- ……
"""


def _compose_skill_md(data: Dict[str, Any]) -> str:
    meta = data.get("meta") or {}
    fm = ["---"]
    fm.append(f"name: {meta.get('name') or data.get('slug') or ''}")
    fm.append(f"description: {meta.get('description') or ''}")
    tools = meta.get("tools") or []
    fm.append("tools: [" + ", ".join(tools) + "]")
    fm.append(f"version: {meta.get('version') or '1.0.0'}")
    fm.append("---")
    return "\n".join(fm) + "\n" + (data.get("body") or "")


class SkillEditor(QDialog):
    """SKILL.md 编辑器：第一部分是标识，下面是正文。"""

    def __init__(self, parent=None, slug: str = "", content: str = ""):
        super().__init__(parent)
        self.setWindowTitle("技能编辑")
        self.setMinimumSize(720, 560)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self._slug = QLineEdit(slug)
        self._slug.setPlaceholderText("目录名，仅字母数字点下划线连字符")
        form.addRow("标识", self._slug)
        lay.addLayout(form)

        tip = QLabel("SKILL.md 内容（--- 之间是 frontmatter：name / description / tools）")
        tip.setObjectName("PageSubtitle")
        lay.addWidget(tip)

        self._edit = QTextEdit()
        self._edit.setPlainText(content or _SKILL_TEMPLATE)
        lay.addWidget(self._edit, 1)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def slug(self) -> str:
        return self._slug.text().strip()

    def content(self) -> str:
        return self._edit.toPlainText()


class ProviderDialog(QDialog):
    """配置非本地提供商的 Base URL / API Key / 默认模型。"""

    def __init__(self, parent=None, provider: Optional[Dict[str, Any]] = None):
        super().__init__(parent)
        provider = provider or {}
        self.setWindowTitle(f"配置 {provider.get('label') or provider.get('id') or '提供商'}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self._base = QLineEdit(provider.get("base_url") or "")
        self._base.setPlaceholderText("https://api.siliconflow.cn/v1")
        form.addRow("Base URL", self._base)

        self._key = QLineEdit("")
        self._key.setEchoMode(QLineEdit.EchoMode.Password)
        self._key.setPlaceholderText(
            "已配置（留空保持不变）" if provider.get("has_key")
            else "sk-…（仅保存在本机 assistant/config.json）")
        form.addRow("API Key", self._key)

        self._model = QLineEdit(provider.get("model") or "")
        self._model.setPlaceholderText("默认模型，如 Qwen/Qwen3-8B")
        form.addRow("默认模型", self._model)

        lay.addLayout(form)
        tip = QLabel("说明：硅基流动 Base URL 为 https://api.siliconflow.cn/v1；"
                     "其它第三方（DeepSeek / 通义 / vLLM / Ollama）填其 OpenAI 兼容地址即可。")
        tip.setObjectName("PageSubtitle")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def payload(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"base_url": self._base.text().strip(),
                               "model": self._model.text().strip()}
        key = self._key.text().strip()
        if key:
            out["api_key"] = key
        return out


class McpDialog(QDialog):
    """添加 MCP 服务器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("添加 MCP 服务器")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        form = QFormLayout()

        self._id = QLineEdit()
        self._id.setPlaceholderText("英文标识，如 filesystem")
        form.addRow("标识", self._id)

        self._name = QLineEdit()
        self._name.setPlaceholderText("显示名，如 文件系统")
        form.addRow("名称", self._name)

        self._transport = QComboBox()
        self._transport.addItem("stdio（本机子进程）", "stdio")
        self._transport.addItem("http（远程/本地 HTTP）", "http")
        form.addRow("传输", self._transport)

        self._command = QLineEdit("npx")
        self._command.setPlaceholderText("如 npx / uvx / python")
        form.addRow("命令", self._command)

        self._args = QPlainTextEdit()
        self._args.setFixedHeight(70)
        self._args.setPlaceholderText("每行一个参数，例如：\n-y\n@modelcontextprotocol/server-filesystem\nE:\\work")
        form.addRow("参数", self._args)

        self._url = QLineEdit()
        self._url.setPlaceholderText("http 传输时填，如 http://127.0.0.1:3000/mcp")
        form.addRow("URL", self._url)

        self._risk = QComboBox()
        for k, v in RISK_TEXT.items():
            self._risk.addItem(v, k)
        self._risk.setCurrentIndex(1)
        form.addRow("工具风险", self._risk)

        lay.addLayout(form)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

    def payload(self) -> Dict[str, Any]:
        sid = self._id.text().strip() or f"mcp{int(__import__('time').time())}"
        transport = self._transport.currentData()
        args = [ln.strip() for ln in self._args.toPlainText().splitlines() if ln.strip()]
        return {
            "id": sid,
            "name": self._name.text().strip() or sid,
            "transport": transport,
            "command": self._command.text().strip(),
            "args": args,
            "url": self._url.text().strip(),
            "risk": self._risk.currentData(),
            "enabled": True,
            "connect": True,
        }
