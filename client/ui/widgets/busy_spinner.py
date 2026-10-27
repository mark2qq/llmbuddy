# -*- coding: utf-8 -*-
"""运行中旋转指示（busy_spinner.py）。

用户反馈（截图批注「助理转的圈圈放大」）：底栏那个表示「助理正在回复」的
旋转指示原来是一个 ``◐`` 文本字符（`#BusyDot`，13px，占位 16×16），在
200% 缩放 / 深色底上几乎看不见，和旁边的 ✨ / 模型胶囊不成比例。

实现取舍：
    · **自绘圆弧而不是字符**：字符受字体影响，不同字号下粗细、留白都不稳定；
      自绘可以精确控制线宽与转速，放大后也不会糊成一块。
    · **24×24 固定尺寸**（原 16×16）：底栏去掉上下文圆环后省下 ~19px，
      尺寸放大到 24 不吃紧（底栏宽度门禁见 `AssistantPage.BAR_BUDGET_W`）。
    · **转速**：40ms 走 24°≈ 600ms 转一圈，比原来的 4 帧字符（480ms 跳变）
      连贯，也不会快到晃眼。
    · **颜色跟随主题**：取 `foregroundRole` 的调色板色（QSS 的 `color` 会写进
      palette），深色/浅色主题都看得清；轨道固定 40 alpha，避免和圆弧抢眼。
    · **可见性即开关**：`start()` 同时 `setVisible(True)`、`stop()` 隐藏，
      与页面上原来的 `_start_spin/_stop_spin` 语义一一对应。
"""
from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

#: 圆环直径 / 线宽（px）——控件总边长 = RING_D + SIDE*2 = 24
RING_D = 18
RING_W = 2.4
SIDE = 3
#: 每帧角度增量与帧间隔（40ms × 24° → 600ms 一圈）
STEP_DEG = 24
INTERVAL_MS = 40
#: 圆弧张开角度（剩下的开口就是「在转」的视觉线索）
ARC_DEG = 110
#: 轨道（未走过部分）的透明度
TRACK_ALPHA = 40


class BusySpin(QWidget):
    """24×24 的旋转圆弧：助理正在回复时的加载指示。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("BusySpin")
        self.setFixedSize(self.sizeHint())
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setToolTip("助理正在工作")
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self.setVisible(False)          # 空闲不占底栏宽度

    # ------------------------------------------------------------------ #
    # 对外状态
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """开始旋转并显示（幂等：已在转就只保证可见）。"""
        self._angle = 0
        self.setVisible(True)
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop(self) -> None:
        """停止旋转并隐藏。"""
        self._timer.stop()
        self.setVisible(False)

    def is_spinning(self) -> bool:
        return self._timer.isActive()

    @property
    def angle(self) -> int:
        return self._angle

    # ------------------------------------------------------------------ #
    # 绘制
    # ------------------------------------------------------------------ #
    def sizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return QSize(RING_D + SIDE * 2, RING_D + SIDE * 2)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt 命名
        return self.sizeHint()

    def _color(self) -> QColor:
        c = QColor(self.palette().color(self.foregroundRole()))
        return c if c.isValid() else QColor("#aab4cc")

    def _tick(self) -> None:
        self._angle = (self._angle + STEP_DEG) % 360
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt 命名
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = self._color()
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        ring = QRectF(cx - RING_D / 2.0, cy - RING_D / 2.0, RING_D, RING_D)

        track = QColor(color)
        track.setAlpha(TRACK_ALPHA)
        p.setPen(QPen(track, RING_W))
        p.drawEllipse(ring)

        pen = QPen(color, RING_W)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        # 顺时针转：起始角随时间递减，负 span 保证方向是顺时针
        p.drawArc(ring, int((90 - self._angle) * 16), int(-ARC_DEG * 16))
        p.end()
