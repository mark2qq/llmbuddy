# -*- coding: utf-8 -*-
"""
智能助理页面（ui/pages/assistant_page.py）——1.0.8 新模块。

布局：
    ┌ 顶部：标题 + 提供商/模型/审批策略 + 工作区 + 新会话 ─────────────┐
    │ 左：对话流（气泡 / 工具卡片 / 审批卡片 / 技能阶段）+ 输入区      │ 右：侧栏
    │                                                                  │ 会话/技能/工具/MCP/审计
    └──────────────────────────────────────────────────────────────────┘

能力（全部走本机后端 /api/v1/assistant/*）：
    - 操作本机文件、执行命令、抓网页（工具卡片可视化每次调用）
    - MCP 服务器增删、连接/断开，工具自动进白名单
    - 技能包 SKILL.md 的新建/编辑/导入(zip)/导出(zip)/删除
    - 高危动作审批卡片（允许 / 本会话允许同类 / 拒绝）
    - 本地模型 / 硅基流动 / 自定义 OpenAI 兼容端点切换
    - 会话历史保存与回看
"""
import json
import os
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog,
                               QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QTabWidget, QTextEdit, QVBoxLayout,
                               QWidget)

from core.assistant_client import AssistantApi, AssistantStream
from core.task_runner import Worker
from ui.widgets.assistant_cards import (ApprovalCard, ChatBubble, StepLine,
                                       ThinkingCard, ToolCard)
from ui.widgets.auto_scroll_area import AutoHideScrollArea

POLICY_ITEMS = [
    ("readonly_auto", "只读自动", "只读类工具直接执行，写入/高危仍逐次确认（推荐）"),
    ("ask_all", "全部确认", "任何工具调用都要你确认后才执行"),
    ("yolo", "全部放行", "所有工具直接执行、不再询问（危险）"),
]
RISK_TEXT = {"low": "只读", "medium": "写入", "high": "高危"}


class AssistantPage(QWidget):
    """智能助理主页面。"""

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
        self._thinking: Optional[ThinkingCard] = None
        self._tool_cards: List[ToolCard] = []
        self._approvals: Dict[str, ApprovalCard] = {}
        self._busy = False
        self._loading = False

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
        root.setContentsMargins(16, 14, 16, 12)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_chat_panel())
        split.addWidget(self._build_sidebar())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([740, 320])
        root.addWidget(split, 1)

    def _build_header(self) -> QHBoxLayout:
        """顶部只留标题与「新会话」；模型/审批/工作区统一下沉到输入框（参考 WorkBuddy）。"""
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

        self._new_btn = QPushButton("新会话")
        self._new_btn.setObjectName("GhostBtn")
        self._new_btn.setToolTip("开一个全新的会话（当前会话会自动保存在侧栏）")
        self._new_btn.setCursor(Qt.PointingHandCursor)
        self._new_btn.clicked.connect(lambda: self._new_session())
        head.addWidget(self._new_btn)
        return head

    # ---------------- 左侧对话区 ----------------
    def _build_chat_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self._scroll = AutoHideScrollArea()
        self._scroll.setObjectName("ChatScroll")
        self._container = QWidget()
        self._msg_layout = QVBoxLayout(self._container)
        self._msg_layout.setContentsMargins(2, 2, 8, 2)
        self._msg_layout.setSpacing(6)
        self._msg_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        lay.addWidget(self._scroll, 1)

        self._status = QLabel("")
        self._status.setObjectName("PageSubtitle")
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        lay.addWidget(self._build_composer())
        return panel

    def _build_composer(self) -> QWidget:
        """
        底部输入区：上方是输入框，下方一排 pill 控件
        （模型来源 / 模型 / 密钥 / 审批 / 工作区 + 停止 + 发送），
        对齐 WorkBuddy 的「选择项贴着对话框」的交互。
        """
        box = QFrame()
        box.setObjectName("Composer")
        box.setProperty("focus", False)
        self._composer = box
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 10, 12, 8)
        lay.setSpacing(6)

        self._input = QTextEdit()
        self._input.setObjectName("ComposerInput")
        self._input.setFrameShape(QFrame.NoFrame)
        self._input.setPlaceholderText(
            "让助理做点什么：整理工作区的文件 / 读一下这个项目的结构 / 帮我写个脚本并运行 …"
            "（Enter 发送，Shift+Enter 换行）")
        self._input.setFixedHeight(78)
        self._input.installEventFilter(self)
        lay.addWidget(self._input)

        bar = QHBoxLayout()
        bar.setSpacing(6)

        self._provider_combo = QComboBox()
        self._provider_combo.setObjectName("Pill")
        self._provider_combo.setMinimumWidth(100)
        self._provider_combo.setToolTip("模型来源：本地引擎 / 硅基流动 / 自定义 OpenAI 兼容端点")
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        bar.addWidget(self._provider_combo)

        self._model_combo = QComboBox()
        self._model_combo.setObjectName("Pill")
        self._model_combo.setEditable(True)
        self._model_combo.setMinimumWidth(150)
        self._model_combo.setToolTip("模型名可直接输入；本地模型留空 = 使用已加载的第一个模型")
        bar.addWidget(self._model_combo)

        self._key_btn = QPushButton("密钥")
        self._key_btn.setObjectName("Pill")
        self._key_btn.setFixedWidth(50)
        self._key_btn.setCursor(Qt.PointingHandCursor)
        self._key_btn.setToolTip("配置当前提供商的 API Key / Base URL")
        self._key_btn.clicked.connect(self._edit_provider)
        bar.addWidget(self._key_btn)

        self._policy_combo = QComboBox()
        self._policy_combo.setObjectName("Pill")
        self._policy_combo.setMinimumWidth(96)
        for _pid, _label, _tip in POLICY_ITEMS:
            self._policy_combo.addItem(_label, _pid)
            self._policy_combo.setItemData(self._policy_combo.count() - 1,
                                           _tip, Qt.ToolTipRole)
        self._policy_combo.setToolTip("高危动作审批策略")
        self._policy_combo.currentIndexChanged.connect(self._on_policy_changed)
        bar.addWidget(self._policy_combo)

        self._ws_btn = QPushButton("工作区")
        self._ws_btn.setObjectName("Pill")
        self._ws_btn.setFixedWidth(62)
        self._ws_btn.setCursor(Qt.PointingHandCursor)
        self._ws_btn.setToolTip("切换文件工具可访问的工作区")
        self._ws_btn.clicked.connect(self._choose_workspace)
        bar.addWidget(self._ws_btn)

        bar.addStretch(1)

        self._stop_btn = QPushButton("停止")
        self._stop_btn.setObjectName("Pill")
        self._stop_btn.setFixedWidth(54)
        self._stop_btn.setCursor(Qt.PointingHandCursor)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)
        self._stop_btn.clicked.connect(self._stop)
        bar.addWidget(self._stop_btn)

        self._send_btn = QPushButton("发送")
        self._send_btn.setObjectName("SendBtn")
        self._send_btn.setFixedWidth(72)
        self._send_btn.setCursor(Qt.PointingHandCursor)
        self._send_btn.clicked.connect(self._on_send_clicked)
        bar.addWidget(self._send_btn)

        lay.addLayout(bar)
        return box

    # ---------------- 右侧侧栏 ----------------
    def _build_sidebar(self) -> QWidget:
        side = QWidget()
        side.setObjectName("AssistantSideBar")
        side.setMinimumWidth(300)
        side.setMaximumWidth(420)
        lay = QVBoxLayout(side)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(6)

        self._tabs = QTabWidget()
        lay.addWidget(self._tabs, 1)

        # 会话
        self._sess_tab = QWidget()
        sl = QVBoxLayout(self._sess_tab)
        sl.setContentsMargins(0, 6, 0, 0)
        self._sess_list = QListWidget()
        self._sess_list.itemDoubleClicked.connect(self._load_session_item)
        sl.addWidget(self._sess_list, 1)
        srow = QHBoxLayout()
        s_load = QPushButton("打开")
        s_load.clicked.connect(lambda: self._load_session_item(self._sess_list.currentItem()))
        s_del = QPushButton("删除")
        s_del.clicked.connect(self._delete_session)
        srow.addWidget(s_load)
        srow.addWidget(s_del)
        srow.addStretch(1)
        sl.addLayout(srow)
        self._tabs.addTab(self._sess_tab, "会话")

        # 技能
        self._skill_tab = QWidget()
        kl = QVBoxLayout(self._skill_tab)
        kl.setContentsMargins(0, 6, 0, 0)
        self._skill_list = QListWidget()
        self._skill_list.itemDoubleClicked.connect(self._edit_skill_item)
        kl.addWidget(self._skill_list, 1)
        krow1 = QHBoxLayout()
        b_new = QPushButton("新建")
        b_new.clicked.connect(self._new_skill)
        b_edit = QPushButton("编辑")
        b_edit.clicked.connect(lambda: self._edit_skill_item(self._skill_list.currentItem()))
        b_del = QPushButton("删除")
        b_del.clicked.connect(self._delete_skill)
        for b in (b_new, b_edit, b_del):
            krow1.addWidget(b)
        kl.addLayout(krow1)
        krow2 = QHBoxLayout()
        b_import = QPushButton("导入 zip")
        b_import.clicked.connect(self._import_skill)
        b_export = QPushButton("导出 zip")
        b_export.clicked.connect(self._export_skill)
        krow2.addWidget(b_import)
        krow2.addWidget(b_export)
        kl.addLayout(krow2)
        self._tabs.addTab(self._skill_tab, "技能")

        # 工具
        self._tool_tab = QWidget()
        tl = QVBoxLayout(self._tool_tab)
        tl.setContentsMargins(0, 6, 0, 0)
        self._tool_list = QListWidget()
        tl.addWidget(self._tool_list, 1)
        tl.addWidget(self._hint("只读工具自动执行；写入/高危按审批策略处理"))
        self._tabs.addTab(self._tool_tab, "工具")

        # MCP
        self._mcp_tab = QWidget()
        ml = QVBoxLayout(self._mcp_tab)
        ml.setContentsMargins(0, 6, 0, 0)
        self._mcp_list = QListWidget()
        ml.addWidget(self._mcp_list, 1)
        mrow = QHBoxLayout()
        b_add = QPushButton("添加")
        b_add.clicked.connect(self._add_mcp)
        b_conn = QPushButton("连接/断开")
        b_conn.clicked.connect(self._toggle_mcp)
        b_rm = QPushButton("删除")
        b_rm.clicked.connect(self._delete_mcp)
        for b in (b_add, b_conn, b_rm):
            mrow.addWidget(b)
        ml.addLayout(mrow)
        self._tabs.addTab(self._mcp_tab, "MCP")

        # 审计
        self._audit_tab = QWidget()
        al = QVBoxLayout(self._audit_tab)
        al.setContentsMargins(0, 6, 0, 0)
        self._audit_view = QPlainTextEdit()
        self._audit_view.setReadOnly(True)
        al.addWidget(self._audit_view, 1)
        arow = QHBoxLayout()
        b_ref = QPushButton("刷新审计")
        b_ref.clicked.connect(self._refresh_audit)
        arow.addWidget(b_ref)
        arow.addStretch(1)
        al.addLayout(arow)
        self._tabs.addTab(self._audit_tab, "审计")

        return side

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("PageSubtitle")
        lbl.setWordWrap(True)
        return lbl

    # ------------------------------------------------------------------ #
    # 刷新：状态 / 提供商 / 技能 / 工具 / MCP / 会话
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
        self._status.setText(f"后端未就绪：{msg}（请先在控制台启动服务）")

    def _apply_state(self, st: Dict[str, Any]) -> None:
        self._loading = False
        if not isinstance(st, dict):
            return
        self._state = st
        ws = str(st.get("workspace") or "")
        short = ws if len(ws) <= 44 else "…" + ws[-43:]
        orch = "LangGraph 编排" if st.get("orchestrator") == "langgraph" else "顺序降级编排"
        self._subtitle.setText(f"{orch}   ·   工作区 {short or '（未设置）'}")
        self._ws_btn.setToolTip(f"文件工具可访问的工作区：\n{ws or '（未设置）'}\n（点击切换）")

        # 提供商
        cur_pid = st.get("active_provider")
        self._provider_combo.blockSignals(True)
        self._provider_combo.clear()
        for p in st.get("providers") or []:
            if p.get("enabled", True):
                self._provider_combo.addItem(p.get("label") or p["id"], p["id"])
        idx = self._provider_combo.findData(cur_pid)
        if idx >= 0:
            self._provider_combo.setCurrentIndex(idx)
        self._provider_combo.blockSignals(False)
        self._fill_models(cur_pid)

        # 审批策略
        pid = st.get("policy")
        i = self._policy_combo.findData(pid)
        if i >= 0:
            self._policy_combo.blockSignals(True)
            self._policy_combo.setCurrentIndex(i)
            self._policy_combo.blockSignals(False)

        self._fill_skills(st.get("skills") or [])
        self._fill_tools(st.get("tools") or [])
        self._fill_mcp(st.get("mcp") or [])
        self._fill_sessions()

        # 首次进入自动建一个会话，减少「空白不知道点哪」
        if not self._session_id:
            self._new_session(silent=True)

    # ---------------- 模型 ----------------
    def _fill_models(self, provider_id: str) -> None:
        """模型清单（本地引擎走后台请求，避免阻塞 UI）。"""
        provider = next((p for p in (self._state.get("providers") or [])
                         if p.get("id") == provider_id), {})
        self._key_btn.setEnabled(provider_id != "local")
        # 硅基流动：首次进入时把对话页已存的 Key 同步到助理配置，免二次输入
        if provider_id == "siliconflow" and not (provider.get("api_key") or ""):
            key = str(self._settings.value("chat/cloud_api_key", "") or "").strip()
            if key:
                self._save_provider_patch("siliconflow", {"api_key": key})
        current = self._model_combo.currentText().strip()
        saved = current or provider.get("model") or ""

        def collect() -> List[str]:
            if provider_id == "local":
                try:
                    return [m.get("id") for m in self.api.list_models()]
                except Exception:
                    return []
            try:
                items = json.loads(self._settings.value("chat/cloud_models", "") or "[]")
            except Exception:
                return []
            return [it["name"] for it in items
                    if isinstance(it, dict) and it.get("name")]

        w = Worker(collect)
        w.result.connect(lambda names: self._apply_models(names or [], saved))
        w.start()

    def _apply_models(self, names: List[str], saved: str) -> None:
        self._model_combo.blockSignals(True)
        self._model_combo.clear()
        for n in names:
            if n:
                self._model_combo.addItem(n)
        self._model_combo.setEditText(saved)
        self._model_combo.blockSignals(False)

    # ---------------- 侧栏填充 ----------------
    def _fill_skills(self, skills: List[Dict[str, Any]]) -> None:
        self._skill_list.clear()
        for s in skills:
            item = QListWidgetItem(f"{s.get('name')}   ·  {len(s.get('tools') or [])} 工具")
            item.setData(Qt.UserRole, s.get("slug"))
            item.setToolTip(s.get("description") or "")
            self._skill_list.addItem(item)
        if not skills:
            self._skill_list.addItem("（暂无技能，点「新建」或「导入 zip」）")

    def _fill_tools(self, tools: List[Dict[str, Any]]) -> None:
        self._tool_list.clear()
        for t in tools:
            risk = RISK_TEXT.get(t.get("risk"), t.get("risk"))
            src = t.get("source") or "builtin"
            item = QListWidgetItem(f"[{risk}] {t.get('name')}   · {src}")
            item.setToolTip(t.get("description") or "")
            self._tool_list.addItem(item)

    def _fill_mcp(self, servers: List[Dict[str, Any]]) -> None:
        self._mcp_list.clear()
        for s in servers:
            mark = {"connected": "●", "error": "×"}.get(s.get("status"), "○")
            item = QListWidgetItem(
                f"{mark} {s.get('name')}  · {s.get('transport')} · "
                f"{s.get('tool_count', 0)} 工具")
            item.setData(Qt.UserRole, s.get("id"))
            if s.get("error"):
                item.setToolTip(s["error"])
            self._mcp_list.addItem(item)
        if not servers:
            self._mcp_list.addItem("（未配置 MCP 服务器）")

    def _fill_sessions(self) -> None:
        w = Worker(self._assist.sessions)
        w.result.connect(self._apply_sessions)
        w.start()

    def _apply_sessions(self, items) -> None:
        self._sess_list.clear()
        for s in (items or []):
            item = QListWidgetItem(f"{s.get('title') or '新会话'}   "
                                   f"（{s.get('message_count', 0)} 条）")
            item.setData(Qt.UserRole, s.get("id"))
            if s.get("id") == self._session_id:
                item.setSelected(True)
            self._sess_list.addItem(item)
        if not items:
            self._sess_list.addItem("（还没有会话）")

    # ------------------------------------------------------------------ #
    # 会话
    # ------------------------------------------------------------------ #
    def _new_session(self, silent: bool = False) -> None:
        w = Worker(self._assist.new_session)
        w.result.connect(lambda s: self._on_session_created(s, silent))
        w.error.connect(lambda m: self._status.setText(f"新建会话失败：{m}"))
        w.start()

    def _on_session_created(self, session: Dict[str, Any], silent: bool) -> None:
        if not isinstance(session, dict) or not session.get("id"):
            return
        self._session_id = session["id"]
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
        w.error.connect(lambda m: self._status.setText(f"加载会话失败：{m}"))
        w.start()

    def _apply_session_detail(self, data: Dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self._session_id = data.get("id") or ""
        self._clear_chat()
        for m in data.get("messages") or []:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                bubble = ChatBubble(role, content)
                self._append_widget(bubble)
        self._add_step("已载入历史会话")

    def _delete_session(self) -> None:
        item = self._sess_list.currentItem()
        sid = item.data(Qt.UserRole) if item else None
        if not sid:
            return
        if QMessageBox.question(self, "删除会话", "确定删除这个会话记录？") \
                != QMessageBox.StandardButton.Yes:
            return
        w = Worker(self._assist.delete_session, sid)
        w.result.connect(lambda _r: self._fill_sessions())
        w.start()
        if sid == self._session_id:
            self._session_id = ""
            self._clear_chat()
            self._new_session(silent=True)

    # ------------------------------------------------------------------ #
    # 对话
    # ------------------------------------------------------------------ #
    def eventFilter(self, obj, event):  # noqa: N802
        from PySide6.QtCore import QEvent
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
            self._status.setText("会话创建失败，请检查后端服务")
            return
        self._session_id = session["id"]
        self._input.clear()
        self._add_bubble("user", text)
        self._begin_streaming()

        payload = {
            "session_id": self._session_id,
            "message": text,
            "provider": self._provider_combo.currentData() or "",
            "model": self._model_combo.currentText().strip(),
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
        self._status.setText("助理正在工作…")
        self._bubble = None
        self._thinking = None
        self._tool_cards = []

    def _end_streaming(self) -> None:
        self._busy = False
        self._stop_btn.setEnabled(False)
        self._stop_btn.setVisible(False)
        self._send_btn.setVisible(True)
        self._send_btn.setEnabled(True)
        if self._thinking is not None:
            self._thinking.finish()

    def _stop(self) -> None:
        if self._stream is not None:
            self._stream.cancel()
        sid = self._session_id
        if sid:
            Worker(lambda s=sid: self._assist.stop(s)).start()
        self._add_step("已请求停止")
        self._end_streaming()
        self._status.setText("已停止")

    def _on_stream_failed(self, msg: str) -> None:
        self._add_step(f"连接失败：{msg}")
        self._status.setText("请求失败，请确认后端已启动")

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
            self._status.setText(f"模型：{ev.get('model')}")
        elif kind == "delta":
            self._ensure_bubble().append_text(ev.get("text") or "")
            self._scroll_bottom()
        elif kind == "reasoning":
            if self._thinking is None:
                self._thinking = ThinkingCard()
                self._append_widget(self._thinking)
            self._thinking.append(ev.get("text") or "")
            self._scroll_bottom()
        elif kind == "replace":
            if self._bubble is not None:
                self._bubble.set_text(ev.get("text") or "", markdown=True)
        elif kind == "tool_call":
            self._finish_bubble()
            card = ToolCard(ev.get("name") or "", ev.get("summary") or "",
                            ev.get("risk") or "low", ev.get("args") or {})
            self._tool_cards.append(card)
            self._append_widget(card)
            self._scroll_bottom()
        elif kind == "tool_result":
            for card in reversed(self._tool_cards):
                if card._name == ev.get("name") and card._result is None:
                    card.set_result(bool(ev.get("ok")), ev.get("content") or "")
                    break
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
            self._status.setText("完成")
            self._scroll_bottom()
        elif kind == "error":
            self._add_step(f"出错了：{ev.get('message')}", error=True)
            self._status.setText("出错，详见对话区")
        elif kind == "stopped":
            self._add_step("已停止生成")
        elif kind == "end":
            self._end_streaming()
            if not ev.get("error"):
                self._status.setText("就绪")
            self._approvals.clear()

    def _resolve_approval(self, aid: str, approve: bool, remember: bool) -> None:
        w = Worker(self._assist.resolve_approval, aid, approve, remember)
        w.error.connect(lambda m: self._status.setText(f"审批提交失败：{m}"))
        w.start()

    # ---------------- 消息渲染 ----------------
    def _ensure_bubble(self) -> ChatBubble:
        if self._bubble is None:
            self._bubble = ChatBubble("assistant", "")
            self._append_widget(self._bubble)
        return self._bubble

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

    def _scroll_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------------ #
    # 提供商 / 策略 / 工作区
    # ------------------------------------------------------------------ #
    def _on_provider_changed(self, _idx: int) -> None:
        pid = self._provider_combo.currentData()
        if not pid:
            return
        self._fill_models(pid)
        w = Worker(self._assist.save_settings, {"active_provider": pid})
        w.result.connect(lambda _r: None)
        w.start()

    def _save_provider_patch(self, pid: str, patch: Dict[str, Any]) -> None:
        """更新某个提供商的字段（key / base_url / model）并回写整份 providers。"""
        providers = [dict(p) for p in (self._state.get("providers") or [])]
        target = next((p for p in providers if p.get("id") == pid), None)
        if target is None:
            return
        target.update(patch)
        for p in providers:
            if p.get("id") == "local":
                p["model"] = ""
            elif not p.get("model"):
                p["model"] = "Qwen/Qwen3-8B" if p["id"] == "siliconflow" else ""
        w = Worker(self._assist.save_settings, {"providers": providers})
        w.result.connect(lambda _r: self.refresh())
        w.error.connect(lambda m: self._status.setText(f"保存失败：{m}"))
        w.start()

    def _edit_provider(self) -> None:
        pid = self._provider_combo.currentData()
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

    def _on_policy_changed(self, _idx: int) -> None:
        pid = self._policy_combo.currentData()
        if not pid:
            return
        w = Worker(self._assist.save_settings, {"policy": pid})
        w.start()
        self._status.setText(f"审批策略已更新：{self._policy_combo.currentText()}")

    def _choose_workspace(self) -> None:
        start = self._state.get("workspace") or os.path.expanduser("~")
        path = QFileDialog.getExistingDirectory(self, "选择工作区", start)
        if not path:
            return
        w = Worker(self._assist.save_settings, {"workspace": path})
        w.result.connect(lambda _r: self.refresh())
        w.start()

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
                                     self._status.setText(f"技能已保存：{slug}")))
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
        path, _ = QFileDialog.getOpenFileName(self, "导入技能包", "", "技能包 (*.zip)")
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
        path, _ = QFileDialog.getSaveFileName(self, "导出技能包",
                                              f"{slug}.zip", "技能包 (*.zip)")
        if not path:
            return

        def do_export():
            data = self._assist.export_skill(slug)
            with open(path, "wb") as f:
                f.write(data)
            return path

        w = Worker(do_export)
        w.result.connect(lambda p: self._status.setText(f"已导出：{p}"))
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
            self._status.setText("MCP 服务器已连接")

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
