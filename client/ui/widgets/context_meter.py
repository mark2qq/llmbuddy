# -*- coding: utf-8 -*-
"""
输入框下方信息行的「上下文占用」指示（context_meter.py）。

位置演变（都来自用户截图反馈）：
    · 1.0.10 及以前：输入框下面单独一行小字「上下文 6% · 453/8.2k」。
    · 1.0.11：挪到输入条右侧、**只留图标**（底栏宽度有门禁，多 20px 就顶爆）。
    · 1.0.12：按用户要求「上下文放到对话框下方」，搬回输入框下方信息行的
      左端；这一行可用宽 ~940，不受底栏门禁约束，所以圆环右边由页面补一个
      文字标签（`AssistantPage._ctx_meta`，如「上下文 33% · 2.6k/8.0k」）。

实现取舍：
    · **控件本身仍然只有圆环**：文字由页面的 QLabel 显示（可单独换字号/主题色，
      测试也能直接断言文案），圆环只负责「一眼看出占比」。
    · **极小比例也看得见**：弧长下限 8%，否则 6% 只剩一个像素点。
    · **无数据不占位**：还没拿到 usage 时 `clear()` → `setVisible(False)`。
    · **颜色**：常态取 QSS 的 `color`（palette WindowText，深/浅色主题自动跟随），
      ≥70% 转琥珀色告警。
"""
from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

#: 圆环直径 / 线宽（px）——图标总宽 = RING_D + SIDE*2
RING_D = 13
RING_W = 2
SIDE = 3
#: ≥ 这个比例转告警色
HOT_RATIO = 0.7
#: 告警色（深/浅底都够醒目）
HOT_COLOR = QColor("#f0b862")
#: 弧长下限：太小的时候也留一小段可见（否则「有占用」和「没占用」看不出区别）
MIN_ARC = 0.08


class ContextMeter(QWidget):
    """上下文占用小圆环（输入框下方信息行左端，右边跟文字标签）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CtxMeter")
        self.setFixedSize(self.sizeHint())
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._pct: Optional[int] = None
        self._used = 0
        self._limit = 0
        self._detail = ""
        self.setVisible(False)      # 没数据就不占信息行宽度

    # ------------------------------------------------------------------ #
    # 对外状态（页面 / 测试读取）
    # ------------------------------------------------------------------ #
    @property
    def pct(self) -> Optional[int]:
        return self._pct

    @property
    def used(self) -> int:
        return self._used

    @property
    def limit(self) -> int:
        return self._limit

    def is_hot(self) -> bool:
        return self._pct is not None and self._pct >= int(HOT_RATIO * 100)

    def text(self) -> str:
        """无障碍/测试用的文字版（与 tooltip 首行同源）。"""
        return self._head()

    def set_usage(self, used: int, limit: int, detail: str = "") -> None:
        """更新占用。limit<=0 视为「无数据」→ 直接隐藏。"""
        try:
            used = int(used or 0)
            limit = int(limit or 0)
        except (TypeError, ValueError):
            used = limit = 0
        if limit <= 0:
            self.clear()
            return
        self._used, self._limit = used, limit
        self._pct = max(0, min(100, int(round(used * 100.0 / limit))))
        self._detail = detail
        self.setToolTip(self._tooltip())
        self.setVisible(True)
        self.update()

    def clear(self) -> None:
        self._pct = None
        self._used = self._limit = 0
        self._detail = ""
        self.setToolTip("")
        self.setVisible(False)
        self.update()

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def _color(self) -> QColor:
        if self.is_hot():
            return QColor(HOT_COLOR)
        # QSS 的 `color` 会写进 palette，深色/浅色主题都自动跟随
        c = QColor(self.palette().color(self.foregroundRole()))
        return c if c.isValid() else QColor("#8f98ab")

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(RING_D + SIDE * 2, 24)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return self.sizeHint()

    def _head(self) -> str:
        if self._pct is None:
            return ""
        return f"上下文 {self._pct}% · {_fmt(self._used)}/{_fmt(self._limit)}"

    def _tooltip(self) -> str:
        if self._pct is None:
            return ""
        tail = ("\n接近上限时会自动把较早的对话压成摘要，原始消息仍完整保留在"
                "本机任务记录里")
        detail = f"\n{self._detail}" if self._detail else ""
        return self._head() + detail + tail

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        if self._pct is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = self._color()
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        ring = QRectF(cx - RING_D / 2.0, cy - RING_D / 2.0, RING_D, RING_D)

        track = QColor(color)
        track.setAlpha(70)                       # 未占用部分（轨道）
        p.setPen(QPen(track, RING_W))
        p.drawEllipse(ring)

        pen = QPen(color, RING_W)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        ratio = max(MIN_ARC, self._pct / 100.0) if self._pct > 0 else 0.0
        span = -int(360 * 16 * ratio)
        if span:
            p.drawArc(ring, 90 * 16, span)       # 12 点方向顺时针
        p.end()


def _fmt(n: int) -> str:
    """tokens 简写（与 AssistantPage._fmt_tokens 同口径）。"""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)
