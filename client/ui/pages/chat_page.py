# -*- coding: utf-8 -*-
"""
对话页面（chat_page.py）。

功能映射：
    - 模型选择       -> 底部输入区下方的下拉按钮（参考 WorkBuddy：显示当前模型 + ⌄），
                       点击弹出选择面板：本地模型 / 在线模型 · SiliconFlow / 自定义模型，
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
import json
from datetime import datetime
from typing import Dict, List, Optional

import httpx
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QTextEdit, QPushButton, QLabel, QScrollArea,
                               QMenu, QMessageBox, QSizePolicy, QDialog,
                               QLineEdit, QFormLayout)
from PySide6.QtCore import Qt, Signal, QSettings, QSize, QPoint, QRect
from PySide6.QtGui import (QPixmap, QPainter, QColor, QPolygon,
                           QIcon)

from core.task_runner import Worker
from ui.widgets.custom_model_dialog import (CustomModelDialog,
                                            load_custom_models,
                                            save_custom_models)
from ui.widgets.progress_dialog import ProgressDialog


# 线上免费模型统一的 OpenAI 兼容端点（SiliconFlow）
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"

# SiliconFlow 官方标注「免费」的模型 ID 清单（2026-10 整理）。
# 背景：GET /models 接口的响应只含 id/created/owned_by，不带免费标记，
# 因此用这份清单给拉取到的模型打「免费」标并在菜单置顶；未命中的模型仍
# 可选择（按量付费，余额不足时发送会报错，用户可自行判断）。
SILICONFLOW_KNOWN_FREE = (
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-14B",
    "Qwen/Qwen3-32B",
    "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    "deepseek-ai/DeepSeek-R1",
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
    "deepseek-ai/DeepSeek-V3",
    "THUDM/glm-4-9b-chat",
    "THUDM/GLM-4-9B-0414",
    "meta-llama/Llama-3.1-8B-Instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct",
)

# 内置云端模型清单：首次「获取最新模型」前的默认值（取已知免费模型）。
# 走云端路由：选中后直连 SiliconFlow，复用 chat_stream_custom。
CLOUD_FREE_MODELS: List[Dict[str, str]] = [
    {"name": m, "tag": "免费"} for m in SILICONFLOW_KNOWN_FREE
]

# 云端模型列表的 QSettings 缓存键（动态拉取结果 + 拉取时间）
CLOUD_MODELS_KEY = "chat/cloud_models"
CLOUD_MODELS_FETCHED_AT_KEY = "chat/cloud_models_fetched_at"


def fetch_cloud_models(api_key: str) -> List[Dict[str, str]]:
    """后台拉取 SiliconFlow 最新 chat 模型列表（GET /models?sub_type=chat）。

    返回 [{name, tag}, ...]：已知免费模型 tag 为「免费」并置顶（组内保持
    接口返回顺序），其余 tag 为「SiliconFlow」。失败抛异常（由 Worker 以
    字符串回传）。trust_env=False 必须保留——本机有系统代理。
    """
    api_key = str(api_key or "").strip()
    if not api_key:
        raise RuntimeError("未配置 SiliconFlow API Key")
    url = f"{SILICONFLOW_BASE_URL}/models?sub_type=chat"
    with httpx.Client(trust_env=False, timeout=15.0,
                      headers={"Authorization": f"Bearer {api_key}",
                               "Accept": "application/json"}) as c:
        resp = c.get(url)
        body = resp.text
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {body[:200]}")
    try:
        data = json.loads(body)
    except Exception:
        raise RuntimeError(f"响应解析失败：{body[:200]}")
    items = data.get("data") or []
    out: List[Dict[str, str]] = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = str(it.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        out.append({"name": mid,
                    "tag": "免费" if mid in SILICONFLOW_KNOWN_FREE else "SiliconFlow"})
    if not out:
        raise RuntimeError("接口未返回任何 chat 模型")
    free = [m for m in out if m["tag"] == "免费"]
    rest = [m for m in out if m["tag"] != "免费"]
    return free + rest


class CloudKeyDialog(QDialog):
    """配置 SiliconFlow API Key（全局，供所有线上免费模型共用）。

    首次使用线上免费模型且尚未配置 Key 时弹出；保存后立即写入 QSettings
    并继续本次发送（无需用户再点一次）。
    """

    def __init__(self, parent=None, current_key: str = ""):
        super().__init__(parent)
        self.setWindowTitle("配置 SiliconFlow API Key")
        self.setObjectName("CloudKeyDialog")
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        hint = QLabel(
            "在线模型由 SiliconFlow 提供，需要一个 API Key 才能调用。\n"
            "免费获取地址：https://cloud.siliconflow.cn/account/ak")
        hint.setWordWrap(True)
        hint.setObjectName("HintLabel")
        layout.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        key_row = QWidget()
        kr = QHBoxLayout(key_row)
        kr.setContentsMargins(0, 0, 0, 0)
        kr.setSpacing(6)
        self._key_edit = QLineEdit(current_key)
        self._key_edit.setObjectName("CloudApiKeyEdit")
        self._key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_edit.setPlaceholderText("sk-...")
        self._show_btn = QPushButton("显示")
        self._show_btn.setObjectName("TinyBtn")
        self._show_btn.setCheckable(True)
        self._show_btn.toggled.connect(self._on_toggle)
        kr.addWidget(self._key_edit, 1)
        kr.addWidget(self._show_btn)
        form.addRow("API Key:", key_row)
        layout.addLayout(form)

        self._error_label = QLabel("")
        self._error_label.setObjectName("ErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#e5484d;")
        layout.addWidget(self._error_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存并继续")
        save.setObjectName("PrimaryBtn")
        save.clicked.connect(self._on_save)
        btn_row.addWidget(cancel)
        btn_row.addWidget(save)
        layout.addLayout(btn_row)

    def _on_toggle(self, checked: bool) -> None:
        self._key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        self._show_btn.setText("隐藏" if checked else "显示")

    def _on_save(self) -> None:
        key = self._key_edit.text().strip()
        if not key:
            self._error_label.setText("请填写 API Key。")
            return
        self._saved_key = key
        self.accept()

    def saved_key(self) -> str:
        return getattr(self, "_saved_key", "").strip()

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
        self._current_custom: Optional[Dict] = None  # 当前选中的自定义模型 entry
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
        self._model_btn.setToolTip("选择要对话的模型（本地模型 / 在线模型）")
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
    def _custom_models(self) -> List[Dict]:
        """读取自定义模型列表（新键 JSON；旧键自动一次性迁移）。"""
        return load_custom_models(self._settings, self._legacy_settings)

    def _save_custom_models(self, models: List[Dict]) -> None:
        """写入自定义模型列表（QSettings 持久化）。"""
        save_custom_models(self._settings, models)

    def _cloud_api_key(self) -> str:
        """读取全局云端（SiliconFlow）API Key（空串表示未配置）。"""
        return str(self._settings.value("chat/cloud_api_key", "") or "").strip()

    def _set_cloud_api_key(self, key: str) -> None:
        """写入全局云端（SiliconFlow）API Key。"""
        self._settings.setValue("chat/cloud_api_key", key)
        self._settings.sync()

    def _on_configure_cloud_key(self) -> None:
        """打开「配置 SiliconFlow API Key」对话框（菜单入口，支持重配/清除）。"""
        dlg = CloudKeyDialog(self, current_key=self._cloud_api_key())
        if dlg.exec():
            self._set_cloud_api_key(dlg.saved_key())

    # ------------------------------------------------------------------ #
    # 云端模型列表（动态拉取 + QSettings 缓存）
    # ------------------------------------------------------------------ #
    def _cloud_models(self) -> List[Dict[str, str]]:
        """当前生效的云端模型列表：QSettings 缓存优先，否则回退内置清单。"""
        raw = self._settings.value(CLOUD_MODELS_KEY, None)
        if raw is not None:
            data = raw
            if isinstance(raw, str):
                try:
                    data = json.loads(raw)
                except Exception:
                    data = None
            if isinstance(data, list):
                out: List[Dict[str, str]] = []
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    name = str(it.get("name") or "").strip()
                    if not name:
                        continue
                    out.append({"name": name,
                                "tag": str(it.get("tag") or "SiliconFlow")})
                if out:
                    return out
        return [dict(m) for m in CLOUD_FREE_MODELS]

    def _save_cloud_models(self, models: List[Dict[str, str]]) -> None:
        """缓存拉取到的云端模型列表并记录拉取时间。"""
        self._settings.setValue(CLOUD_MODELS_KEY,
                                json.dumps(models, ensure_ascii=False))
        self._settings.setValue(CLOUD_MODELS_FETCHED_AT_KEY,
                                datetime.now().strftime("%Y-%m-%d %H:%M"))
        self._settings.sync()

    def _on_fetch_cloud_models(self) -> None:
        """菜单动作：从 SiliconFlow 拉取最新 chat 模型列表。

        未配置 Key 先弹配置框；拉取走后台线程 + 进度弹窗（不冻结 UI）；
        成功后写缓存并重建菜单，失败保留原列表并提示原因。
        """
        api_key = self._cloud_api_key()
        if not api_key:
            kdlg = CloudKeyDialog(self, current_key="")
            if not kdlg.exec():
                return
            api_key = kdlg.saved_key()
            self._set_cloud_api_key(api_key)

        dlg = ProgressDialog("获取最新模型", cancellable=False, parent=self)
        dlg.set_indeterminate("正在从 SiliconFlow 获取最新模型列表…")

        def _ok(models: List[Dict[str, str]]) -> None:
            self._save_cloud_models(models)
            # 当前选中的模型若已不在新列表中（且非本地/自定义），复位到首个免费模型
            names = [m["name"] for m in models]
            if (self._current_custom is None and self._current_model
                    and self._current_model not in names
                    and self._current_model not in self._local_models):
                self._current_model = names[0] if names else ""
                self._update_model_btn()
            self._build_model_menu()
            free_n = sum(1 for m in models if m.get("tag") == "免费")
            dlg.finish(True,
                       f"✓ 已获取 {len(models)} 个模型（其中 {free_n} 个免费）")

        def _fail(msg: str) -> None:
            dlg.finish(False, f"获取失败：{str(msg)[:200]}")

        w = Worker(fetch_cloud_models, api_key)
        w.result.connect(_ok)
        w.error.connect(_fail)
        w.start()
        dlg.exec()

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
        """重建选择面板：本地模型 / 在线模型 · SiliconFlow / 自定义模型 / 配置自定义模型。

        结构收敛为两大类（需求：模型分本地与在线，在线以免费为主，另支持自定义）：
            本地模型   —— 运行中(●)与已下载(○)合并为一段，前缀保留状态语义；
            在线模型 · SiliconFlow —— 免费模型直接展示，付费模型收「其它模型」子菜单；
            自定义模型 —— 在线段内子分组（本质也是在线模型），保留 custom: 内部键路由；
            底部动作   —— 配置 API Key / 获取最新模型列表 / 配置自定义模型。

        参数 local 缺省时使用 refresh() 缓存的 _local_split（绝不在此处发起
        网络请求——本函数会被打开菜单/选模型等交互频繁调用，必须瞬时完成）。
        """
        self._model_menu.clear()
        if local is None:
            local = self._local_split
        running = local["running"]
        not_running = local["not_running"]

        # 本地模型：运行中(●)与已下载(○)合并为一段，前缀保留状态语义；
        # 两者皆空时保留禁用提示，引导用户到模型库下载/加载
        if running or not_running:
            self._add_menu_header("本地模型")
            for n in running:
                self._add_menu_action(f"●  {n}", n)
            for n in not_running:
                self._add_menu_action(f"○  {n}", n)
        else:
            hint = self._model_menu.addAction("（暂无本地模型，请到模型库下载/加载）")
            hint.setEnabled(False)

        self._model_menu.addSeparator()

        # 在线模型：免费置顶 + 付费进子菜单（"以免费为主"≠只有免费，保留可用性）
        self._add_menu_header("在线模型 · SiliconFlow")
        cloud = self._cloud_models()
        free = [c for c in cloud if c.get("tag") == "免费"]
        rest = [c for c in cloud if c.get("tag") != "免费"]
        for c in free:
            label = f"☁  {c['name']}" + (f"  ·  {c.get('tag', '')}" if c.get("tag") else "")
            self._add_menu_action(label, c["name"])
        if rest:
            # 付费模型收进子菜单，避免主列表过长；选中后同样走云端直连
            sub = self._model_menu.addMenu(f"其它模型（{len(rest)}，按量付费）")
            sub.setObjectName("ModelMenu")
            for c in rest:
                label = f"☁  {c['name']}"
                self._add_menu_action(label, c["name"], menu=sub)

        # 自定义模型：在线段内子分组（自定义模型本质也是在线模型），
        # 放在 SiliconFlow 模型列表之后、底部动作按钮之前；逻辑不变
        custom_models = self._custom_models()
        if custom_models:
            self._add_menu_header("自定义模型")
            for e in custom_models:
                name = e.get("name") or e.get("model") or ""
                if not name:
                    continue
                self._add_custom_menu_action(f"☁  {name}", e)

        self._model_menu.addSeparator()
        cfg_key = self._model_menu.addAction("🔑  配置 SiliconFlow API Key")
        cfg_key.triggered.connect(self._on_configure_cloud_key)
        refresh = self._model_menu.addAction("🔄  获取最新模型列表")
        refresh.triggered.connect(self._on_fetch_cloud_models)
        cfg = self._model_menu.addAction("⚙  配置自定义模型")
        cfg.triggered.connect(self._on_configure_custom_model)

    def _add_menu_header(self, text: str) -> None:
        act = self._model_menu.addAction(text)
        act.setEnabled(False)

    def _add_menu_action(self, label: str, name: str, menu=None) -> None:
        """添加模型菜单项；menu 缺省为顶层面板（子菜单传入 sub QMenu）。"""
        target = menu if menu is not None else self._model_menu
        act = target.addAction(("✓  " if name == self._current_model else "    ") + label)
        act.triggered.connect(
            lambda _checked=False, m=name: self._on_pick_model(m))

    def _add_custom_menu_action(self, label: str, entry: Dict) -> None:
        """自定义模型菜单项：内部键 custom:{name}，避免与本地/免费模型重名。"""
        name = entry.get("name") or entry.get("model") or ""
        key = f"custom:{name}"
        current_name = ""
        if self._current_custom is not None:
            current_name = (self._current_custom.get("name")
                            or self._current_custom.get("model") or "")
        act = self._model_menu.addAction(("✓  " if name == current_name else "    ") + label)
        act.triggered.connect(
            lambda _checked=False, k=key: self._on_pick_model(k))

    def _find_custom_by_name(self, display: str) -> Optional[Dict]:
        """按显示名查找自定义模型 entry。"""
        for e in self._custom_models():
            if (e.get("name") or e.get("model") or "") == display:
                return e
        return None

    def _find_custom_by_model(self, model_id: str) -> Optional[Dict]:
        """按模型 ID 查找自定义模型 entry（编辑后同步当前选中项用）。"""
        for e in self._custom_models():
            if (e.get("model") or "") == model_id:
                return e
        return None

    def _on_configure_custom_model(self, entry: Optional[Dict] = None) -> None:
        """打开「配置自定义模型」对话框（新建 / 编辑 / 删除）。"""
        dlg = CustomModelDialog(self, entry=entry)
        if not dlg.exec():
            return
        self._build_model_menu()
        # 编辑可能改了当前选中项的字段（如 API Key）：按模型 ID / 名称重新同步
        if self._current_custom is not None:
            old = self._current_custom
            refreshed = self._find_custom_by_model(old.get("model") or "")
            if refreshed is None:
                refreshed = self._find_custom_by_name(old.get("name") or old.get("model") or "")
            if refreshed is not None:
                self._current_custom = refreshed
                self._current_model = refreshed.get("name") or refreshed.get("model") or ""
            else:
                self._current_custom = None
                self._current_model = ""
        self._update_model_btn()

    def _on_pick_model(self, name: str) -> None:
        """选中某模型，更新当前模型与下拉按钮显示（参考 WorkBuddy：按钮显示选中项）。

        custom:{name} 内部键 -> 自定义模型路由；其余为本地/线上免费模型名。
        """
        if name.startswith("custom:"):
            display = name[len("custom:"):]
            entry = self._find_custom_by_name(display)
            if entry is None:
                return
            self._current_custom = entry
            self._current_model = display
        else:
            self._current_custom = None
            self._current_model = name
        self._update_model_btn()
        self._build_model_menu()

    def _update_model_btn(self) -> None:
        if self._current_custom is not None:
            prefix = "☁ "
            display = (self._current_custom.get("name")
                       or self._current_custom.get("model")
                       or self._current_model)
        else:
            prefix = "" if self._current_model in self._local_models else "☁ "
            display = self._current_model
        text = prefix + (display or "选择模型")
        # 名称可能很长：按按钮最大宽度省略，tooltip 展示完整名
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(self._model_btn.font())
        self._model_btn.setText(fm.elidedText(text, Qt.TextElideMode.ElideRight, 170) + "  ⌄")
        self._model_btn.setToolTip(display or "选择模型")

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
                                    "请先点击下方「选择模型」挑选一个模型（本地模型 / 在线模型）。")
            return

        # 自定义模型：跳过本地模型拦截，走自定义 OpenAI 兼容端点
        if self._current_custom is not None:
            entry = self._current_custom
            if not (entry.get("base_url") or "").strip() \
                    or not (entry.get("api_key") or "").strip():
                ret = QMessageBox.question(
                    self, "自定义模型配置不完整",
                    "该自定义模型缺少 API 地址 / Key，请重新配置。\n"
                    "是否现在打开配置对话框？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                if ret == QMessageBox.StandardButton.Yes:
                    self._on_configure_custom_model(entry)
                return
            self._add_bubble(text, "user")
            self._messages.append({"role": "user", "content": text})
            self._input.clear()
            self._start_streaming(
                lambda on_chunk, on_done, max_tokens, cancel_check:
                    self.api.chat_stream_custom(
                        entry, self._messages,
                        on_chunk=on_chunk, on_done=on_done,
                        max_tokens=max_tokens, cancel_check=cancel_check))
            return

        # 线上免费模型预设：走云端路由（SiliconFlow 直连），复用自定义模型流式通道
        if self._current_model in [c["name"] for c in self._cloud_models()]:
            api_key = self._cloud_api_key()
            if not api_key:
                dlg = CloudKeyDialog(self, current_key="")
                if not dlg.exec():
                    return  # 取消：不发送，也不卡死
                api_key = dlg.saved_key()
                self._set_cloud_api_key(api_key)
            # 用局部 entry 直连，不污染 _current_custom（保持选中态语义干净）
            entry = {
                "name": self._current_model,
                "base_url": SILICONFLOW_BASE_URL,
                "api_key": api_key,
                "model": self._current_model,
            }
            self._add_bubble(text, "user")
            self._messages.append({"role": "user", "content": text})
            self._input.clear()
            self._start_streaming(
                lambda on_chunk, on_done, max_tokens, cancel_check:
                    self.api.chat_stream_custom(
                        entry, self._messages,
                        on_chunk=on_chunk, on_done=on_done,
                        max_tokens=max_tokens, cancel_check=cancel_check))
            return

        self._add_bubble(text, "user")
        self._messages.append({"role": "user", "content": text})
        self._input.clear()
        model = self._current_model
        self._start_streaming(
            lambda on_chunk, on_done, max_tokens, cancel_check:
                self.api.chat_stream(
                    self._messages, model,
                    on_chunk=on_chunk, on_done=on_done,
                    max_tokens=max_tokens, cancel_check=cancel_check))

    def _start_streaming(self, stream_fn) -> None:
        """启动流式生成（本地 / 自定义共用）：复用打字机、停止、多轮上下文逻辑。

        stream_fn 接收 on_chunk/on_done/max_tokens/cancel_check 关键字参数，
        返回 {"text": 全文, "cancelled": 是否取消}（与 api_client.chat_stream 一致）。
        """
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

            res = stream_fn(
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
        cloud_names = ([c["name"] for c in self._cloud_models()]
                       + [e.get("name") or e.get("model") or ""
                          for e in self._custom_models()])

        # 默认选中本地正在运行的模型；否则选已下载；否则空
        default = running[0] if running else (not_running[0] if not_running else "")
        if (not self._current_model
                or self._current_model not in (self._local_models + cloud_names)):
            self._current_model = default
            self._current_custom = None
        self._update_model_btn()
        self._build_model_menu()
