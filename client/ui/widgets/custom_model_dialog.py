# -*- coding: utf-8 -*-
"""
配置自定义模型对话框（custom_model_dialog.py）。

参考 WorkBuddy 的「配置自定义模型」流程：
    - 表单（非单行输入）：显示名称 / API 地址 / API Key / 模型 ID
    - 「测试连接」：后台线程用当前表单值发一次非流式请求，不冻结 UI
    - 「编辑已有」下拉 + 「删除此模型」按钮：支持新增 / 编辑 / 删除
    - 保存时校验三个必填项（API 地址 / API Key / 模型 ID），缺项红字提示且不关闭

存储（QSettings 键）：
    - 新键 chat/custom_models：JSON 字符串列表，每项
      {"name": 显示名, "base_url": ..., "api_key": ..., "model": 模型 ID}
    - 旧键 chat/custom_cloud_models：纯名字列表（历史版本遗留），读取时一次性迁移

QSS 注意：Qt 样式表 rgba() 的 alpha 只认 0-255 整数或百分比（如
rgba(255,255,255,22%)），浮点 0.22 会让整条规则失效；本文件需要内联颜色时
直接使用十六进制颜色，规避该坑。
"""
import json
import re
import time
from typing import Dict, List, Optional

import httpx
from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
                               QLineEdit, QPushButton, QLabel, QComboBox,
                               QMessageBox, QWidget)

from core.api_client import custom_chat_url
from core.task_runner import Worker

# 自定义模型的 QSettings 存储键（新 JSON 格式 / 旧纯名字列表）
CUSTOM_MODELS_KEY = "chat/custom_models"
LEGACY_CUSTOM_MODELS_KEY = "chat/custom_cloud_models"


def _normalize_entries(data: list) -> List[Dict]:
    """把读取到的原始列表规范化为统一 entry 结构，丢弃非法项。"""
    out: List[Dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        model = str(item.get("model") or "").strip()
        name = str(item.get("name") or "").strip()
        if not name and not model:
            continue
        out.append({
            "name": name or model,
            "base_url": str(item.get("base_url") or "").strip(),
            "api_key": str(item.get("api_key") or ""),
            "model": model or name,
        })
    return out


def save_custom_models(settings: QSettings, models: List[Dict]) -> None:
    """写入自定义模型列表（JSON 字符串，ensure_ascii=False）。"""
    settings.setValue(CUSTOM_MODELS_KEY, json.dumps(models, ensure_ascii=False))
    settings.sync()


def load_custom_models(settings: QSettings,
                       legacy_settings: Optional[QSettings] = None) -> List[Dict]:
    """读取自定义模型列表；旧键（纯名字列表）不存在新键时一次性迁移。

    迁移规则：旧键每个名字 -> {"name": n, "base_url": "", "api_key": "",
    "model": n}，写回新键后旧键保留不动。旧数据可能存在于当前组织或
    LLMDesktop 旧组织（2026-10-20 产品改名）。
    """
    raw = settings.value(CUSTOM_MODELS_KEY, None)
    if raw is not None:
        data = raw
        if isinstance(raw, str):
            try:
                data = json.loads(raw)
            except Exception:
                data = None
        if isinstance(data, list):
            return _normalize_entries(data)

    old = settings.value(LEGACY_CUSTOM_MODELS_KEY, None)
    if old is None and legacy_settings is not None:
        old = legacy_settings.value(LEGACY_CUSTOM_MODELS_KEY, None, type=list)

    names: List[str] = []
    if isinstance(old, list):
        names = [str(v).strip() for v in old if str(v).strip()]
    elif isinstance(old, str) and old.strip():
        names = [old.strip()]
    entries = [{"name": n, "base_url": "", "api_key": "", "model": n}
               for n in names]
    if entries:
        save_custom_models(settings, entries)
    return entries


def _summarize_error(msg: str) -> str:
    """把 Worker 回传的「异常类型: 消息」压缩为可读的错误摘要。"""
    text = (msg or "").strip()
    m = re.match(r"^[A-Za-z_][A-Za-z0-9_.]*:\s*(.*)$", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    if len(text) > 200:
        text = text[:200] + "…"
    return text or "未知错误"


def _test_custom_entry(entry: Dict) -> float:
    """后台测试连接：非流式 POST chat/completions（max_tokens=1）。

    成功返回耗时（毫秒）；失败抛异常（由 Worker 以字符串回传）。
    trust_env=False 必须保留——本机有系统代理。
    """
    url = custom_chat_url(str(entry.get("base_url") or ""))
    headers: Dict[str, str] = {"Accept": "application/json"}
    api_key = str(entry.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": str(entry.get("model") or ""),
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
    }
    start = time.perf_counter()
    with httpx.Client(trust_env=False, timeout=15.0, headers=headers) as c:
        resp = c.post(url, json=payload)
        body = resp.text
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {body[:200]}")
    return elapsed_ms


class CustomModelDialog(QDialog):
    """配置自定义模型表单对话框（新建 / 编辑 / 删除）。"""

    def __init__(self, parent=None, entry: Optional[Dict] = None):
        super().__init__(parent)
        self.setWindowTitle("配置自定义模型")
        self.setObjectName("CustomModelDialog")
        self.setMinimumWidth(540)

        self._settings = QSettings("llmBuddy", "llmBuddy")
        self._legacy_settings = QSettings("LLMDesktop", "LLMDesktop")
        self._entries: List[Dict] = []
        self._editing: Optional[Dict] = None  # 当前编辑的原始条目（删除/替换用）
        self._test_worker: Optional[Worker] = None

        self._build_ui()
        self._populate_edit_combo()
        # 连接信号后再回填/选中，保证编辑模式也能正确选中下拉项
        self._edit_combo.currentIndexChanged.connect(self._on_combo_changed)
        if entry:
            self._prefill(entry)
            self._select_combo_for_entry(entry)

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 编辑已有 / 删除（放在顶部：既能新增，也能管理已有条目）
        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("编辑已有:"))
        self._edit_combo = QComboBox()
        self._edit_combo.setObjectName("EditExistingCombo")
        edit_row.addWidget(self._edit_combo, 1)
        self._delete_btn = QPushButton("删除此模型")
        self._delete_btn.setObjectName("DangerBtn")
        self._delete_btn.clicked.connect(self._on_delete)
        edit_row.addWidget(self._delete_btn)
        layout.addLayout(edit_row)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._name_edit = QLineEdit()
        self._name_edit.setObjectName("CustomNameEdit")
        self._name_edit.setPlaceholderText("留空则默认使用模型 ID")
        form.addRow("显示名称:", self._name_edit)

        self._base_url_edit = QLineEdit()
        self._base_url_edit.setObjectName("CustomBaseUrlEdit")
        self._base_url_edit.setPlaceholderText("https://api.siliconflow.cn/v1")
        form.addRow("API 地址:", self._base_url_edit)

        key_row = QWidget()
        kr = QHBoxLayout(key_row)
        kr.setContentsMargins(0, 0, 0, 0)
        kr.setSpacing(6)
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setObjectName("CustomApiKeyEdit")
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_edit.setPlaceholderText("sk-...")
        self._show_key_btn = QPushButton("显示")
        self._show_key_btn.setObjectName("TinyBtn")
        self._show_key_btn.setCheckable(True)
        self._show_key_btn.toggled.connect(self._on_toggle_key)
        kr.addWidget(self._api_key_edit, 1)
        kr.addWidget(self._show_key_btn)
        form.addRow("API Key:", key_row)

        self._model_edit = QLineEdit()
        self._model_edit.setObjectName("CustomModelEdit")
        self._model_edit.setPlaceholderText("Qwen/Qwen2.5-7B-Instruct")
        form.addRow("模型 ID:", self._model_edit)
        layout.addLayout(form)

        # 测试连接 + 状态
        test_row = QHBoxLayout()
        self._test_btn = QPushButton("测试连接")
        self._test_btn.setObjectName("TinyBtn")
        self._test_btn.clicked.connect(self._on_test)
        self._test_label = QLabel("")
        self._test_label.setObjectName("TestStatusLabel")
        self._test_label.setWordWrap(True)
        test_row.addWidget(self._test_btn)
        test_row.addWidget(self._test_label, 1)
        layout.addLayout(test_row)

        # 校验错误提示
        self._error_label = QLabel("")
        self._error_label.setObjectName("ErrorLabel")
        self._error_label.setWordWrap(True)
        self._error_label.setStyleSheet("color:#e5484d;")
        layout.addWidget(self._error_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.clicked.connect(self.reject)
        self._save_btn = QPushButton("保存")
        self._save_btn.setObjectName("PrimaryBtn")
        self._save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._save_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------ #
    # 编辑已有下拉
    # ------------------------------------------------------------------ #
    def _populate_edit_combo(self) -> None:
        """刷新「编辑已有」下拉（读取当前存储的条目）。"""
        self._entries = load_custom_models(self._settings, self._legacy_settings)
        self._edit_combo.blockSignals(True)
        self._edit_combo.clear()
        self._edit_combo.addItem("（选择已有模型以编辑）", None)
        for e in self._entries:
            label = e.get("name") or e.get("model") or ""
            self._edit_combo.addItem(label, e)
        self._edit_combo.blockSignals(False)

    def _select_combo_for_entry(self, entry: Dict) -> None:
        """在下拉中选中与 entry 相同的条目（编辑模式回显）。"""
        for i in range(self._edit_combo.count()):
            data = self._edit_combo.itemData(i)
            if data and self._same_entry(data, entry):
                self._edit_combo.setCurrentIndex(i)
                return

    def _on_combo_changed(self, index: int) -> None:
        entry = self._edit_combo.itemData(index)
        if not entry:
            return
        self._prefill(entry)

    # ------------------------------------------------------------------ #
    # 取值 / 回填 / 校验
    # ------------------------------------------------------------------ #
    def entry(self) -> Dict:
        """返回当前表单值（显示名缺省时回退为模型 ID）。"""
        name = self._name_edit.text().strip()
        model = self._model_edit.text().strip()
        return {
            "name": name or model,
            "base_url": self._base_url_edit.text().strip(),
            "api_key": self._api_key_edit.text().strip(),
            "model": model,
        }

    def _prefill(self, entry: Dict) -> None:
        """用已有条目回填表单，并标记为编辑态。"""
        self._name_edit.setText(entry.get("name") or "")
        self._base_url_edit.setText(entry.get("base_url") or "")
        self._api_key_edit.setText(entry.get("api_key") or "")
        self._model_edit.setText(entry.get("model") or "")
        self._editing = entry
        self._error_label.clear()
        self._clear_test_status()

    @staticmethod
    def _validate(entry: Dict) -> str:
        """校验必填项，返回错误文案（空串表示通过）。"""
        if not entry["base_url"]:
            return "请填写 API 地址（OpenAI 兼容 base URL）。"
        if not entry["api_key"]:
            return "请填写 API Key。"
        if not entry["model"]:
            return "请填写模型 ID。"
        return ""

    @staticmethod
    def _same_entry(a: Optional[Dict], b: Optional[Dict]) -> bool:
        """按四个字段判断两个条目是否相同（作为增删改的定位依据）。"""
        if a is None or b is None:
            return False
        return (a.get("name") == b.get("name")
                and a.get("model") == b.get("model")
                and a.get("base_url") == b.get("base_url")
                and a.get("api_key") == b.get("api_key"))

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _on_toggle_key(self, checked: bool) -> None:
        self._api_key_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password)
        self._show_key_btn.setText("隐藏" if checked else "显示")

    def _clear_test_status(self) -> None:
        self._test_label.clear()
        self._test_label.setStyleSheet("")

    def _on_save(self) -> None:
        entry = self.entry()
        err = self._validate(entry)
        if err:
            self._error_label.setText(err)
            return
        entries = load_custom_models(self._settings, self._legacy_settings)
        if self._editing is not None and any(
                self._same_entry(e, self._editing) for e in entries):
            # 编辑：替换原条目
            entries = [entry if self._same_entry(e, self._editing) else e
                       for e in entries]
        elif not any(self._same_entry(e, entry) for e in entries):
            # 新增（去重）
            entries.append(entry)
        save_custom_models(self._settings, entries)
        self.accept()

    def _on_delete(self) -> None:
        if self._editing is None:
            QMessageBox.information(
                self, "未选择模型",
                "请先在「编辑已有」下拉中选择要删除的模型。")
            return
        name = self._editing.get("name") or self._editing.get("model") or ""
        ret = QMessageBox.question(
            self, "删除自定义模型", f"确定删除「{name}」吗？")
        if ret != QMessageBox.StandardButton.Yes:
            return
        entries = load_custom_models(self._settings, self._legacy_settings)
        entries = [e for e in entries if not self._same_entry(e, self._editing)]
        save_custom_models(self._settings, entries)
        self._editing = None
        self._populate_edit_combo()
        self._name_edit.clear()
        self._base_url_edit.clear()
        self._api_key_edit.clear()
        self._model_edit.clear()
        self._error_label.clear()
        self._clear_test_status()

    def _on_test(self) -> None:
        entry = self.entry()
        err = self._validate(entry)
        if err:
            self._error_label.setText(err)
            self._clear_test_status()
            return
        self._error_label.clear()
        self._test_btn.setEnabled(False)
        self._test_label.setText("正在测试连接…")
        self._test_label.setStyleSheet("color:#8a93a8;")
        self._test_worker = Worker(_test_custom_entry, entry)
        self._test_worker.result.connect(self._on_test_ok)
        self._test_worker.error.connect(self._on_test_fail)
        self._test_worker.start()

    def _on_test_ok(self, elapsed_ms: float) -> None:
        self._test_btn.setEnabled(True)
        self._test_label.setText(f"✓ 连接成功（{elapsed_ms:.0f} ms）")
        self._test_label.setStyleSheet("color:#3ddc84;")

    def _on_test_fail(self, msg: str) -> None:
        self._test_btn.setEnabled(True)
        self._test_label.setText(f"连接失败：{_summarize_error(msg)}")
        self._test_label.setStyleSheet("color:#e5484d;")
