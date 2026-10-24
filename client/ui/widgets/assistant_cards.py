# -*- coding: utf-8 -*-
"""
智能助理对话区控件（ui/widgets/assistant_cards.py）。

    ChatBubble     用户/助手气泡（助手支持 Markdown 渐进渲染）
    ToolCard       工具调用卡片（可展开看参数与结果）
    ApprovalCard   高危动作审批卡片（允许 / 本会话允许 / 拒绝）
    StepLine       技能阶段提示（plan/act/review 的细线文字）
    ThinkingCard   模型思考过程（可折叠）

全部控件只用 objectName + QSS，不写死颜色，浅色主题由 theme.py 覆盖。
"""
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

RISK_LABEL = {"low": "只读", "medium": "写入", "high": "高危"}
RISK_OBJ = {"low": "RiskLow", "medium": "RiskMedium", "high": "RiskHigh"}


class ChatBubble(QWidget):
    """一条消息气泡。role ∈ user / assistant。"""

    def __init__(self, role: str, text: str = "", parent=None):
        super().__init__(parent)
        self.role = role
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

        if role == "user":
            lay.addStretch(1)
            lay.addWidget(self.label, 0)
        else:
            lay.addWidget(self.label, 1)
            lay.addStretch(0)

    def set_text(self, text: str, markdown: bool = True) -> None:
        self.label.setText(text or "")
        self._apply_format(markdown=markdown)

    def append_text(self, piece: str) -> None:
        """流式追加（期间用纯文本，避免半截 Markdown 抖动）。"""
        self.label.setText(self.label.text() + piece)

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
                 args: Optional[Dict[str, Any]] = None, parent=None):
        super().__init__(f"工具 · {name}", parent)
        self.setProperty("risk", risk)
        self._name = name
        self._summary = summary
        self._risk = risk
        self._args = args or {}
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
        self.btn_allow_once = QPushButton("本会话允许同类")
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


class ThinkingCard(_Collapsible):
    """模型思考过程（默认折叠）。"""

    def __init__(self, parent=None):
        super().__init__("思考过程", parent)
        self._buf = ""

    def append(self, piece: str) -> None:
        self._buf += piece
        self.body.setText(self._buf[-4000:])

    def finish(self) -> None:
        if not self._buf.strip():
            self.setVisible(False)
            return
        self.head_label.setText(f"思考过程（{len(self._buf)} 字，点击展开）")


def _short_json(obj: Any, limit: int = 400) -> str:
    import json
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = str(obj)
    if len(text) <= limit:
        return text
    return text[:limit] + " …"
