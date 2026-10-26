# -*- coding: utf-8 -*-
"""
智能助理对话区控件（ui/widgets/assistant_cards.py）。

    ChatBubble     用户/助手气泡（助手支持 Markdown 渐进渲染）
    ToolCard       工具调用卡片（可展开看参数与结果）
    ApprovalCard   高危动作审批卡片（允许 / 本任务允许 / 拒绝）
    ProcessLine    进程呈现行（WorkBuddy 式：一行聚合摘要 + 可展开明细）
    StepLine       技能阶段提示（plan/act/review 的细线文字）
    AutoLoadPrompt 自动加载提示行：StepLine 文案 + 行尾 MiniBtn（1.0.8，不改 StepLine）
    ThinkingBlock  模型思考过程（WorkBuddy 形态：灰色「深度思考」+ 正文平铺，不折叠）

全部控件只用 objectName + QSS，不写死颜色，浅色主题由 theme.py 覆盖。
"""
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

RISK_LABEL = {"low": "只读", "medium": "写入", "high": "高危"}
RISK_OBJ = {"low": "RiskLow", "medium": "RiskMedium", "high": "RiskHigh"}


class ChatBubble(QWidget):
    """一条消息气泡。role ∈ user / assistant。

    time_text（HH:MM）非空时，气泡下方追加一枚 #BubbleTime 小标签——
    1.0.9 起引导消息专用：进了对话流就带发送时间、可复制、不可撤回
    （与队列内可改形成分界）。
    """

    #: 气泡最大宽度：长段落到这里换行，短句按内容贴合
    MAX_WIDTH = 660
    #: 估宽时预留的内边距（QSS 里 padding 8~12px + 边框）
    PAD = 30
    #: 字宽估算余量：中文/emoji 混排时按字宽累加会偏小 1~2 个字，
    #: 不留余量就会出现「明明放得下却折了最后一个字」（用户反馈截图：
    #: 「继续完成任务」被折成「继续完成任 / 务」）
    SLACK = 1.08
    #: 流式刷新节流（毫秒）：每个 token 都重排整段文本会闪得厉害
    FLUSH_MS = 40

    def __init__(self, role: str, text: str = "", parent=None,
                 time_text: str = ""):
        super().__init__(parent)
        self.role = role
        self.time_text = str(time_text or "")   # HH:MM；仅引导消息传（P1-2）
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

        if self.time_text:
            # 时间标签挂在气泡下方、与气泡同侧对齐（用户右 / 助手左），
            # 走 #BubbleTime QSS；复制文本沿用 label 的 TextSelectableByMouse
            self.time_label = QLabel(self.time_text)
            self.time_label.setObjectName("BubbleTime")
            self.time_label.setAlignment(
                Qt.AlignRight if role == "user" else Qt.AlignLeft)
            col = QVBoxLayout()
            col.setSpacing(0)
            col.setContentsMargins(0, 0, 0, 0)
            col.addWidget(self.label, 0)
            col.addWidget(self.time_label, 0)
            if role == "user":
                lay.addStretch(1)
                lay.addLayout(col, 0)
            else:
                lay.addLayout(col, 1)
                lay.addStretch(0)
        elif role == "user":
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
        w = max(260, min(w, self._avail_width()))
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

    def clear(self) -> None:
        """清空内容（后端自动放大 token 预算重试时用，避免半截文字留在屏上）。"""
        self._buf = ""
        self._shown = ""
        self._timer.stop()
        self.label.setText("")
        self.label.setFixedWidth(max(120, min(self.MAX_WIDTH, self._avail_width())))

    def _avail_width(self) -> int:
        """内容区可用宽度（父容器宽度），封顶 MAX_WIDTH。

        用户要求「按界面内容区宽度计算，超过才换行」：气泡宽度上限不能是
        写死的常量，否则宽窗口下短句仍被折行。父容器即对话列（`_col`，
        上限 940），这里取其宽度作为可用宽度，留 8% 给滚动条与边距。
        """
        p = self.parentWidget()
        w = 0
        while p is not None:
            cw = p.width()
            if cw > 0:
                w = cw
                break
            p = p.parentWidget()
        if w <= 0:
            return self.MAX_WIDTH
        return max(220, min(self.MAX_WIDTH, int(w * 0.92)))

    def _fit_width(self) -> None:
        """按内容估算理想宽度。

        开了 wordWrap 的 QLabel 给不出可用宽度（布局会把它压成很窄一列，
        中文长句被切成四五行），所以这里按字宽自己算一次，换行仍交给
        wordWrap；短句贴合文字，长段落封顶「内容区可用宽度」。
        """
        text = self.label.text() or ""
        fm = self.label.fontMetrics()
        widest = 0
        for line in (text.splitlines() or [""]):
            widest = max(widest, fm.horizontalAdvance(line))
        need = int(widest * self.SLACK) + self.PAD
        width = min(max(need, 84), self._avail_width())
        if width != self._fit_w:
            self._fit_w = width
            self.label.setFixedWidth(width)

    def _apply_format(self, markdown: bool) -> None:
        if markdown:
            self.label.setTextFormat(Qt.MarkdownText)
        else:
            self.label.setTextFormat(Qt.PlainText)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """首次显示时重算宽度。

        气泡常在「还没挂进对话列」时就构造好（此时拿不到父容器宽度，
        只能按 MAX_WIDTH 兜底）；挂上去之后必须按真实内容区宽度再算一次，
        否则窄窗口里会横向溢出、宽窗口里又提前折行。
        """
        super().showEvent(event)
        if self._buf or self.label.text():
            self._fit_width()


class StepLine(QWidget):
    """技能阶段 / 系统提示：**左对齐**的一行小字；error=True 时变成醒目的错误条。

    1.0.9 变更（用户反馈截图）：原实现居中（`AlignCenter`），长句在内容列中间
    折成两三行、两侧留白，读起来像一段漂浮的文字。统一改左对齐 + wordWrap，
    折行宽度由内容列宽度决定（`QHBoxLayout` 拉伸 + `setWordWrap`）。
    """

    def __init__(self, text: str, parent=None, error: bool = False):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        self.label = QLabel(text)
        self.label.setObjectName("ErrorLine" if error else "StepLine")
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        # 长句必须按可用宽度折行：默认 Preferred 会让 QLabel 在布局里被压成
        # 一窄条，改 Ignored 让它吃满内容列宽度、同时不把最小宽度顶出去
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
        lay.addWidget(self.label, 1)

    def set_text(self, text: str) -> None:
        self.label.setText(text)


class AutoLoadPrompt(QWidget):
    """自动加载提示行：`StepLine` 文案 + 行尾一个 MiniBtn 动作按钮。

    为什么不改造 `StepLine`：`assistant_cards.py` 的 `StepLine` 只有一个居中的
    QLabel，页面侧 `_add_step()` 有 5 处调用（`skill_step` / `note` / `dequeued` /
    `stopped` / `queued` 等）；加按钮位会破坏居中 QSS 并波及这 5 处签名。
    这里做「组合」而不是「继承改造」：内部原样复用 `StepLine`，按钮复用既有的
    `#MiniBtn` 样式（`core/theme.py` 已覆盖，无需新增选择器）。

    用法见 `AssistantPage._add_step_with_action()`（1.0.8 自动加载提示）。
    """

    def __init__(self, text: str, action_label: str, parent=None,
                 error: bool = False):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(6)
        lay.addStretch(1)
        # 原样复用既有组件：文案样式与所有其它 StepLine 完全一致
        self.step = StepLine(text, error=error)
        lay.addWidget(self.step)
        self.btn = QPushButton(action_label)
        self.btn.setObjectName("MiniBtn")
        self.btn.setFixedHeight(20)
        self.btn.setCursor(Qt.PointingHandCursor)
        lay.addWidget(self.btn)
        lay.addStretch(1)

    def set_text(self, text: str) -> None:
        self.step.set_text(text)


class ElidedLabel(QLabel):
    """单行标签：超宽时按可用宽度省略（`Qt.ElideRight`），全称进 tooltip。

    为什么要这么个类：不折行、不可压缩的 QLabel 会把所在卡片的最小宽度撑到
    文本全宽；卡片一旦比视口宽，对话区就出现横向溢出，被横向滚走后**所有行
    的行首文字**都会被裁掉（用户反馈截图：整列只剩「ob 查找 …」）。
    `Ignored` 策略允许它被压窄，`resizeEvent` 里重新省略，全称仍可看 tooltip。
    """

    def __init__(self, text: str = "", parent=None,
                 mode: Qt.TextElideMode = Qt.ElideRight):
        super().__init__("", parent)
        self.setTextFormat(Qt.PlainText)
        self.setWordWrap(False)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._mode = mode
        self._full = ""
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        """设置全称（显示的可能是省略后的文本，`text()` 返回显示值）。"""
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._refresh()

    def fullText(self) -> str:
        return self._full

    def _refresh(self) -> None:
        fm = self.fontMetrics()
        avail = self.width() - 2
        # 宽度还是布局初始值（0/100）时先用全称顶着，resizeEvent 会重算
        text = self._full if avail <= 8 else fm.elidedText(
            self._full, self._mode, avail)
        if text != super().text():      # 防 setText → relayout → resize 循环
            super().setText(text)

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._refresh()


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
        # 超宽省略（中间省略：保住「工具 · fs_glob」开头和「· 完成」结尾）
        self.head_label = ElidedLabel(title, mode=Qt.ElideMiddle)
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
        # WordWrap 的 QLabel 最小宽度 = 最长「词」宽度，长路径/长 JSON 会顶爆列宽
        self.body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setVisible(False)
        outer.addWidget(self.body)

    # ------------------------------------------------------------------ #
    # 标题：超宽省略（不撑宽卡片）
    # ------------------------------------------------------------------ #
    def set_head(self, text: str) -> None:
        """设置标题全称（内部按当前宽度省略显示，全称进 tooltip）。"""
        self.head_label.setFullText(text)

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
        self.set_head(f"工具 · {self._name}   {self._summary}   · {state}")
        detail = []
        if self._args:
            detail.append("参数：\n" + _short_json(self._args))
        if self._result is not None:
            detail.append("结果：\n" + (self._result[1] or "（空）"))
        self.set_body("\n\n".join(detail))

    def set_result(self, ok: bool, content: str) -> None:
        self._result = (bool(ok), (content or "")[:4000])
        self._render()


class ProcessLine(QWidget):
    """WorkBuddy 式「进程呈现」：一行聚合摘要 + 可展开的步骤明细。

    对应 WorkBuddy 对话流里那种「⊠ 运行命令、查看文件：x.png ›」的紧凑条目：
    **默认只显示一行聚合摘要**（不铺开调用参数与结果），点一下才展开每一步。
    这样「系统在干活」一眼可见，又不会把原始调用细节堆在对话流里。

    用法：页面侧按「一批连续工具调用」建一个 ProcessLine，逐条 `add_step()`，
    出结果时 `set_result()` 回填；`toggle()` 展开/收起。
    """

    MAX_ITEMS = 3          # 摘要里最多列几项，超出折成「等 N 项」

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ProcessLine")
        self._steps = []            # [{"action": str, "ok": Optional[bool]}]

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 1, 0, 1)
        v.setSpacing(1)

        self.head = QToolButton(self)
        self.head.setObjectName("ProcessHead")
        self.head.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.head.setCursor(Qt.PointingHandCursor)
        self.head.setAutoRaise(True)
        self.head.setText("⚡ 正在处理")
        self.head.setToolTip("点击展开这一步的过程")
        self.head.clicked.connect(self.toggle)
        v.addWidget(self.head, 0, Qt.AlignLeft)

        self.body = QWidget(self)
        self.body.setObjectName("ProcessBody")
        self._body_lay = QVBoxLayout(self.body)
        self._body_lay.setContentsMargins(14, 0, 0, 0)
        self._body_lay.setSpacing(1)
        self.body.setVisible(False)
        v.addWidget(self.body, 0)
        self._expanded = False        # 自己记展开态：父控件不可见时 isVisible() 恒 False

    # ---------------- 步骤 ----------------
    def add_step(self, action: str) -> int:
        """追加一步，返回它的序号（页面侧用它回填结果）。"""
        self._steps.append({"action": action, "ok": None})
        self._refresh()
        return len(self._steps) - 1

    def set_result(self, pos: int, ok: bool) -> None:
        if 0 <= pos < len(self._steps):
            self._steps[pos]["ok"] = bool(ok)
        self._refresh()

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.body.setVisible(self._expanded)
        self._refresh()

    @property
    def expanded(self) -> bool:
        return self._expanded

    # ---------------- 渲染 ----------------
    def _refresh(self) -> None:
        acts = [s["action"] for s in self._steps] or ["正在处理"]
        shown = acts[:self.MAX_ITEMS]
        if len(acts) > self.MAX_ITEMS:
            shown.append(f"等 {len(acts)} 项")
        arrow = "▾" if self._expanded else "›"
        self.head.setText(f"⚡ {'、'.join(shown)} {arrow}")
        self.head.setToolTip("点击展开这一步的过程" if not self._expanded
                             else "点击收起")

        while self._body_lay.count():
            item = self._body_lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for s in self._steps:
            mark = "·" if s["ok"] is None else ("✓" if s["ok"] else "✗")
            lbl = QLabel(f"{mark} {s['action']}")
            lbl.setObjectName("ProcessStep")
            lbl.setWordWrap(True)
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
            self._body_lay.addWidget(lbl)


class ApprovalCard(QFrame):
    """高危动作审批卡片。"""

    resolved = Signal(str, bool, bool)      # approval_id, approve, remember
    closed = Signal(str)                     # approval_id：卡片已关闭（确认后自动收起）

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
        self.resolved.emit(self.approval_id, approve, remember)
        # 1.0.9（用户截图）：确认后这张卡片不再留在对话流里 —— 关闭它
        self.close_now()

    def close_now(self) -> None:
        """关闭审批卡：先通知外部摘掉，再从布局里移除并销毁。"""
        self.closed.emit(self.approval_id)
        self.hide()
        self.setParent(None)
        self.deleteLater()

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
    #: 展示上限：思考型模型（R1 / Qwen3 thinking）单轮可吐几万字，QLabel
    #: 对超长富文本的每次重排都是 O(n)，节流也压不住 → 只保留尾部 N 字
    MAX_CHARS = 20000

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
        # Ignored：避免「最长词」（长英文/路径/代码行）把整列最小宽度顶爆
        self.body.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
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
        text = self._shown
        if len(text) > self.MAX_CHARS:
            # 只留尾部（最新思考），避免超长文本拖死布局
            text = "（前文已省略 %d 字）\n…%s" % (
                len(text) - self.MAX_CHARS, text[-self.MAX_CHARS:])
        self.body.setText(text)

    def finish(self) -> None:
        """收尾：把剩余内容刷出来；一个字都没有就整块隐藏。"""
        self._flush()
        if not self._buf.strip():
            self.setVisible(False)
            return
        self.head.setText(f"深度思考（{len(self._buf)} 字）")

    def set_text(self, text: str) -> None:
        """整体替换思考正文（后端把工具标记从思考里摘掉后回填）。"""
        self._timer.stop()
        self._buf = str(text or "")
        self._shown = ""
        self._flush()
        if not self._buf.strip():
            self.setVisible(False)

    def clear(self) -> None:
        """清空（后端自动放大 token 预算重试时，旧思考作废）。"""
        self._timer.stop()
        self._buf = ""
        self._shown = ""
        self.body.setText("")


def _short_json(obj: Any, limit: int = 400) -> str:
    import json
    try:
        text = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        text = str(obj)
    if len(text) <= limit:
        return text
    return text[:limit] + " …"
