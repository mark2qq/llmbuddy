# -*- coding: utf-8 -*-
"""
API 页面（api_page.py）。

功能映射：
    - 展示 base_url      -> config.openai_base_url()（http://host:port/v1）
    - 模型名列表         -> GET /v1/models（可点选）
    - API Key 按模型管理 -> 选中模型显示其绑定 Key（未生成显示空）；
                           「生成」为当前选中模型创建 Key（M7 扩展，model 绑定）
"""
from typing import Optional

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QLabel, QPushButton, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from core.task_runner import Worker
from ui.widgets.api_key_dialog import ApiKeyDialog


class ApiPage(QWidget):
    """API 页面。"""

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._selected_model: Optional[str] = None   # 当前选中的模型
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(14)
        root.setContentsMargins(16, 16, 16, 16)

        # base_url
        card = QGroupBox("OpenAI 兼容接口")
        card.setObjectName("Card")
        card.setLayout(QVBoxLayout())
        url_row = QHBoxLayout()
        self._url = QLineEdit(self.config.openai_base_url())
        self._url.setReadOnly(True)
        self._url.setObjectName("ApiUrlField")
        url_row.addWidget(QLabel("Base URL:"))
        url_row.addWidget(self._url, 1)
        copy_url = QPushButton("复制")
        copy_url.clicked.connect(lambda: self._copy(self._url.text()))
        url_row.addWidget(copy_url)
        card.layout().addLayout(url_row)
        card.layout().addWidget(QLabel(
            "其它智能体 / 脚本可直接对接此地址（/v1/models、/v1/chat/completions）。"))
        root.addWidget(card)

        # 模型列表（可点选：红字要求——选择不同模型显示对应 Key）
        model_card = QGroupBox("可用模型（点选模型以查看 / 生成其 API Key）")
        model_card.setObjectName("Card")
        model_card.setLayout(QVBoxLayout())
        self._models = QListWidget()
        self._models.setObjectName("ModelList")
        self._models.currentItemChanged.connect(self._on_model_selected)
        model_card.layout().addWidget(self._models)
        root.addWidget(model_card, 1)

        # API Key（按模型）
        key_card = QGroupBox("API Key")
        key_card.setObjectName("Card")
        key_card.setLayout(QVBoxLayout())
        self._key_model_label = QLabel("未选择模型（生成全局 Key）")
        self._key_model_label.setObjectName("MutedLabel")
        key_card.layout().addWidget(self._key_model_label)

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("Key:"))
        self._key_label = QLineEdit("")
        self._key_label.setReadOnly(True)
        self._key_label.setObjectName("ApiKeyField")
        self._key_label.setPlaceholderText("（尚未生成 Key，点击「生成」创建）")
        key_row.addWidget(self._key_label, 1)
        gen = QPushButton("生成")
        gen.setObjectName("PrimaryBtn")
        gen.clicked.connect(self._on_generate)
        # 一键复制完整 Key（64 位）并给出反馈，避免手动选中复制不全
        self._copy_btn = QPushButton("复制")
        self._copy_btn.setToolTip("复制完整 Key（64 位），请勿手动选中部分文本")
        self._copy_btn.clicked.connect(self._on_copy_key)
        key_row.addWidget(gen)
        key_row.addWidget(self._copy_btn)
        key_card.layout().addLayout(key_row)
        root.addWidget(key_card)

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _copy(self, text: str) -> None:
        cb = QApplication.clipboard()
        if cb is not None and text:
            cb.setText(text)

    def _on_copy_key(self) -> None:
        """复制完整 Key 并提示位数（便于确认没有复制残缺）。"""
        text = self._key_label.text()
        if not text:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self._copy_btn.setText(f"已复制 {len(text)} 位")
        QTimer.singleShot(1800, lambda: self._copy_btn.setText("复制"))

    def _on_model_selected(self, current, _previous) -> None:
        """红字要求：选择不同模型，对应显示不同 Key（未生成则显示空）。"""
        if current is None:
            self._selected_model = None
        else:
            self._selected_model = current.data(Qt.ItemDataRole.UserRole) or ""
        self._key_label.setText("")
        if not self._selected_model:
            self._key_model_label.setText("未选择模型（生成全局 Key）")
            return
        self._key_model_label.setText(f"当前模型: {self._selected_model}")
        # 后台查询该模型已绑定的 Key
        def _do():
            return self.api.list_keys()

        def _ok(keys):
            model = self._selected_model
            if model is None:
                return
            found = ""
            for k in keys or []:
                if k.get("model") == model and k.get("key"):
                    found = k["key"]        # 列表按时间倒序 -> 最后命中即最新
            self._key_label.setText(found)
            self._key_label.setPlaceholderText(
                "（该模型尚未生成 Key，点击「生成」创建）" if not found else "")

        def _err(e):
            # 后端旧版本无 /api/v1/keys 时静默降级
            pass

        self._bg(_do, _ok, _err)

    def _on_generate(self) -> None:
        model = self._selected_model or None

        def _do():
            return self.api.gen_key(model)

        def _ok(key):
            self._show_key(key, model)

        w = Worker(_do)
        w.result.connect(_ok)
        w.error.connect(lambda e: QMessageBox.warning(self, "失败", f"生成失败: {e}"))
        w.start()

    def _show_key(self, key: str, model: Optional[str] = None) -> None:
        self._key_label.setText(key)
        if model:
            self._key_model_label.setText(f"当前模型: {model}")
        dlg = ApiKeyDialog(key, self)
        dlg.exec()

    def _bg(self, fn, on_ok, on_err=None):
        w = Worker(fn)
        w.result.connect(on_ok)
        if on_err:
            w.error.connect(on_err)
        w.start()
        return w

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def _local_model_names(self):
        """本程序已下载模型名集合（目录里 local=True 的条目 + 池中已加载）。

        返回 None 表示「无法判断」（目录查询失败），此时不做过滤，
        避免后端异常时列表被误清空。
        """
        names = set()
        known = False
        try:
            cat = self.api.catalog(force=False) or {}
            items = cat.get("items") if isinstance(cat, dict) else None
            if items is not None:
                known = True
                for it in items:
                    if it.get("local") and it.get("name"):
                        names.add(it["name"])
        except Exception:
            pass
        try:
            pool = self.api.pool() or {}
            for e in (pool.get("loaded") or []):
                if e.get("name"):
                    names.add(e["name"])
                    known = True
        except Exception:
            pass
        return names if known else None

    def refresh(self) -> None:
        self._url.setText(self.config.openai_base_url())
        try:
            models = self.api.list_models()
        except Exception:
            models = []
        # 红字要求：不是本程序下载的模型不要显示（内置目录模板不计入）
        local_names = self._local_model_names()
        if local_names is not None:
            models = [m for m in models if m.get("id") in local_names]
        # 红字要求：要默认显示已经加载的所有模型 ->
        #   ① 已加载模型置顶（● 标记，一眼可见）；② 默认选中第一个已加载模型
        loaded = [m for m in models if m.get("loaded")]
        others = [m for m in models if not m.get("loaded")]
        ordered = loaded + others
        self._models.clear()
        # 第 0 项固定为「全局 Key」：不绑定模型，任何模型都能用。
        # （默认选中已加载模型后，仍需保留一个生成通用 Key 的入口，
        #   否则第三方客户端切换模型会因 Key 绑定而 403。）
        global_item = QListWidgetItem("（全局 Key · 不绑定模型，任意模型可用）")
        global_item.setData(Qt.ItemDataRole.UserRole, "")
        self._models.addItem(global_item)
        for m in ordered:
            mid = m.get("id", "")
            mark = "● 已加载" if m.get("loaded") else "○ 未加载"
            self._models.addItem(f"{mid}   [{mark}]")
        # 记录条目对应的模型 id（用于按模型查询 Key；跳过第 0 项全局项）
        for i in range(1, self._models.count()):
            item = self._models.item(i)
            mid = item.text().rsplit("   [", 1)[0].strip()
            item.setData(Qt.ItemDataRole.UserRole, mid)
        if not ordered:
            # 没有任何已下载模型：给一句可操作的提示（全局 Key 仍可生成）
            hint = QListWidgetItem(
                "（暂无已下载模型：请先到「模型库」下载，或使用上方全局 Key）")
            hint.setFlags(Qt.ItemFlag.NoItemFlags)
            self._models.addItem(hint)
            self._selected_model = None
            self._key_model_label.setText("未选择模型（生成全局 Key）")
            self._models.setCurrentRow(0)
            return
        # 尽量保持选中项；否则默认选中第一个「已加载」模型
        cur_row = -1
        if self._selected_model:
            for i in range(1, self._models.count()):
                if self._models.item(i).data(Qt.ItemDataRole.UserRole) == self._selected_model:
                    cur_row = i
                    break
        if cur_row < 0:
            cur_row = 0
            for i in range(1, self._models.count()):
                if "● 已加载" in self._models.item(i).text():
                    cur_row = i
                    break
        # 选中变化会自动触发按模型 Key 查询
        self._models.setCurrentRow(cur_row)
