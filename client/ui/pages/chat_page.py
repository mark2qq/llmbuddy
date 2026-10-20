# -*- coding: utf-8 -*-
"""
对话页面（chat_page.py）。

功能映射：
    - 模型选择       -> 底部输入区下方的下拉按钮（参考 WorkBuddy：显示当前模型 + ⌄），
                       点击弹出选择面板：本地模型·运行中 / 本地模型·已下载 / 线上免费模型，
                       面板底部「⚙ 配置自定义模型」可添加自定义线上模型（QSettings 持久化）
    - 发送对话       -> POST /v1/chat/completions stream:true（M 网关）
    - 快捷键         -> Enter 发送 / Shift+Enter 换行
    - 发送/停止按钮  -> 参考主流智能体：空闲为圆形「↑」发送；生成中变为圆形「■」停止，
                       可随时终止运行；生成结束/出错/终止后恢复发送态
    - 打字机流式     -> 逐块追加到当前助手气泡（SSE data: 解析由 api_client 完成）
    - 多轮上下文     -> 维护 messages 列表，连续追加

说明：流式回调通过 Qt 信号投递回主线程更新 UI，避免跨线程操作控件；
     通过 generation id 隔离「已终止/已过期」的流式回调，防止串气泡或状态错乱。
"""
from typing import Dict, List, Optional

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QTextEdit, QPushButton, QLabel, QScrollArea,
                               QMenu, QMessageBox, QInputDialog, QSizePolicy)
from PySide6.QtCore import Qt, Signal, QSettings, QSize, QPoint, QRect
from PySide6.QtGui import (QPixmap, QPainter, QColor, QPolygon,
                           QIcon)

from core.task_runner import Worker


# 线上免费模型选项（UI 展示用；实际云端路由需后端接入，当前未接通）
CLOUD_FREE_MODELS: List[Dict[str, str]] = [
    {"name": "Qwen/Qwen2.5-7B-Instruct", "tag": "SiliconFlow 免费"},
    {"name": "Qwen/Qwen2.5-Coder-7B-Instruct", "tag": "SiliconFlow 免费"},
    {"name": "deepseek-ai/DeepSeek-V3", "tag": "SiliconFlow"},
    {"name": "meta-llama/Llama-3.1-8B-Instruct", "tag": "SiliconFlow"},
]

# 自定义线上模型的 QSettings 存储键
_CUSTOM_MODELS_KEY = "chat/custom_cloud_models"


def _send_icon() -> QIcon:
    """白色向上箭头（发送）。"""
    pm = QPixmap(22, 22)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#ffffff"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawPolygon(QPolygon([QPoint(11, 3), QPoint(19, 15), QPoint(3, 15)]))
    p.end()
    return QIcon(pm)


def _stop_icon() -> QIcon:
    """白色圆角方块（停止）。"""
    pm = QPixmap(22, 22)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#ffffff"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRect(6, 6, 10, 10), 2, 2)
    p.end()
    return QIcon(pm)


class _ChatInput(QTextEdit):
    """对话输入框：Enter 发送 / Shift+Enter 换行。"""

    send_requested = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
                event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class ChatPage(QWidget):
    """对话页面。"""

    # 流式信号（跨线程安全；chunk 带 generation id 以隔离过期回调）
    chunk_received = Signal(int, str)
    done_received = Signal(dict)

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._messages: List[Dict[str, str]] = []
        self._busy = False
        self._gen_id = 0                  # 当前 generation id（发送时 +1）
        self._cancel_requested = False    # 用户是否请求终止当前生成
        self._current_model: str = ""      # 当前选中的模型
        self._local_models: List[str] = []  # 刷新得到的本地模型（运行+已下载）
        self._local_split: Dict[str, List[str]] = {"running": [], "not_running": []}
        self._refresh_token = 0  # 丢弃乱序/过期的后台刷新结果
        self._assistant_label: Optional[QLabel] = None
        self._assistant_text = ""
        # QSettings 组织/应用名随产品改名（LLMDesktop -> llmBuddy，2026-10-20）；
        # 旧设置仅存「自定义线上模型」一个键，读取时做一次兼容回退，避免用户配置丢失。
        self._settings = QSettings("llmBuddy", "llmBuddy")
        self._legacy_settings = QSettings("LLMDesktop", "LLMDesktop")
        self.chunk_received.connect(self._append_chunk)
        self.done_received.connect(self._on_done)
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        # 消息区
        card = QGroupBox("对话")
        card.setObjectName("Card")
        card.setLayout(QVBoxLayout())
        card.layout().setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setObjectName("ChatScroll")
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._content_layout.setSpacing(10)
        self._scroll.setWidget(self._content)
        card.layout().addWidget(self._scroll)
        root.addWidget(card, 1)

        # 输入区
        bottom = QVBoxLayout()

        # 输入框（Enter 发送 / Shift+Enter 换行）
        self._input = _ChatInput()
        self._input.setObjectName("ChatInput")
        self._input.setPlaceholderText("输入消息，Enter 发送 / Shift+Enter 换行")
        self._input.setMaximumHeight(120)
        self._input.send_requested.connect(self._on_send)
        bottom.addWidget(self._input)

        # 底部操作行：清空对话 | 模型下拉（WorkBuddy 位置：输入框下方） | 发送/停止圆形按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        self._clear_btn = QPushButton("清空对话")
        self._clear_btn.setObjectName("TinyBtn")
        self._clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(self._clear_btn)

        self._model_btn = QPushButton("选择模型  ⌄")
        self._model_btn.setObjectName("ModelDropBtn")
        self._model_btn.setToolTip("选择要对话的模型（本地 / 线上免费）")
        self._model_btn.clicked.connect(self._open_model_menu)
        self._model_menu = QMenu(self)
        self._model_menu.setObjectName("ModelMenu")
        self._model_menu.aboutToShow.connect(self._build_model_menu)
        btn_row.addWidget(self._model_btn)

        btn_row.addStretch(1)

        # 发送 / 停止圆形按钮（参考主流智能体）
        self._send_btn = QPushButton()
        self._send_btn.setObjectName("SendCircleBtn")
        self._send_btn.setFixedSize(40, 40)
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setToolTip("Enter 发送，Shift+Enter 换行")
        self._send_btn.clicked.connect(self._on_send_clicked)
        self._set_send_state(False)
        btn_row.addWidget(self._send_btn)

        bottom.addLayout(btn_row)
        root.addLayout(bottom)

    # ------------------------------------------------------------------ #
    # 发送/停止按钮状态
    # ------------------------------------------------------------------ #
    def _set_send_state(self, busy: bool) -> None:
        """busy=False -> 发送箭头；busy=True -> 停止方块（点击即终止生成）。"""
        self._send_btn.setIcon(_stop_icon() if busy else _send_icon())
        self._send_btn.setIconSize(QSize(20, 20))
        self._send_btn.setToolTip(
            "停止生成" if busy else "Enter 发送，Shift+Enter 换行")

    # ------------------------------------------------------------------ #
    # 模型选择面板（参考 WorkBuddy 模式）
    # ------------------------------------------------------------------ #
    def _custom_cloud_models(self) -> List[str]:
        """读取用户配置的自定义线上模型（QSettings 持久化）。"""
        val = self._settings.value(_CUSTOM_MODELS_KEY, None)
        if val is None:
            # 兼容 2026-10-20 产品改名（LLMDesktop -> llmBuddy）前的旧设置
            val = self._legacy_settings.value(_CUSTOM_MODELS_KEY, None, type=list)
        return [str(v) for v in (val or []) if str(v).strip()]

    def _open_model_menu(self) -> None:
        """弹出模型选择面板（锚定在按钮下方）。"""
        self._build_model_menu()
        pos = self._model_btn.mapToGlobal(
            self._model_btn.rect().bottomLeft())
        self._model_menu.exec(pos)

    def _collect_local_models(self) -> Dict[str, List[str]]:
        """聚合本地模型：运行中 / 已下载未运行。返回 {running, not_running}。"""
        try:
            pool = self.api.pool() or {}
        except Exception:
            pool = {}
        loaded = pool.get("loaded") or []
        running = [e.get("name") for e in loaded if e.get("status") == "ready"]
        loaded_names = [e.get("name") for e in loaded if e.get("name")]
        try:
            dl = self.api.list_models() or []
        except Exception:
            dl = []
        downloaded = [m.get("id") for m in dl if m.get("id")]
        all_local = sorted(set(loaded_names) | set(downloaded))
        not_running = [n for n in all_local if n not in running]
        return {"running": running, "not_running": not_running}

    def _build_model_menu(self, local: Optional[Dict[str, List[str]]] = None) -> None:
        """重建选择面板：本地模型·运行中 / 本地模型·已下载 / 线上免费模型 / 配置自定义模型。

        参数 local 缺省时使用 refresh() 缓存的 _local_split（绝不在此处发起
        网络请求——本函数会被打开菜单/选模型等交互频繁调用，必须瞬时完成）。
        """
        self._model_menu.clear()
        if local is None:
            local = self._local_split
        running = local["running"]
        not_running = local["not_running"]

        if running:
            self._add_menu_header("本地模型 · 运行中")
            for n in running:
                self._add_menu_action(f"●  {n}", n)
        if not_running:
            self._add_menu_header("本地模型 · 已下载")
            for n in not_running:
                self._add_menu_action(f"○  {n}", n)
        if not running and not not_running:
            hint = self._model_menu.addAction("（暂无本地模型，请到模型库下载/加载）")
            hint.setEnabled(False)

        cloud_names = [c["name"] for c in CLOUD_FREE_MODELS]
        customs = [c for c in self._custom_cloud_models() if c not in cloud_names]
        self._model_menu.addSeparator()
        self._add_menu_header("线上免费模型")
        for c in CLOUD_FREE_MODELS:
            label = f"☁  {c['name']}" + (f"  ·  {c.get('tag', '')}" if c.get("tag") else "")
            self._add_menu_action(label, c["name"])
        for name in customs:
            self._add_menu_action(f"☁  {name}  ·  自定义", name)

        self._model_menu.addSeparator()
        cfg = self._model_menu.addAction("⚙  配置自定义模型")
        cfg.triggered.connect(self._on_configure_custom_model)

    def _add_menu_header(self, text: str) -> None:
        act = self._model_menu.addAction(text)
        act.setEnabled(False)

    def _add_menu_action(self, label: str, name: str) -> None:
        act = self._model_menu.addAction(("✓  " if name == self._current_model else "    ") + label)
        act.triggered.connect(
            lambda _checked=False, m=name: self._on_pick_model(m))

    def _on_configure_custom_model(self) -> None:
        """添加自定义线上模型（QSettings 持久化）。"""
        text, accepted = QInputDialog.getText(
            self, "配置自定义模型",
            "输入线上模型名称（如 SiliconFlow 的模型 ID）：")
        if not accepted:
            return
        name = text.strip()
        if not name:
            return
        customs = self._custom_cloud_models()
        if name not in customs:
            customs.append(name)
            self._settings.setValue(_CUSTOM_MODELS_KEY, customs)
        self._build_model_menu()

    def _on_pick_model(self, name: str) -> None:
        """选中某模型，更新当前模型与下拉按钮显示（参考 WorkBuddy：按钮显示选中项）。"""
        self._current_model = name
        self._update_model_btn()
        self._build_model_menu()

    def _update_model_btn(self) -> None:
        prefix = "" if self._current_model in self._local_models else "☁ "
        text = prefix + (self._current_model or "选择模型")
        # 名称可能很长：按按钮最大宽度省略，tooltip 展示完整名
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._model_btn.font())
        self._model_btn.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 170) + "  ⌄")
        self._model_btn.setToolTip(self._current_model or "选择模型")

    # ------------------------------------------------------------------ #
    # 气泡
    # ------------------------------------------------------------------ #
    def _add_bubble(self, text: str, role: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("UserBubble" if role == "user" else "AssistantBubble")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if role == "user":
            label.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        else:
            label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        container = QWidget()
        cl = QHBoxLayout(container)
        cl.setContentsMargins(0, 0, 0, 0)
        if role == "user":
            cl.addStretch(1)
            cl.addWidget(label)
        else:
            cl.addWidget(label, 1)
        self._content_layout.addWidget(container)
        self._apply_bubble_width(label, role)
        self._scroll_to_bottom()
        return label

    def _apply_bubble_width(self, label: QLabel, role: str) -> None:
        """按当前视口宽度约束气泡最大宽度：用户 70%，回复 95%。"""
        w = max(self._scroll.viewport().width(), 320)
        label.setMaximumWidth(int(w * 0.7 if role == "user" else w * 0.95))

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            container = item.widget()
            if container is None:
                continue
            lay = container.layout()
            if lay is None or lay.count() == 0:
                continue
            label = lay.itemAt(1 if lay.itemAt(0).widget() is None else 0)
            if label is None:
                continue
            lbl = label.widget()
            if isinstance(lbl, QLabel):
                role = "user" if lbl.objectName() == "UserBubble" else "assistant"
                self._apply_bubble_width(lbl, role)

    def _scroll_to_bottom(self) -> None:
        sb = self._scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_send_clicked(self) -> None:
        """圆形按钮点击：生成中->终止；空闲->发送。"""
        if self._busy:
            self._on_stop_generation()
        else:
            self._on_send()

    def _on_send(self) -> None:
        if self._busy:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        if not self._current_model:
            QMessageBox.information(self, "未选择模型",
                                    "请先点击下方「选择模型」挑选一个本地模型。")
            return
        if self._current_model not in self._local_models:
            QMessageBox.information(
                self, "线上模型未接入",
                f"当前选择的是线上模型「{self._current_model}」，\n"
                "云端路由尚未接入本地服务，请选择「本地模型」对话。")
            return

        self._add_bubble(text, "user")
        self._messages.append({"role": "user", "content": text})
        self._input.clear()

        model = self._current_model
        self._busy = True
        self._cancel_requested = False
        self._gen_id += 1
        gen_id = self._gen_id
        self._set_send_state(True)
        self._assistant_text = ""
        self._assistant_label = self._add_bubble("…", "assistant")

        def _do():
            # 兜底标记：chat_stream 返回但未触发 on_done 时补发 done，
            # 确保 busy 一定被释放（红字：一直运行中->无论如何切换模型都无法发送）
            done_fired = []

            def _on_done(r):
                done_fired.append(True)
                self.done_received.emit({**r, "gen_id": gen_id})

            res = self.api.chat_stream(
                self._messages, model,
                on_chunk=lambda piece: self.chunk_received.emit(gen_id, piece),
                on_done=_on_done,
                max_tokens=2048,
                cancel_check=lambda: self._cancel_requested)
            if not done_fired:
                self.done_received.emit({**res, "gen_id": gen_id})
            return res

        def _err(e):
            # 关键修复：推理异常/超时必须解除 busy，否则一直卡在「生成中」无法发送
            if gen_id != self._gen_id:
                return  # 已被终止/新 generation 取代
            self._busy = False
            self._set_send_state(False)
            if self._assistant_label is not None:
                self._assistant_text += f"\n[生成失败] {e}"
                self._assistant_label.setText(self._assistant_text)
                self._scroll_to_bottom()
            self._assistant_label = None

        w = Worker(_do)
        w.error.connect(_err)
        w.start()

    def _on_stop_generation(self) -> None:
        """终止当前生成（参考主流智能体的停止按钮）。"""
        if not self._busy:
            return
        self._cancel_requested = True
        self._gen_id += 1          # 作废当前 generation 的后续回调
        self._busy = False
        self._set_send_state(False)
        if self._assistant_label is not None:
            self._assistant_text += "\n（已停止生成）"
            self._assistant_label.setText(self._assistant_text)
            self._scroll_to_bottom()
        self._assistant_label = None

    def _append_chunk(self, gen_id: int, piece: str) -> None:
        """主线程：把增量文本追加到当前助手气泡（过期 generation 忽略）。"""
        if gen_id != self._gen_id:
            return
        self._assistant_text += piece
        if self._assistant_label is not None:
            self._assistant_label.setText(self._assistant_text)
            self._scroll_to_bottom()

    def _on_done(self, result: dict) -> None:
        """主线程：流式结束，记录助手消息，解除忙碌。"""
        if result.get("gen_id") != self._gen_id:
            return  # 已被终止/新 generation 取代
        full = result.get("text", "")
        if result.get("cancelled"):
            full = full or "（已停止生成）"
        self._messages.append({"role": "assistant", "content": full})
        self._busy = False
        self._set_send_state(False)
        self._assistant_label = None

    def _on_clear(self) -> None:
        self._messages.clear()
        for i in reversed(range(self._content_layout.count())):
            item = self._content_layout.itemAt(i)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            self._content_layout.removeItem(item)
        self._assistant_label = None
        self._assistant_text = ""

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def refresh(self) -> None:
        """刷新模型列表。

        _collect_local_models 内含 2 个 HTTP 请求（pool + list_models），
        若在主线程同步执行会冻结 UI——这正是「切换到对话模块反应慢」的根因；
        这里放到后台线程执行，完成后经信号回主线程应用结果。
        """
        self._refresh_token += 1
        token = self._refresh_token
        worker = Worker(self._collect_local_models)
        worker.result.connect(lambda local, t=token: self._apply_refresh(local, t))
        # 出错时保留现有列表不动（不清空），仅忽略本次刷新
        worker.error.connect(lambda _msg, t=token: self._discard_refresh(t))
        worker.start()

    def _discard_refresh(self, token: int) -> None:
        """后台刷新失败：仅作废令牌，保留上次展示数据。"""
        if token == self._refresh_token:
            self._refresh_token += 1

    def _apply_refresh(self, local: Dict[str, List[str]], token: int) -> None:
        """应用后台刷新结果（主线程）。令牌不匹配说明已有更新的刷新，丢弃。"""
        if token != self._refresh_token:
            return
        running = list(local.get("running") or [])
        not_running = list(local.get("not_running") or [])
        self._local_split = {"running": running, "not_running": not_running}
        self._local_models = running + not_running
        cloud_names = ([c["name"] for c in CLOUD_FREE_MODELS]
                       + self._custom_cloud_models())

        # 默认选中本地正在运行的模型；否则选已下载；否则空
        default = running[0] if running else (not_running[0] if not_running else "")
        if (not self._current_model
                or self._current_model not in (self._local_models + cloud_names)):
            self._current_model = default
        self._update_model_btn()
        self._build_model_menu()
