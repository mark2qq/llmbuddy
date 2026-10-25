# -*- coding: utf-8 -*-
"""
智能助理页面（ui/pages/assistant_page.py）——1.0.8 新模块。

布局（1.0.8 按 WorkBuddy 对话框形态重排）：
    左栏（主窗口 NavPanel 内）              右栏（本页）
      智能助理                          ┌ 标题 + 管理入口 ────────────────┐
        ▸ 任务（最近 6 条，点即切换）    │ 对话流（气泡 / 工具卡 / 审批卡） │
        ▸ 技能 / 工具 / MCP / 审计      │ ┌─ 输入框 ────────────────────┐ │
          （点开管理面板）              │ │ [+] [工作区][审批][技能]      │ │
                                        │ │          ◐  [模型 ▾]   [↑]  │ │
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
"""
import json
import os
import time
import tempfile
from typing import Any, Dict, List, Optional

from PySide6.QtCore import QEvent, QPoint, QSettings, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QIcon, QPainter, QPainterPath, QPixmap,
                           QTextCursor)
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QInputDialog,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QSizePolicy, QTabWidget, QTextEdit, QVBoxLayout,
                               QWidget)

from core.assistant_client import AssistantApi, AssistantStream
from core.task_runner import Worker
from core.theme import is_light
from ui.widgets.assistant_cards import (ApprovalCard, ChatBubble, StepLine,
                                       ThinkingBlock, ToolCard)
from ui.widgets.auto_scroll_area import AutoHideScrollArea

POLICY_ITEMS = [
    ("readonly_auto", "只读自动", "只读类工具直接执行，写入/高危仍逐次确认（推荐）"),
    ("ask_all", "全部确认", "任何工具调用都要你确认后才执行"),
    ("yolo", "全部放行", "所有工具直接执行、不再询问（危险）"),
]
POLICY_ICON = {"readonly_auto": "⊘", "ask_all": "✓", "yolo": "⚡"}
RISK_TEXT = {"low": "只读", "medium": "写入", "high": "高危"}

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
        """底部固定区：状态提示 / 输入框 / 合规提示（与对话列同宽居中）。"""
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

        il.addWidget(self._build_composer())

        tip = QLabel("内容由 AI 生成，请核实重要信息")
        tip.setObjectName("ComposerTip")
        tip.setAlignment(Qt.AlignCenter)
        il.addWidget(tip)

        outer.addStretch(1)
        outer.addWidget(inner, 100)
        outer.addStretch(1)
        return foot

    def _build_composer(self) -> QWidget:
        """
        底部输入区（对齐 WorkBuddy 输入框底部工具条）：

            ┌ 今天帮你做些什么？ ───────────────────────────────┐
            │                                                        │
            │ [+] [工作区] [⊘ 只读自动] [技能]     ◐  [模型 ▾]  [↑] │
            └────────────────────────────────────────────────────────┘

        左侧是「动作与上下文」，右侧是「运行状态 / 模型 / 发送」；
        发送在流式时原地变成停止。
        """
        box = QFrame()
        box.setObjectName("Composer")
        box.setProperty("focus", False)
        self._composer = box
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 10, 8)
        lay.setSpacing(2)

        self._input = QTextEdit()
        self._input.setObjectName("ComposerInput")
        self._input.setFrameShape(QFrame.NoFrame)
        self._input.setPlaceholderText(
            "今天帮你做些什么？（Enter 发送，Shift+Enter 换行）")
        self._input.setFixedHeight(66)
        self._input.installEventFilter(self)
        lay.addWidget(self._input)

        bar = QHBoxLayout()
        bar.setSpacing(2)

        # 左：附件 / 工作区 / 审批策略 / 技能
        self._attach_btn = QPushButton("+")
        self._attach_btn.setObjectName("BarBtn")
        self._attach_btn.setFixedSize(26, 26)
        self._attach_btn.setCursor(Qt.PointingHandCursor)
        self._attach_btn.setToolTip("添加附件，或打开管理面板（技能 / 工具 / MCP / 审计）")
        self._attach_btn.clicked.connect(self._show_attach_menu)
        bar.addWidget(self._attach_btn)

        self._ws_btn = QPushButton("工作区")
        self._ws_btn.setObjectName("BarChip")
        self._ws_btn.setCursor(Qt.PointingHandCursor)
        self._ws_btn.setToolTip("切换文件工具可访问的工作区")
        self._ws_btn.clicked.connect(self._choose_workspace)
        bar.addWidget(self._ws_btn)

        self._policy_btn = QPushButton("⊘ 只读自动")
        self._policy_btn.setObjectName("BarChip")
        self._policy_btn.setCursor(Qt.PointingHandCursor)
        self._policy_btn.setToolTip("高危动作审批策略（点击切换）")
        self._policy_btn.clicked.connect(self._show_policy_menu)
        bar.addWidget(self._policy_btn)

        self._skill_btn = QPushButton("技能")
        self._skill_btn.setObjectName("BarChip")
        self._skill_btn.setCursor(Qt.PointingHandCursor)
        self._skill_btn.setToolTip("挂载技能：之后每条消息自动带上「用 xx 技能」")
        self._skill_btn.clicked.connect(self._show_skill_menu)
        bar.addWidget(self._skill_btn)

        # 多技能挂载区：每个已挂载技能一个可移除 chip（最多 5 个）
        self._skill_chip_cont = QWidget()
        self._skill_chip_cont.setObjectName("SkillChips")
        chip_lay = QHBoxLayout(self._skill_chip_cont)
        chip_lay.setContentsMargins(0, 0, 0, 0)
        chip_lay.setSpacing(2)
        self._skill_chip_lay = chip_lay
        self._skill_chip_cont.setVisible(False)
        bar.addWidget(self._skill_chip_cont)

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
        self._model_btn.setCursor(Qt.PointingHandCursor)
        self._model_btn.setToolTip("切换模型来源与模型")
        self._model_btn.clicked.connect(self._show_model_menu)
        bar.addWidget(self._model_btn)

        self._stop_btn = QPushButton("■")
        self._stop_btn.setObjectName("StopBtn")
        self._stop_btn.setFixedSize(30, 30)
        self._stop_btn.setCursor(Qt.PointingHandCursor)
        self._stop_btn.setToolTip("停止生成")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._stop)
        bar.addWidget(self._stop_btn)

        self._send_btn = QPushButton("↑")
        self._send_btn.setObjectName("SendBtn")
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

    # ---------------- 底栏弹出菜单（贴近窗口底部，向上展开） ----------------
    def _popup_above(self, menu: QMenu, anchor: QWidget) -> None:
        menu.ensurePolished()
        size = menu.sizeHint()
        pos = anchor.mapToGlobal(QPoint(0, 0))
        menu.exec(QPoint(pos.x(), pos.y() - size.height() - 6))

    # ---------------- 文件选择（统一入口，禁止原生对话框） ----------------
    def _pick_file(self, title: str, start: str = "",
                   name_filter: str = "所有文件 (*.*)", save: bool = False,
                   default_name: str = "", accept_text: str = "选择") -> str:
        """统一的文件选择入口，返回选中路径（取消返回 ""）。

        **必须 DontUseNativeDialog**：本机冻结包里，系统原生文件对话框一调用就
        以 `0x8001010E RPC_E_WRONG_THREAD`（COM 套间错误）直接杀掉进程，
        crash.log 留有栈：`_attach_file → QFileDialog.getOpenFileName`。
        表现为「点 + 或点工作区就闪退」且没有任何 Python 异常。自绘框没有这条
        路径依赖，最坏情况只是不弹出。同时标签写死中文，不依赖 Qt 翻译文件。
        """
        try:
            fd = QFileDialog(self._dialog_parent(), title, start)
            fd.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            fd.setNameFilter(name_filter)
            if save:
                fd.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
                fd.setFileMode(QFileDialog.FileMode.AnyFile)
                if default_name:
                    fd.selectFile(default_name)
            else:
                fd.setFileMode(QFileDialog.FileMode.ExistingFile)
            fd.setLabelText(QFileDialog.DialogLabel.LookIn, "位置：")
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

    # ---------------- 底栏 [+] 菜单：附件 + 四个管理入口 ----------------
    def _build_attach_menu(self) -> "QMenu":
        """[+] 弹出菜单：第一项「添加附件」走原逻辑，分隔线后四个管理入口。"""
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")  # 复用深浅样式
        add = menu.addAction("添加附件…")
        add.setToolTip("把本机文件加进本次提问（插入 @路径）")
        add.triggered.connect(self._attach_file)
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


    # ---------------- 管理面板（任务 / 技能 / 工具 / MCP / 审计） ----------------
    def open_manager(self, key: str = "sessions") -> None:
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
        dlg.exec()

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
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self._hint(
            "技能 = SKILL.md（frontmatter: name / description / tools）+ 正文；"
            "与 OpenClaw、Claude 技能包同构，可用 zip 互相导入导出。"))
        self._skill_list = QListWidget()
        self._skill_list.itemDoubleClicked.connect(self._edit_skill_item)
        lay.addWidget(self._skill_list, 1)
        row1 = QHBoxLayout()
        b_new = QPushButton("新建")
        b_new.clicked.connect(self._new_skill)
        b_edit = QPushButton("编辑")
        b_edit.clicked.connect(
            lambda: self._edit_skill_item(self._skill_list.currentItem()))
        b_del = QPushButton("删除")
        b_del.clicked.connect(self._delete_skill)
        for b in (b_new, b_edit, b_del):
            row1.addWidget(b)
        row1.addStretch(1)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        b_import = QPushButton("导入 zip")
        b_import.clicked.connect(self._import_skill)
        b_export = QPushButton("导出 zip")
        b_export.clicked.connect(self._export_skill)
        row2.addWidget(b_import)
        row2.addWidget(b_export)
        row2.addStretch(1)
        lay.addLayout(row2)
        return w

    def _build_tools_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        self._tool_list = QListWidget()
        lay.addWidget(self._tool_list, 1)
        lay.addWidget(self._hint("只读工具自动执行；写入 / 高危按底栏的审批策略处理"))
        return w

    def _build_mcp_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)
        lay.addWidget(self._hint(
            "连接成功后，MCP 工具自动以 mcp__<标识>__<工具> 进白名单，审批规则同上。"))
        self._mcp_list = QListWidget()
        lay.addWidget(self._mcp_list, 1)
        row = QHBoxLayout()
        b_add = QPushButton("添加")
        b_add.clicked.connect(self._add_mcp)
        b_conn = QPushButton("连接/断开")
        b_conn.clicked.connect(self._toggle_mcp)
        b_rm = QPushButton("删除")
        b_rm.clicked.connect(self._delete_mcp)
        for b in (b_add, b_conn, b_rm):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        return w

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
    def _refresh_ws_btn(self, ws: str = "") -> None:
        """工作区按钮显示目录名（WorkBuddy 的「分支 master」位：当前上下文根）。"""
        path = (ws or str(self._state.get("workspace") or "")
                or str(getattr(self.config, "assistant_workspace", "") or ""))
        name = os.path.basename(path.rstrip("\\/")) if path else ""
        if len(name) > 12:
            name = name[:11] + "…"
        self._ws_btn.setText(name or "工作区")
        self._ws_btn.setToolTip(
            f"文件工具可访问的工作区：\n{path or '（未设置）'}\n（点击切换）")

    def _refresh_policy_btn(self) -> None:
        pid = str(self._state.get("policy") or "readonly_auto")
        label = next((lb for p, lb, _t in POLICY_ITEMS if p == pid), pid)
        tip = next((t for p, _lb, t in POLICY_ITEMS if p == pid), "")
        self._policy_btn.setText(f"{POLICY_ICON.get(pid, '⊘')} {label}")
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

    def _refresh_model_btn(self) -> None:
        names = self._model_cache.get(self._active_provider) or []
        model = self._active_model or (names[0] if names else "")
        provider = next((p for p in (self._state.get("providers") or [])
                         if p.get("id") == self._active_provider), {})
        plabel = provider.get("label") or self._active_provider or "模型"
        text = model or "选择模型"
        if len(text) > 26:
            text = text[:25] + "…"
        self._model_btn.setText(f"{text}   ▾")
        self._model_btn.setToolTip(
            f"当前：{plabel} · {model or '（默认）'}\n"
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
    def _fill_skills(self, skills: List[Dict[str, Any]]) -> None:
        lst = getattr(self, "_skill_list", None)
        if lst is None:
            return
        lst.clear()
        for s in skills:
            item = QListWidgetItem(f"{s.get('name')}   ·  {len(s.get('tools') or [])} 工具")
            item.setData(Qt.UserRole, s.get("slug"))
            item.setToolTip(s.get("description") or "")
            lst.addItem(item)
        if not skills:
            lst.addItem("（暂无技能，点「新建」或「导入 zip」）")

    def _fill_tools(self, tools: List[Dict[str, Any]]) -> None:
        lst = getattr(self, "_tool_list", None)
        if lst is None:
            return
        lst.clear()
        for t in tools:
            risk = RISK_TEXT.get(t.get("risk"), t.get("risk"))
            src = t.get("source") or "builtin"
            item = QListWidgetItem(f"[{risk}] {t.get('name')}   · {src}")
            item.setToolTip(t.get("description") or "")
            lst.addItem(item)

    def _fill_mcp(self, servers: List[Dict[str, Any]]) -> None:
        lst = getattr(self, "_mcp_list", None)
        if lst is None:
            return
        lst.clear()
        for s in servers:
            mark = {"connected": "●", "error": "×"}.get(s.get("status"), "○")
            item = QListWidgetItem(
                f"{mark} {s.get('name')}  · {s.get('transport')} · "
                f"{s.get('tool_count', 0)} 工具")
            item.setData(Qt.UserRole, s.get("id"))
            if s.get("error"):
                item.setToolTip(s["error"])
            lst.addItem(item)
        if not servers:
            lst.addItem("（未配置 MCP 服务器）")

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

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if mods & Qt.ShiftModifier:
                    return False
                self._on_send_clicked()
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
        if self._busy:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        if not self._session_id:
            w = Worker(self._assist.new_session)
            w.result.connect(lambda s: self._send_with_session(s, text))
            w.start()
            return
        self._send_with_session({"id": self._session_id}, text)

    def _send_with_session(self, session: Any, text: str) -> None:
        if not isinstance(session, dict) or not session.get("id"):
            self._set_status("任务创建失败，请检查后端服务")
            return
        self._session_id = session["id"]
        self._input.clear()
        self._add_bubble("user", text)
        self._scroll_bottom(force=True)   # 用户刚发消息：一定回到底部
        self._begin_streaming()

        # 技能 chip：挂载后每条消息自动带上「用 xx、yy 技能」，输入框本身保持干净
        message = (f"用 {'、'.join(self._skill_slugs)} 技能：{text}"
                   if self._skill_slugs else text)
        payload = {
            "session_id": self._session_id,
            "message": message,
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

    def _begin_streaming(self) -> None:
        self._busy = True
        self._send_btn.setVisible(False)
        self._stop_btn.setVisible(True)
        self._stop_btn.setEnabled(True)
        self._start_spin()
        self._set_status("助理正在工作…")
        self._bubble = None
        self._thinking = None
        self._tool_cards = []

    def _end_streaming(self) -> None:
        self._busy = False
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)
        self._send_btn.setVisible(True)
        self._send_btn.setEnabled(True)
        self._stop_spin()
        if self._thinking is not None:
            self._thinking.finish()
        if self._bubble is not None:
            self._bubble.end_stream()      # 收尾贴合宽度
        self._scroll_bottom(force=True)    # 答完了，回到底部看全文

    def _stop(self) -> None:
        if self._stream is not None:
            self._stream.cancel()
        sid = self._session_id
        if sid:
            Worker(lambda s=sid: self._assist.stop(s)).start()
        self._add_step("已请求停止")
        self._end_streaming()
        self._set_status("已停止")

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
        if kind == "session":
            self._session_id = ev.get("session_id") or self._session_id
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
        elif kind == "done":
            self._finish_bubble(ev.get("content") or "")
            self._set_status("完成")
            self._scroll_bottom()
        elif kind == "error":
            self._add_step(f"出错了：{ev.get('message')}", error=True)
            self._set_status("出错，详见对话区")
        elif kind == "stopped":
            self._add_step("已停止生成")
        elif kind == "end":
            self._end_streaming()
            if not ev.get("error"):
                self._set_status("就绪")
            self._approvals.clear()

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
        """构建技能菜单（多技能同时挂载，上限 5 个）。

        鼠标悬停技能项显示详细介绍（SKILL.md 的 description + 工具数）——
        QMenu 必须开 setToolTipsVisible(True)，否则 setToolTip 不显示。
        """
        menu = QMenu(self)
        menu.setObjectName("ModelMenu")  # 复用对话页的菜单样式
        menu.setToolTipsVisible(True)
        note = menu.addAction(f"最多同时挂载 {self.MAX_SKILLS} 个")
        note.setEnabled(False)
        skills = self._state.get("skills") or []
        full = len(self._skill_slugs) >= self.MAX_SKILLS
        if not skills:
            act = menu.addAction("（暂无技能，去底栏 [+] → 技能 里新建或导入 zip）")
            act.setEnabled(False)
        else:
            menu.addSeparator()
            for s in skills:
                slug = s.get("slug") or ""
                name = s.get("name") or slug
                mounted = slug in self._skill_slugs
                act = menu.addAction(name)
                tip = str(s.get("description") or "").strip()
                ntools = len(s.get("tools") or [])
                if ntools:
                    tip = f"{tip}\n包含 {ntools} 个工具" if tip \
                        else f"包含 {ntools} 个工具"
                if mounted:
                    tip = f"{tip}\n已挂载" if tip else "已挂载"
                elif full:
                    tip = f"{tip}\n已挂载 {self.MAX_SKILLS} 个，先移除一个" if tip \
                        else "已挂载 5 个，先移除一个"
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
        if self._skill_slugs:
            menu.addSeparator()
            clear = menu.addAction("清除全部")
            clear.triggered.connect(self._clear_skills)
        return menu

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
        # 占位按钮：始终保持「技能」文案，挂载数变化时切换高亮与 tooltip
        self._skill_btn.setText("技能")
        has = bool(slugs)
        tip = (f"已挂载 {len(slugs)} 个技能：" + "、".join(slugs) + "\n点击增删"
               if slugs else "挂载技能：之后每条消息自动带上「用 xx 技能」")
        self._skill_btn.setToolTip(tip)
        if bool(self._skill_btn.property("active")) != has:
            self._skill_btn.setProperty("active", has)
            self._skill_btn.style().unpolish(self._skill_btn)
            self._skill_btn.style().polish(self._skill_btn)

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
    """智能助理管理面板（原右侧 tab 面板整块搬到这里）。

    左栏只做「任务 / 空间两区 + 四个入口」，真正需要空间的列表操作
    （全部任务、技能包导入导出、工具清单、MCP 增删、审计明细）在面板里。
    """

    TABS = [("sessions", "任务"), ("skills", "技能"), ("tools", "工具"),
            ("mcp", "MCP"), ("audit", "审计")]

    def __init__(self, page):
        super().__init__(page)
        self.page = page
        self.setWindowTitle("智能助理 · 管理")
        self.setMinimumSize(760, 560)

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

    def show_tab(self, key: str) -> None:
        keys = [k for k, _ in self.TABS]
        if key in keys:
            self.tabs.setCurrentIndex(keys.index(key))


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
