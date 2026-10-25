# -*- coding: utf-8 -*-
"""
智能助理对话区控件（ui/widgets/assistant_cards.py）。

    ChatBubble     用户/助手气泡（助手支持 Markdown 渐进渲染）
    ToolCard       工具调用卡片（可展开看参数与结果）
    ApprovalCard   高危动作审批卡片（允许 / 本任务允许 / 拒绝）
    StepLine       技能阶段提示（plan/act/review 的细线文字）
    ThinkingBlock  模型思考过程（WorkBuddy 形态：灰色「深度思考」+ 正文平铺，不折叠）

全部控件只用 objectName + QSS，不写死颜色，浅色主题由 theme.py 覆盖。
"""
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

RISK_LABEL = {"low": "只读", "medium": "写入", "high": "高危"}
RISK_OBJ = {"low": "RiskLow", "medium": "RiskMedium", "high": "RiskHigh"}


class ChatBubble(QWidget):
    """一条消息气泡。role ∈ user / assistant。"""

    #: 气泡最大宽度：长段落到这里换行，短句按内容贴合
    MAX_WIDTH = 660
    #: 估宽时预留的内边距（QSS 里 padding 8~12px + 边框）
    PAD = 30
    #: 流式刷新节流（毫秒）：每个 token 都重排整段文本会闪得厉害
    FLUSH_MS = 40

    def __init__(self, role: str, text: str = "", parent=None):
        super().__init__(parent)
        self.role = role
        self._fit_w = -1
        self._buf = ""
        self._shown = ""
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(0)

        self.label = QLabel(text)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.label.setObjectName("UserBubble" if role == "user" else "BotBubble")
        self.label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        if text:
            self._apply_format(markdown=(role == "assistant"))
            self._fit_width()
        else:
            # 空气泡（流式起点）：先不定宽，等 begin_stream 给一个稳定宽度
            self._buf = ""

        self._timer = QTimer(self)
        self._timer.setInterval(self.FLUSH_MS)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

        if role == "user":
            lay.addStretch(1)
            lay.addWidget(self.label, 0)
        else:
            lay.addWidget(self.label, 1)
            lay.addStretch(0)

    # ------------------------------------------------------------------ #
    # 流式输出
    # ------------------------------------------------------------------ #
    def begin_stream(self, width: int = 0) -> None:
        """开始流式输出：把气泡宽度**固定**下来。

        宽度每来一个 token 就变 → 文字反复重新折行 → 屏幕闪得厉害。
        固定宽度后文字只往下长，结束时再一次性贴合内容。
        """
        w = int(width or 0) or self.MAX_WIDTH
        w = max(260, min(w, self.MAX_WIDTH))
        self._fit_w = w
        self.label.setFixedWidth(w)

    def append_text(self, piece: str) -> None:
        """流式追加：节流刷新，避免每个 token 都触发整段重排。"""
        if not piece:
            return
        self._buf += piece
        if not self._timer.isActive():
            self._flush()          # 首段立即显示，之后按节流窗口合并

    def _flush(self) -> None:
        self._timer.stop()
        if self._buf == self._shown:
            return
        self._shown = self._buf
        self.label.setText(self._shown)

    def end_stream(self) -> None:
        """流式结束：把剩余文本刷出来，并把宽度贴合到实际内容。"""
        self._flush()
        self._fit_width()

    def set_text(self, text: str, markdown: bool = True) -> None:
        self._buf = text or ""
        self._flush()
        self._apply_format(markdown=markdown)
        self._fit_width()

    def _fit_width(self) -> None:
        """按内容估算理想宽度。

        开了 wordWrap 的 QLabel 给不出可用宽度（布局会把它压成很窄一列，
        中文长句被切成四五行），所以这里按字宽自己算一次，换行仍交给
        wordWrap；短句贴合文字，长段落封顶 MAX_WIDTH。
        """
        text = self.label.text() or ""
        fm = self.label.fontMetrics()
        widest = 0
        for line in (text.splitlines() or [""]):
            widest = max(widest, fm.horizontalAdvance(line))
        width = min(max(widest + self.PAD, 84), self.MAX_WIDTH)
        if width != self._fit_w:
            self._fit_w = width
            self.label.setFixedWidth(width)

    def _apply_format(self, markdown: bool) -> None:
        if markdown:
            self.label.setTextFormat(Qt.MarkdownText)
        else:
            self.label.setTextFormat(Qt.PlainText)


class StepLine(QWidget):
    """技能阶段 / 系统提示：居中的一行小字；error=True 时变成醒目的错误条。"""

    def __init__(self, text: str, parent=None, error: bool = False):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        self.label = QLabel(text)
        self.label.setObjectName("ErrorLine" if error else "StepLine")
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignLeft if error else Qt.AlignCenter)
        lay.addWidget(self.label, 1)

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class _Collapsible(QFrame):
    """可折叠卡片基类：标题行点击展开/收起。"""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("ToolCard")
        self._expanded = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.head_label = QLabel(title)
        self.head_label.setObjectName("CardHead")
        self.head_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        head.addWidget(self.head_label, 1)
        self.toggle_btn = QPushButton("展开")
        self.toggle_btn.setObjectName("MiniBtn")
        self.toggle_btn.setFixedWidth(52)
        self.toggle_btn.clicked.connect(self._toggle)
        head.addWidget(self.toggle_btn, 0)
        outer.addLayout(head)

        self.body = QLabel("")
        self.body.setObjectName("CardBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setVisible(False)
        outer.addWidget(self.body)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self.toggle_btn.setText("收起" if self._expanded else "展开")

    def set_body(self, text: str, auto_expand: bool = False) -> None:
        self.body.setText(text or "")
        if auto_expand and not self._expanded:
            self._toggle()


class ToolCard(_Collapsible):
    """一次工具调用：标题 + 参数 + 结果。"""

    def __init__(self, name: str, summary: str, risk: str = "low",
                 args: Optional[Dict[str, Any]] = None, parent=None,
                 index: Optional[int] = None):
        super().__init__(f"工具 · {name}", parent)
        self.setProperty("risk", risk)
        self._name = name
        self._summary = summary
        self._risk = risk
        self._args = args or {}
        self._index = index
        self._result: Optional[tuple] = None
        self._render()

    def _render(self) -> None:
        state = "运行中…" if self._result is None else (
            "完成" if self._result[0] else "失败")
        self.head_label.setText(
            f"工具 · {self._name}   {self._summary}   · {state}")
        detail = []
        if self._args:
            detail.append("参数：\n" + _short_json(self._args))
        if self._result is not None:
            detail.append("结果：\n" + (self._result[1] or "（空）"))
        self.set_body("\n\n".join(detail))

    def set_result(self, ok: bool, content: str) -> None:
        self._result = (bool(ok), (content or "")[:4000])
        self._render()


class ApprovalCard(QFrame):
    """高危动作审批卡片。"""

    resolved = Signal(str, bool, bool)      # approval_id, approve, remember

    def __init__(self, approval_id: str, tool: str, summary: str,
                 risk: str, args: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("ApprovalCard")
        self.approval_id = approval_id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        head = QHBoxLayout()
        title = QLabel("需要你确认这个动作")
        title.setObjectName("ApprovalTitle")
        head.addWidget(title, 1)
        badge = QLabel(RISK_LABEL.get(risk, risk))
        badge.setObjectName(RISK_OBJ.get(risk, "RiskMedium"))
        badge.setProperty("badge", True)
        head.addWidget(badge, 0)
        lay.addLayout(head)

        desc = QLabel(f"{tool}  ·  {summary}")
        desc.setObjectName("CardBody")
        desc.setWordWrap(True)
        lay.addWidget(desc)

        if args:
            detail = QLabel(_short_json(args))
            detail.setObjectName("CardBody")
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(detail)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_allow = QPushButton("允许")
        self.btn_allow.setObjectName("PrimaryBtn")
        self.btn_allow.clicked.connect(lambda: self._done(True, False))
        self.btn_allow_once = QPushButton("本任务允许同类")
        self.btn_allow_once.clicked.connect(lambda: self._done(True, True))
        self.btn_deny = QPushButton("拒绝")
        self.btn_deny.clicked.connect(lambda: self._done(False, False))
        for b in (self.btn_deny, self.btn_allow_once, self.btn_allow):
            row.addWidget(b, 0)
        lay.addLayout(row)

    def _done(self, approve: bool, remember: bool) -> None:
        for b in (self.btn_allow, self.btn_allow_once, self.btn_deny):
            b.setEnabled(False)
        note = QLabel("已允许" if approve else "已拒绝")
        note.setObjectName("CardBody")
        self.layout().addWidget(note)
        self.resolved.emit(self.approval_id, approve, remember)

    def mark_closed(self, text: str = "该审批已失效") -> None:
        for b in (self.btn_allow, self.btn_allow_once, self.btn_deny):
            b.setEnabled(False)
        note = QLabel(text)
        note.setObjectName("CardBody")
        self.layout().addWidget(note)


class ThinkingBlock(QWidget):
    """模型思考过程（WorkBuddy 形态）。

    与旧版「思考过程（可折叠）」的差别：
        - **不折叠**：灰色小标题「深度思考」+ 灰色正文一直平铺可见
        - **流式节流**：正文按 60ms 窗口合并刷新，不再每个 token 重排整段
    """

    FLUSH_MS = 60

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ThinkingBlock")
        self._buf = ""
        self._shown = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(2)

        self.head = QLabel("深度思考")
        self.head.setObjectName("ThinkingHead")
        lay.addWidget(self.head)

        self.body = QLabel("")
        self.body.setObjectName("ThinkingBody")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        lay.addWidget(self.body)

        self._timer = QTimer(self)
        self._timer.setInterval(self.FLUSH_MS)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._flush)

    def append(self, piece: str) -> None:
        if not piece:
            return
        self._buf += piece
        if not self._timer.isActive():
            self._flush()          # 首段立即出现，之后按节流窗口合并

    def _flush(self) -> None:
        self._timer.stop()
        if self._buf == self._shown:
            return
        self._shown = self._buf
        self.body.setText(self._shown)

    def finish(self) -> None:
        """收尾：把剩余内容刷出来；一个字都没有就整块隐藏。"""
        self._flush()
        if not self._buf.strip():
            self.setVisible(False)
            return
        self.head.setText(f"深度思考（{len(self._buf)} 字）")


def _short_json(obj: Any, limit: int = 400) -> str:
    import json
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = str(obj)
    if len(text) <= limit:
        return text
    return text[:limit] + " …"
