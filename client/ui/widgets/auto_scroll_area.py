# -*- coding: utf-8 -*-
"""
自动隐藏滚动条的滚动区（auto_scroll_area.py）。

红字需求：页面右侧那条竖滚动条**默认不显示**，只有窗口高度变化、内容确实
超出视口需要滚动时才短暂出现；不滚动时自动收起，不长期占视觉。

实现要点：
    1. 原生纵向滚动条策略设为 AlwaysOff —— 它不占宽度、也不显示，
       但滚动能力完全保留（滚轮 / 代码 setValue 仍然生效）；
    2. 叠一条自绘的细滚动条（6px）作为 QScrollArea 的子控件浮在右边缘，
       与内部滚动条双向同步；
    3. 出现时机：内容溢出且（窗口尺寸变化 / 滚轮滚动 / 拖动 / 鼠标停在条上），
       停止交互 IDLE_MS 后自动隐藏；
    4. 因为不占用布局宽度，出现/消失都不会让内容左右跳动。
"""
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QFrame, QScrollArea, QScrollBar


class AutoHideScrollArea(QScrollArea):
    """默认不显示滚动条、需要滚动时才浮出细条的滚动区。"""

    #: 停止滚动后多久自动收起（毫秒）
    IDLE_MS = 1200
    #: 悬浮细条宽度（px）
    BAR_WIDTH = 6
    #: 细条距上下边的内缩（px）
    BAR_INSET = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 原生纵向条永久隐藏（不占宽度），滚动能力仍保留
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._syncing = False

        # 内部（隐藏的）纵向滚动条：唯一事实来源
        self._inner = self.verticalScrollBar()
        self._inner.rangeChanged.connect(self._on_range_changed)
        self._inner.valueChanged.connect(self._on_inner_moved)

        # 浮层细滚动条
        self._bar = QScrollBar(Qt.Orientation.Vertical, self)
        self._bar.setObjectName("OverlayScrollBar")
        self._bar.setFixedWidth(self.BAR_WIDTH)
        self._bar.setRange(0, 0)
        self._bar.setCursor(Qt.CursorShape.ArrowCursor)
        self._bar.hide()
        self._bar.valueChanged.connect(self._on_bar_moved)
        self._bar.installEventFilter(self)

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(self.IDLE_MS)
        self._idle.timeout.connect(self._hide_if_idle)

        self._hover = False

    # ------------------------------------------------------------------ #
    # 对外查询（供测试/调试）
    # ------------------------------------------------------------------ #
    def can_scroll(self) -> bool:
        """内容是否超出视口（即是否真的需要滚动条）。"""
        return self._inner.maximum() > 0

    def overlay_visible(self) -> bool:
        """浮层滚动条当前是否可见。"""
        return self._bar.isVisible()

    # ------------------------------------------------------------------ #
    # 内部逻辑
    # ------------------------------------------------------------------ #
    def _sync_bar_range(self) -> None:
        self._syncing = True
        try:
            self._bar.setRange(self._inner.minimum(), self._inner.maximum())
            self._bar.setPageStep(self._inner.pageStep())
            self._bar.setSingleStep(self._inner.singleStep())
            self._bar.setValue(self._inner.value())
        finally:
            self._syncing = False

    def _sync_geometry(self) -> None:
        h = max(0, self.height() - self.BAR_INSET * 2)
        self._bar.setGeometry(
            self.width() - self.BAR_WIDTH - self.BAR_INSET,
            self.BAR_INSET, self.BAR_WIDTH, h)

    def reveal(self) -> None:
        """浮出滚动条并重新计时（内容不溢出时保持隐藏）。"""
        if not self.can_scroll():
            self._hide_now()
            return
        self._sync_bar_range()
        self._sync_geometry()
        self._bar.show()
        self._bar.raise_()
        self._idle.start()

    def _hide_now(self) -> None:
        self._idle.stop()
        self._bar.hide()

    def _hide_if_idle(self) -> None:
        # 鼠标停在滚动条上时不收起，方便拖动
        if self._hover:
            self._idle.start()
            return
        if not self.can_scroll():
            self._hide_now()
            return
        self._bar.hide()

    def _on_range_changed(self, _minimum: int, _maximum: int) -> None:
        # 内容/高度变化：需要滚动就短暂浮出提示，不需要就彻底隐藏
        if self.can_scroll():
            self.reveal()
        else:
            self._hide_now()

    def _on_inner_moved(self, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._bar.setValue(value)
        finally:
            self._syncing = False
        # 只要发生滚动（滚轮/代码/拖动）就浮出细条，滚动停止后再收起
        if self.can_scroll():
            self.reveal()

    def _on_bar_moved(self, value: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            self._inner.setValue(value)
        finally:
            self._syncing = False

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #
    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        if getattr(self, "_bar", None) is None:
            return
        self._sync_geometry()
        if self.can_scroll():
            self._sync_bar_range()
            self.reveal()
        self._bar.raise_()

    def wheelEvent(self, event):  # noqa: N802
        super().wheelEvent(event)
        self.reveal()

    def eventFilter(self, obj, event):  # noqa: N802
        # 构造期 setVerticalScrollBarPolicy 会先触发本回调，此时 _bar 尚未创建
        if obj is getattr(self, "_bar", None):
            if event.type() == QEvent.Type.Enter:
                self._hover = True
                self._idle.stop()
            elif event.type() == QEvent.Type.Leave:
                self._hover = False
                self._idle.start()
        return super().eventFilter(obj, event)
