# -*- coding: utf-8 -*-
"""
模型库页面（library_page.py）。

功能映射：
    - 目录拉取/搜索       -> M3 catalog(force, keyword)
    - 按 free_ram 过滤    -> M1 system_info().free_ram_gb >= item.min_ram_gb 标记为可运行
    - 认证下拉下载(进度)  -> M4 pull() + 轮询 M5 pull_status()
    - 需鉴权弹 auth_dialog -> 下载失败时弹出重新尝试

说明：表格展现目录；可运行模型高亮，不可运行置灰；下载进度用弹窗显示。
"""
import os
from typing import Optional

import httpx
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                               QLabel, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QHeaderView, QMessageBox)
from PySide6.QtCore import Qt, QTimer, QObject, Signal

from core.task_runner import Worker
from core.auto_service import AutoService
from core.api_client import ApiError
from ui.widgets.progress_dialog import ProgressDialog
from ui.widgets.auth_dialog import AuthDialog
from ui.widgets.import_dialog import ImportDialog, derive_name_from_url


class _ProgressBridge(QObject):
    """跨线程进度桥：Worker 子线程 emit tick，主线程 ProgressDialog 接收更新。"""

    tick = Signal(float, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancelled = False


class LibraryPage(QWidget):
    """模型库页面。"""

    status_message = None  # 由主窗口可选注入

    def __init__(self, api, launcher, config, parent=None):
        super().__init__(parent)
        self.api = api
        self.launcher = launcher
        self.config = config
        self._items = []
        self._free_ram = 0.0
        self._poll_timer: Optional[QTimer] = None
        self._dl_name: str = ""
        self._dl_progress: Optional[ProgressDialog] = None
        # 连接被拒（服务未运行）时自动启动后端并延时重试
        self._auto = AutoService(launcher, retry_cb=lambda: self.refresh())
        self._build_ui()

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(16, 16, 16, 16)

        # 顶部工具条
        bar = QHBoxLayout()
        bar.addWidget(QLabel("搜索:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("模型名 / 仓库关键字")
        self._search.returnPressed.connect(self.refresh)
        bar.addWidget(self._search, 1)
        self._import_btn = QPushButton("手动添加")
        self._import_btn.setObjectName("PrimaryBtn")
        self._import_btn.setToolTip(
            "粘贴 .gguf 直链（ModelScope / hf-mirror / GitHub Releases）直接下载")
        self._import_btn.clicked.connect(self._on_manual_import)
        bar.addWidget(self._import_btn)
        self._refresh_btn = QPushButton("自动获取")
        self._refresh_btn.setObjectName("PrimaryBtn")
        self._refresh_btn.setToolTip(
            "重新获取模型目录（绕过缓存），符合本机配置的模型排在前面，需要时自行下载")
        self._refresh_btn.clicked.connect(lambda _checked=False: self.refresh(True))
        bar.addWidget(self._refresh_btn)
        root.addLayout(bar)

        self._hint = QLabel("加载中…")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

        card = QGroupBox("模型目录")
        card.setObjectName("Card")
        card.setLayout(QVBoxLayout())
        self._table = QTableWidget(0, 8)
        self._table.setObjectName("CatalogTable")
        self._table.setHorizontalHeaderLabels(
            ["名称", "仓库", "量化", "大小(GB)", "最小内存(GB)", "参数", "发布时间", "本地/操作"])
        # 列宽策略（红字要求：所有列——含名称列——都可左右拖动伸缩）：
        #   全部列 Interactive（初始按内容给定宽度，用户可拖动；名称列不再用 Stretch，
        #   因为 Stretch 列会强制铺满且禁用用户拖动）
        hdr = self._table.horizontalHeader()
        for col in range(0, 8):
            hdr.setSectionResizeMode(col, QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        hdr.setSectionsMovable(False)
        # 初始列宽（用户可拖动调整）
        for col, w in ((0, 340), (1, 200), (2, 100), (3, 95), (4, 120), (5, 80), (6, 110), (7, 270)):
            self._table.setColumnWidth(col, w)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._table.verticalHeader().setVisible(False)
        # 红字要求：操作列（按钮）显示不全 -> 加大默认行高，保证按钮完整呈现
        self._table.verticalHeader().setDefaultSectionSize(42)
        card.layout().addWidget(self._table)
        root.addWidget(card, 1)

    # ------------------------------------------------------------------ #
    # 后台
    # ------------------------------------------------------------------ #
    def _bg(self, fn, on_ok, on_err=None):
        w = Worker(fn)
        w.result.connect(on_ok)
        if on_err:
            w.error.connect(on_err)
        else:
            def _default_err(e: str) -> None:
                # 连接被拒（服务未运行）→ 自动启动服务并延时重试，否则按原样提示
                if not self._auto.handle(e, lambda m: self._hint.setText(m)):
                    self._hint.setText(f"错误: {e}")
            w.error.connect(_default_err)
        w.start()
        return w

    # ------------------------------------------------------------------ #
    # 刷新
    # ------------------------------------------------------------------ #
    def refresh(self, force: bool = False) -> None:
        """拉取目录 + 系统信息，渲染表格。

        force=True（「自动获取」按钮）：绕过 24h 缓存重新合并目录，
        并把符合本机配置的模型排在前面。
        """
        kw = self._search.text().strip()
        # 红字要求：「自动获取」点击要有反馈 -> 立即进入加载态（禁用按钮+提示）
        self._set_loading(True, "正在获取模型目录…" if force else "加载中…")

        def _finish():
            self._set_loading(False)

        def _sys_ok(d):
            self._free_ram = float((d or {}).get("free_ram_gb", 0) or 0)
            _finish()
            self._render()

        def _sys_err(e):
            _finish()
            self._on_catalog_err(e)

        self._bg(lambda: self.api.system_info(), _sys_ok, _sys_err)

        def _cat_ok(d):
            self._items = (d or {}).get("items", []) or []
            cached = (d or {}).get("cached", False)
            self._hint.setText(
                f"共 {len(self._items)} 个模型（缓存: {cached}）"
                f"；空闲内存 {self._free_ram:.1f} GB；"
                f"符合配置的排在前面，灰色为内存不足，需要时点击下载")
            _finish()
            self._render()

        def _cat_err(e):
            _finish()
            self._on_catalog_err(e)

        self._bg(lambda: self.api.catalog(force=force, keyword=kw), _cat_ok, _cat_err)

    def _set_loading(self, loading: bool, text: str = "") -> None:
        """目录获取期间的加载态：禁用操作按钮 + 更新提示（红字：点击要有反馈）。"""
        self._import_btn.setEnabled(not loading)
        self._refresh_btn.setEnabled(not loading)
        self._search.setEnabled(not loading)
        if text:
            self._hint.setText(text)

    def _on_catalog_err(self, e: str) -> None:
        """目录加载失败：连接被拒（服务未运行）时自动启动服务并重试。"""
        if not self._auto.handle(e, lambda m: self._hint.setText(m)):
            self._hint.setText(f"目录加载失败: {e}")

    def _render(self) -> None:
        kw = self._search.text().strip().lower()
        rows = []
        for it in self._items:
            name = it.get("name", "")
            repo = it.get("repo", "")
            if kw and kw not in name.lower() and kw not in repo.lower():
                continue
            rows.append(it)

        def _runnable(it) -> bool:
            if it.get("local"):
                return True
            return self._free_ram >= float(it.get("min_ram_gb", 0) or 0)

        # 自动获取语义：符合本机配置的排前，内存不足的灰显在后（稳定排序保序）
        rows.sort(key=lambda it: 0 if _runnable(it) else 1)

        self._table.setRowCount(len(rows))
        for i, it in enumerate(rows):
            name = it.get("name", "")
            local = bool(it.get("local", False))
            min_ram = float(it.get("min_ram_gb", 0) or 0)
            runnable = local or (self._free_ram >= min_ram) if not local else True
            name_item = QTableWidgetItem(name)
            if not runnable:
                name_item.setForeground(Qt.GlobalColor.gray)
            # 名称可能被省略号截断：tooltip 展示完整名称
            name_item.setToolTip(f"{name}（仓库: {it.get('repo', '')}）")
            self._table.setItem(i, 0, name_item)
            self._table.setItem(i, 1, QTableWidgetItem(str(it.get("repo", ""))))
            self._table.setItem(i, 2, QTableWidgetItem(str(it.get("quant", ""))))
            self._table.setItem(i, 3, QTableWidgetItem(str(it.get("size_gb", ""))))
            self._table.setItem(i, 4, QTableWidgetItem(str(it.get("min_ram_gb", ""))))
            self._table.setItem(i, 5, QTableWidgetItem(str(it.get("params_b", ""))))
            # 「发布时间」：取目录中的 published 字段（官方发布日，YYYY-MM-DD）；
            # 本地导入/缺失该字段时显示「—」
            pub = str(it.get("published", "") or "").strip()
            pub_item = QTableWidgetItem(pub if pub else "—")
            pub_item.setToolTip(pub if pub else "该模型无发布时间信息")
            self._table.setItem(i, 6, pub_item)
            self._table.setCellWidget(i, 7, self._build_action_cell(name, local, it))
        self._table.horizontalScrollBar().setValue(0)

    def _build_action_cell(self, name: str, local: bool, it: dict) -> QWidget:
        """第 7 列操作单元格（红字要求：按钮要在单元格内垂直居中）。

        未下载：单个「⬇ 下载」按钮；
        已下载：绿色徽标 + 「删除」+「重新下载」。
        """
        cell = QWidget()
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(6)
        if local:
            lbl = QLabel("✔ 已下载")
            lbl.setObjectName("LocalBadge")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(lbl)
            del_btn = QPushButton("删除")
            del_btn.setObjectName("CellBtn")
            del_btn.setToolTip(f"删除模型文件 {name}.gguf（不可恢复）")
            del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            del_btn.clicked.connect(lambda _checked=False, n=name: self._on_delete_model(n))
            lay.addWidget(del_btn)
            re_btn = QPushButton("重新下载")
            re_btn.setObjectName("CellBtn")
            re_btn.setToolTip("重新下载该模型（覆盖本地文件）")
            re_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            re_btn.clicked.connect(lambda _checked=False, n=name: self._on_download(n))
            lay.addWidget(re_btn)
        else:
            btn = QPushButton("⬇ 下载")
            btn.setObjectName("DownloadBtn")
            btn.setToolTip(f"下载 {name}（约 {it.get('size_gb', '?')} GB）")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _checked=False, n=name: self._on_download(n))
            lay.addWidget(btn)
        return cell

    def _on_delete_model(self, name: str) -> None:
        """删除本地模型文件（确认后调用后端 M19）。"""
        reply = QMessageBox.question(
            self, "删除模型",
            f"确定删除模型 '{name}' 的本地文件吗？\n删除后需要重新下载才能使用。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return

        def _do():
            return self.api.delete_model(name)

        def _ok(_d):
            self._hint.setText(f"已删除模型 {name}")
            self.refresh()

        def _err(e):
            QMessageBox.warning(self, "删除失败", str(e))

        self._bg(_do, _ok, _err)

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #
    def _on_download(self, name: str) -> None:
        self._dl_name = name
        self._dl_progress = ProgressDialog(title=f"下载 {name}", cancellable=True, parent=self)
        self._dl_progress.set_cancel_callback(lambda: self._cancel_download(name))
        self._dl_progress.show()
        self._do_pull(name, None)

    # ------------------------------------------------------------------ #
    # 手动添加（粘贴 .gguf 直链直接下载）
    # ------------------------------------------------------------------ #
    def _on_manual_import(self) -> None:
        """打开「手动添加」对话框，确认后按直链下载 .gguf。"""
        dlg = ImportDialog(free_ram_gb=self._free_ram, parent=self)
        if dlg.exec() != ImportDialog.Accepted:
            return
        url = dlg.get_url()
        if not url:
            QMessageBox.warning(self, "提示", "请填写 .gguf 下载直链")
            return
        self._do_manual_download(url, dlg.get_name())

    def _do_manual_download(self, url: str, name: str, transport=None) -> None:
        """按直链下载 .gguf 到 models_dir。

        参数：
            url        下载直链
            name       目标文件名（空则从 url 推导）
            transport  供测试注入 httpx.MockTransport，运行期为 None
        """
        if not name:
            name = derive_name_from_url(url)
        if not name.lower().endswith(".gguf"):
            name += ".gguf"

        models_dir = getattr(self.config, "models_dir", "") or ""
        try:
            os.makedirs(models_dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001 - 目录不可写时提示即可
            QMessageBox.warning(self, "提示", f"模型目录不可用: {e}")
            return
        target = os.path.join(models_dir, name)

        if os.path.exists(target):
            reply = QMessageBox.question(
                self, "覆盖确认",
                f"已存在同名文件：\n{target}\n是否覆盖？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return

        bridge = _ProgressBridge()
        progress = ProgressDialog(title=f"手动下载 {name}", cancellable=True,
                                  parent=self)
        progress.set_cancel_callback(lambda: setattr(bridge, "cancelled", True))
        # 跨线程信号会自动排队到主线程，UI 更新安全
        bridge.tick.connect(progress.set_progress)
        progress.show()

        def _do():
            self._download_to_file(url, target, name, bridge,
                                   transport=transport)

        def _ok(_d):
            progress.finish(True, f"{name} 下载完成")
            self.refresh()

        def _err(e):
            progress.finish(False, f"手动下载失败: {e}")
            # 失败/取消：删除半截文件，避免留下损坏的 gguf
            try:
                if os.path.exists(target):
                    os.remove(target)
            except Exception:
                pass

        self._bg(_do, _ok, _err)

    def _download_to_file(self, url: str, target: str, name: str,
                          bridge: _ProgressBridge, transport=None) -> None:
        """流式下载到目标文件，并通过 bridge 上报进度（支持取消）。"""
        with httpx.Client(trust_env=False, follow_redirects=True,
                          timeout=httpx.Timeout(connect=30, read=120,
                                                write=60, pool=30),
                          transport=transport) as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length") or 0)
                done = 0
                with open(target, "wb") as f:
                    for chunk in r.iter_bytes(256 * 1024):
                        if bridge.cancelled:
                            raise RuntimeError("已取消")
                        f.write(chunk)
                        done += len(chunk)
                        bridge.tick.emit(
                            done / total if total else 0.0,
                            f"{name} {done/1e6:.0f}/{total/1e6:.0f} MB")

    def _do_pull(self, name: str, auth) -> None:
        def _do():
            return self.api.pull(name, auth=auth)
        self._bg(_do, lambda _d: self._start_poll(name),
                 lambda e: self._on_pull_error(name, e))

    def _on_pull_error(self, name: str, err: str) -> None:
        low = err.lower()
        if "auth" in low or "token" in low or "401" in low or "gated" in low:
            # 需要鉴权：弹 auth_dialog 重试
            dlg = AuthDialog(self)
            if dlg.exec() == dlg.Accepted:
                auth = dlg.get_auth()
                if auth.get("token"):
                    self._do_pull(name, auth)
                    return
            if self._dl_progress:
                self._dl_progress.finish(False, "已取消（需要鉴权）")
            return
        if self._dl_progress:
            self._dl_progress.finish(False, f"失败: {err}")

    def _start_poll(self, name: str) -> None:
        self._dl_name = name
        if self._dl_progress:
            self._dl_progress.set_indeterminate(f"下载 {name} …")
        if self._poll_timer is None:
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._poll_tick)
        self._poll_timer.start(1000)

    @staticmethod
    def _dl_text(name: str, status: str, st: dict) -> str:
        """下载进度文案：百分比 + 已下载/总量 + 实时速度 + 并发线程数。

        多线程分片加速后（core/download.py），让用户能直观看到速度提升。
        """
        pct = int(float(st.get("progress", 0.0) or 0.0) * 100)
        parts = [f"{name}: {status} ({pct}%)"]
        done = float(st.get("downloaded", 0) or 0)
        total = float(st.get("total_bytes", 0) or 0)
        if total > 0:
            parts.append(f"{done / 1048576:.1f}/{total / 1048576:.0f} MB")
        spd = float(st.get("speed_mbps", 0.0) or 0.0)
        if spd > 0:
            parts.append(f"{spd:.1f} MB/s")
        th = int(st.get("threads", 1) or 1)
        if th > 1:
            parts.append(f"{th} 线程并行")
        return " · ".join(parts)

    def _poll_tick(self) -> None:
        name = self._dl_name
        if not name:
            return
        try:
            st = self.api.pull_status(name)
        except Exception as e:
            if self._dl_progress:
                self._dl_progress.finish(False, f"查询失败: {e}")
            self._stop_poll()
            return
        status = st.get("status", "")
        progress = float(st.get("progress", 0.0) or 0.0)
        if self._dl_progress:
            self._dl_progress.set_progress(
                progress, self._dl_text(name, status, st))
        if status == "done":
            if self._dl_progress:
                self._dl_progress.finish(True, f"{name} 下载完成")
            self._stop_poll()
            self.refresh()
        elif status == "error":
            err = st.get("error") or "未知错误"
            self._on_pull_error(name, err)
            self._stop_poll()
        elif status == "cancelled":
            if self._dl_progress:
                self._dl_progress.finish(False, "已取消")
            self._stop_poll()
            self.refresh()

    def _stop_poll(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        self._dl_name = ""

    def _cancel_download(self, name: str) -> None:
        try:
            self.api.cancel_pull(name)
        except Exception:
            pass
        self._stop_poll()
        if self._dl_progress:
            self._dl_progress.finish(False, "已取消下载")
        self.refresh()
