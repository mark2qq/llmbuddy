# -*- coding: utf-8 -*-
"""
在线能力目录窗口（ui/widgets/online_catalog_dialog.py）——1.0.9 新增。

## 为什么单独开窗

旧实现把「在线技能仓库」「在线 MCP 清单」塞进清单中心的 Tab 里，列表被限高
（技能 140px / MCP 180px）又只有两行文本，用户的原话是：

    「目前显示区域太小导致看不全，最好单独弹一个页面，显示更多的信息，
      同时加下载安装的功能操作」

所以这里做两个**独立窗口**，共同点：

- 默认占父窗口 92% × 88%，封顶到屏幕可用区（本机 200% 缩放，逻辑宽仅 ~700，
  所以 `MIN_W=700` 是硬上限，布局全部按 700 宽验过）；
- 左侧列表 + 右侧详情分栏，列表全高、可搜索，不再被限高裁切；
- **安装动作是显式按钮**（不再依赖「双击」这种看不见的交互），
  并带确认框（来源 / ⭐ / 文件数 / 是否含脚本 / 风险等级）；
- 所有网络动作走 `Worker`（后台线程），窗口不卡；状态行实时回报进度。

## 数据流

    catalog_repos()          -> 仓库列表（GitHub topic:claude-skills）
    catalog_repo_skills()    -> 仓库内 `**/SKILL.md` 枚举（tree API，限流时后端
                                自动降级成 codeload tarball 扫描）
    catalog_install_skill()  -> 整包 tarball 落盘（原子 + 回滚）
    catalog_mcp()            -> MCP Registry 清单
    catalog_install_mcp()    -> 生成配置 + 尝试连接

安装成功后统一回调页面 `refresh()` + `_auto_refresh_manager_lists()`，
清单中心里立刻能看到新装的条目。
"""
from typing import Any, Dict, List, Optional

import time

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from core.task_runner import Worker

#: 在线目录条目的「零配置 / 需密钥 / 需注册 / 未知」徽标（PRD §5.3 判定规则）
#: 单一来源：清单中心（`pages/assistant_page.py`）直接 import 这里的
#: `CLASS_BADGE as CATALOG_CLASS_BADGE`，两边文案不会再漂移。
CLASS_BADGE = {
    "zero_config": "[零配置]",
    "need_key": "[需密钥]",
    "need_register": "[需注册]",
    "unknown": "[未知]",
}


class _CatalogDialog(QDialog):
    """两个在线窗口的公共骨架：尺寸 / 状态行 / 关闭按钮 / 后台任务封装。"""

    TITLE = "在线目录"
    MIN_W, MIN_H = 700, 560

    def __init__(self, page, parent: Optional[QWidget] = None):
        # 父窗口优先用显式传入的，其次才用 page（page 不是 QWidget 时退回无父）
        host = parent if parent is not None else (
            page if isinstance(page, QWidget) else None)
        super().__init__(host)
        self.page = page
        self.setWindowTitle(self.TITLE)
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.setSizeGripEnabled(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        self._root = root

        #: 长任务计时（_start_wait / _tick_wait）
        self._wait_timer = None
        self._wait_t0 = 0.0
        self._wait_base = ""
        self._status = QLabel("")
        self._status.setObjectName("PageSubtitle")
        self._status.setWordWrap(True)
        self._status.setVisible(False)

        self._bottom = QHBoxLayout()
        self._bottom.setSpacing(6)
        self._bottom.addWidget(self._status, 1)

    # ---------------- 生命周期 ----------------
    def fit_to_parent(self) -> None:
        """按父窗口 92% × 88% 定尺寸，并封顶到屏幕可用区（200% 缩放必须验）。"""
        try:
            host = self.parentWidget()
            base_w = int(host.width()) if host is not None else 720
            base_h = int(host.height()) if host is not None else 720
            w = max(self.MIN_W, int(base_w * 0.92))
            h = max(self.MIN_H, int(base_h * 0.88))
            scr = self.screen()
            if scr is not None:
                avail = scr.availableGeometry()
                w = min(w, max(self.MIN_W, avail.width() - 16))
                h = min(h, max(self.MIN_H, avail.height() - 16))
            self.resize(w, h)
        except Exception:                     # 无头环境 / 尺寸异常：保持最小值
            self.resize(self.MIN_W, self.MIN_H)

    def _finish_bottom(self) -> None:
        """把状态行 + 关闭按钮挂到底部（子类构建完主体后调用一次）。"""
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        btn = box.button(QDialogButtonBox.StandardButton.Close)
        if btn is not None:
            btn.setText("关闭")
        self._bottom.addWidget(box)
        self._root.addLayout(self._bottom)

    # ---------------- 工具方法 ----------------
    def status(self, text: str, warn: bool = False) -> None:
        self._status.setText(text or "")
        self._status.setObjectName("CatalogWarn" if warn else "PageSubtitle")
        self._status.setVisible(bool((text or "").strip()))

    def _start_wait(self, text: str) -> None:
        """长任务（拉目录 / 枚举 / 下载整包）带「已等待 Ns」计时。

        下载大仓库的整包可能真要几分钟；只写一行静态文字，用户会以为卡死。
        """
        self._wait_base = text
        self._wait_t0 = time.time()
        self.status(text)
        if self._wait_timer is None:
            self._wait_timer = QTimer(self)
            self._wait_timer.setInterval(1000)
            self._wait_timer.timeout.connect(self._tick_wait)
        self._wait_timer.start()

    def _tick_wait(self) -> None:
        n = int(time.time() - getattr(self, "_wait_t0", time.time()))
        if n >= 3:
            self.status(f"{getattr(self, '_wait_base', '')}（已等待 {n}s）")

    def _stop_wait(self) -> None:
        timer = getattr(self, "_wait_timer", None)
        if timer is not None:
            timer.stop()

    @staticmethod
    def _hint(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("PageSubtitle")
        lbl.setWordWrap(True)
        return lbl

    def _run(self, fn, on_ok, on_err=None, *args, **kwargs) -> Worker:
        """统一后台调用（Worker 自持引用，避免 QThread 被 GC 掉）。"""
        w = Worker(fn, *args, **kwargs)
        w.result.connect(on_ok)
        if on_err is not None:
            w.error.connect(on_err)
        w.start()
        return w

    def _api(self):
        return self.page._assist

    def _row_height(self, widget, lines: int = 2) -> int:
        """列表行高按字高算（200% 缩放下写死 24px 会把文字裁一半）。

        `lines`：条目文本实际占几行。仓库 / MCP 是「标题 + 描述」两行，
        技能列表只有一行 —— 一律按两行算会留下尴尬的大空隙。
        """
        fm = widget.fontMetrics()
        return max(28, fm.height() * max(1, lines) + 12)


# --------------------------------------------------------------------------- #
# 在线技能目录
# --------------------------------------------------------------------------- #
class OnlineSkillsDialog(_CatalogDialog):
    """在线技能目录：仓库列表 → 枚举 SKILL.md → 下载安装到本地。"""

    TITLE = "在线技能目录"

    def __init__(self, page, parent: Optional[QWidget] = None):
        super().__init__(page, parent)
        self._repos: List[Dict[str, Any]] = []
        self._repo: Dict[str, Any] = {}
        self._items: List[Dict[str, Any]] = []
        self._busy = False
        #: 批量安装状态（`install_all` 置 True，`install_selected` 置 False）
        self._batch = False
        self._queue: List[Dict[str, Any]] = []
        self._done = 0
        self._errors: List[str] = []

        self._root.addWidget(self._hint(
            "数据源：GitHub `topic:claude-skills`（按 star 降序）。"
            "选中左侧仓库 →「列出技能」→ 选中技能 →「⬇ 下载并安装」。"
            "安装只落文件（SKILL.md + 附件），不会执行任何脚本。"))

        # ---- 顶部工具条 ----
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._search = QLineEdit()
        self._search.setObjectName("HubSearch")
        self._search.setPlaceholderText("搜索仓库（名称 / 描述）")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._fill_repos())
        bar.addWidget(self._search, 1)
        self._sort = QComboBox()
        self._sort.setObjectName("HubCombo")
        self._sort.addItem("⭐ star 降序", "stars")
        self._sort.addItem("名称 A→Z", "name")
        self._sort.currentIndexChanged.connect(lambda _i: self._fill_repos())
        bar.addWidget(self._sort)
        self._btn_refresh = QPushButton("⟳ 刷新目录")
        self._btn_refresh.setObjectName("MiniBtn")
        self._btn_refresh.setToolTip("重新联网拉取仓库列表（force=1，会占用 GitHub 配额）")
        self._btn_refresh.clicked.connect(lambda _c=False: self.refresh(force=True))
        bar.addWidget(self._btn_refresh)
        self._btn_token = QPushButton("Token")
        self._btn_token.setObjectName("MiniBtn")
        self._btn_token.setToolTip(
            "配置 GitHub Token（可选）：未认证配额仅 60 次/小时，"
            "浏览几个仓库就会限流；填任意只读 PAT 可升到 5000 次/小时")
        self._btn_token.clicked.connect(lambda _c=False: self._edit_token())
        bar.addWidget(self._btn_token)
        self._root.addLayout(bar)

        self._banner = QLabel("")
        self._banner.setObjectName("PageSubtitle")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        self._root.addWidget(self._banner)

        # ---- 主体：左仓库 / 右详情 ----
        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("HubSplitter")
        split.setChildrenCollapsible(False)

        left = QWidget()
        l_lay = QVBoxLayout(left)
        l_lay.setContentsMargins(0, 0, 0, 0)
        l_lay.setSpacing(4)
        self._count = QLabel("共 0 个仓库")
        self._count.setObjectName("HubHeadCount")
        l_lay.addWidget(self._count)
        self._list = QListWidget()
        self._list.setObjectName("HubList")
        self._list.setWordWrap(False)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentItemChanged.connect(
            lambda cur, _prev: self._on_repo_selected(cur))
        self._list.itemDoubleClicked.connect(lambda _it: self.enumerate_repo())
        l_lay.addWidget(self._list, 1)
        split.addWidget(left)

        right = QWidget()
        r_lay = QVBoxLayout(right)
        r_lay.setContentsMargins(0, 0, 0, 0)
        r_lay.setSpacing(4)
        head = QHBoxLayout()
        head.setSpacing(4)
        self._repo_lbl = QLabel("（左侧选一个仓库）")
        self._repo_lbl.setObjectName("HubHeadCount")
        self._repo_lbl.setWordWrap(True)
        head.addWidget(self._repo_lbl, 1)
        self._btn_web = QPushButton("打开网页")
        self._btn_web.setObjectName("MiniBtn")
        self._btn_web.setToolTip("在浏览器里查看该仓库")
        self._btn_web.clicked.connect(lambda _c=False: self._open_web())
        head.addWidget(self._btn_web)
        r_lay.addLayout(head)

        row_enum = QHBoxLayout()
        row_enum.setSpacing(4)
        self._btn_enum = QPushButton("🔍 列出技能")
        self._btn_enum.setObjectName("MiniBtn")
        self._btn_enum.setToolTip(
            "枚举该仓库里所有的 SKILL.md（缓存 7 天；GitHub 限流时后端自动改走"
            "整包快照扫描，可能要 10-60 秒）")
        self._btn_enum.clicked.connect(lambda _c=False: self.enumerate_repo())
        row_enum.addWidget(self._btn_enum, 1)
        r_lay.addLayout(row_enum)

        self._skill_list = QListWidget()
        self._skill_list.setObjectName("HubList")
        self._skill_list.setWordWrap(False)
        self._skill_list.setVerticalScrollMode(
            QListWidget.ScrollMode.ScrollPerPixel)
        self._skill_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._skill_list.currentItemChanged.connect(
            lambda cur, _prev: self._render_skill(cur))
        self._skill_list.itemDoubleClicked.connect(
            lambda _it: self.install_selected())
        self._skill_list.setMinimumHeight(96)
        r_lay.addWidget(self._skill_list, 1)

        self._detail = QPlainTextEdit()
        self._detail.setObjectName("HubDetail")
        self._detail.setReadOnly(True)
        self._detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._detail.setPlaceholderText("选中技能查看详细（来源 / 目录 / 文件 / 风险）")
        self._detail.setMaximumHeight(150)
        r_lay.addWidget(self._detail)

        row_ins = QHBoxLayout()
        row_ins.setSpacing(4)
        self._btn_install = QPushButton("⬇ 下载并安装")
        self._btn_install.setToolTip("把选中技能整包下载到本地技能目录（原子写入，可回滚）")
        self._btn_install.clicked.connect(lambda _c=False: self.install_selected())
        row_ins.addWidget(self._btn_install, 1)
        self._btn_install_all = QPushButton("⬇ 安装全部")
        self._btn_install_all.setObjectName("MiniBtn")
        self._btn_install_all.setToolTip("把该仓库里列出的所有技能依次装入本地")
        self._btn_install_all.clicked.connect(lambda _c=False: self.install_all())
        row_ins.addWidget(self._btn_install_all)
        r_lay.addLayout(row_ins)
        split.addWidget(right)
        split.setSizes([300, 380])
        split.setStretchFactor(0, 45)
        split.setStretchFactor(1, 55)
        self._root.addWidget(split, 1)
        self._finish_bottom()

    # ---------------- 目录 ----------------
    def refresh(self, force: bool = True) -> None:
        """拉仓库列表：force=False 先吃缓存（窗口打开瞬间就能看到内容）。"""
        self._start_wait("正在获取在线技能目录…")
        self._btn_refresh.setEnabled(False)
        self._btn_refresh.setText("⟳ 获取中…")
        self._run(self._api().catalog_repos, self._apply,
                  lambda m: self._fail("获取仓库列表失败", m), "", bool(force))

    def _fail(self, what: str, msg: str) -> None:
        self._stop_wait()
        self._btn_refresh.setEnabled(True)
        self._btn_refresh.setText("⟳ 刷新目录")
        self._busy = False
        self.status(f"{what}：{msg}", warn=True)

    def _apply(self, data: Any) -> None:
        self._stop_wait()
        self._btn_refresh.setEnabled(True)
        self._btn_refresh.setText("⟳ 刷新目录")
        data = data if isinstance(data, dict) else {}
        self._repos = [r for r in (data.get("items") or [])
                       if isinstance(r, dict)]
        self._fill_repos()
        fetched = float(data.get("fetched_at") or 0)
        import time as _t
        when = (_t.strftime("%m-%d %H:%M", _t.localtime(fetched))
                if fetched else "")
        stale = bool(data.get("stale"))
        err = str(data.get("error") or "")
        if err:
            self._banner.setText(f"目录刷新失败：{err}"
                                 + (f" · 正在显示 {when} 的缓存" if when else ""))
            self._banner.setObjectName("CatalogWarn")
            self._banner.setVisible(True)
        elif stale and when:
            self._banner.setText(f"正在显示 {when} 的缓存（点「⟳ 刷新目录」同步）")
            self._banner.setObjectName("CatalogNote")
            self._banner.setVisible(True)
        else:
            self._banner.setVisible(False)
        self.status(f"目录已就绪：{len(self._repos)} 个仓库"
                    + (f" · 数据时间 {when}" if when else ""))

    def _fill_repos(self) -> None:
        """按搜索词 + 排序渲染仓库列表（纯本地过滤，不再打网络）。"""
        kw = (self._search.text() or "").strip().lower()
        rows = [r for r in self._repos
                if not kw
                or kw in str(r.get("full_name") or "").lower()
                or kw in str(r.get("description") or "").lower()]
        mode = str(self._sort.currentData() or "stars")
        if mode == "name":
            rows.sort(key=lambda r: str(r.get("full_name") or "").lower())
        else:
            rows.sort(key=lambda r: -int(r.get("stars") or 0))
        self._list.blockSignals(True)
        self._list.clear()
        h = self._row_height(self._list)
        for r in rows:
            name = str(r.get("full_name") or "")
            desc = str(r.get("description") or "").strip()
            item = QListWidgetItem(f"⭐{int(r.get('stars') or 0)}  {name}\n{desc}")
            item.setData(Qt.ItemDataRole.UserRole, r)
            item.setSizeHint(QSize(0, h))
            item.setToolTip(f"{name}\n{desc}\n默认分支："
                            f"{r.get('default_branch') or 'main'}")
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._count.setText(f"共 {len(rows)} 个仓库"
                            + (f"（已筛选，总 {len(self._repos)}）"
                               if len(rows) != len(self._repos) else ""))
        if not rows:
            hint = ("（在线技能目录为空：点「⟳ 刷新目录」或检查网络 / 代理）"
                    if not self._repos
                    else "（没有匹配的仓库：清空搜索词）")
            self._list.addItem(QListWidgetItem(hint))

    # ---------------- 仓库 → 技能 ----------------
    def _on_repo_selected(self, item: Optional[QListWidgetItem]) -> None:
        repo = (item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        self._repo = repo if isinstance(repo, dict) else {}
        self._items = []
        self._skill_list.clear()
        self._detail.clear()
        name = str(self._repo.get("full_name") or "")
        if not name:
            self._repo_lbl.setText("（左侧选一个仓库）")
            return
        self._repo_lbl.setText(f"{name}  ·  ⭐{int(self._repo.get('stars') or 0)}")
        self.status("已选中仓库，点「🔍 列出技能」枚举其中的 SKILL.md")
        self._render_repo_detail()

    def _render_repo_detail(self) -> None:
        r = self._repo
        if not r:
            return
        lines = [
            f"仓库：{r.get('full_name') or ''}",
            f"⭐ star：{int(r.get('stars') or 0)}",
            f"默认分支：{r.get('default_branch') or 'main'}",
        ]
        for key, label in (("language", "语言"), ("license", "许可"),
                           ("updated_at", "更新时间"), ("html_url", "地址")):
            if r.get(key):
                lines.append(f"{label}：{r.get(key)}")
        if r.get("description"):
            lines.append("")
            lines.append(f"说明：{r.get('description')}")
        topics = r.get("topics") or []
        if topics:
            lines.append(f"标签：{'、'.join(str(t) for t in topics[:12])}")
        self._detail.setPlainText("\n".join(lines))

    def focus_repo(self, full_name: str) -> None:
        """按 `owner/repo` 选中并自动枚举（供页面里旧的「双击仓库」入口跳转）。"""
        target = str(full_name or "").strip().lower()
        for i in range(self._list.count()):
            item = self._list.item(i)
            repo = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(repo, dict) and \
                    str(repo.get("full_name") or "").lower() == target:
                self._list.setCurrentItem(item)
                self.enumerate_repo()
                return

    def enumerate_repo(self, force: bool = False) -> None:
        full = str(self._repo.get("full_name") or "")
        if "/" not in full:
            self.status("先在左侧选一个仓库", warn=True)
            return
        if self._busy:
            return
        self._busy = True
        owner, name = full.split("/", 1)
        ref = str(self._repo.get("default_branch") or "")
        self._btn_enum.setEnabled(False)
        self._btn_enum.setText("🔍 枚举中…")
        self._start_wait("正在枚举仓库里的 SKILL.md…"
                         "（GitHub 限流时后端会自动改走整包快照扫描，大仓库需较久）")
        self._run(self._api().catalog_repo_skills, self._apply_skills,
                  lambda m: self._enum_failed(m), owner, name, ref, bool(force))

    def _enum_failed(self, msg: str) -> None:
        self._stop_wait()
        self._busy = False
        self._btn_enum.setEnabled(True)
        self._btn_enum.setText("🔍 列出技能")
        self.status(f"枚举失败：{msg}", warn=True)

    def _apply_skills(self, res: Any) -> None:
        self._stop_wait()
        self._busy = False
        self._btn_enum.setEnabled(True)
        self._btn_enum.setText("🔍 列出技能")
        res = res if isinstance(res, dict) else {}
        self._items = [i for i in (res.get("items") or [])
                       if isinstance(i, dict)]
        self._skill_list.clear()
        h = self._row_height(self._skill_list, lines=1)
        for it in self._items:
            flag = "⚠含脚本" if it.get("has_scripts") else "纯文本"
            text = (f"{it.get('name') or it.get('slug') or ''}"
                    f"   [{it.get('dir') or '/'}]   "
                    f"{int(it.get('file_count') or 0)} 附件 · {flag}")
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, it)
            item.setSizeHint(QSize(0, h))
            item.setToolTip(str(it.get("description") or ""))
            self._skill_list.addItem(item)
        via = "整包快照扫描" if str(res.get("via") or "") == "archive" else "tree API"
        if not self._items:
            self._skill_list.addItem(
                QListWidgetItem("（该仓库里没有找到 SKILL.md，不是技能仓库）"))
            self.status("该仓库没有 SKILL.md，无法安装", warn=True)
            return
        self._skill_list.setCurrentRow(0)
        self.status(f"找到 {len(self._items)} 个技能（来源：{via}）"
                    f" · 选中后点「⬇ 下载并安装」")
        if res.get("stale") and res.get("error"):
            self.status(f"找到 {len(self._items)} 个技能（缓存结果："
                        f"{res.get('error')}）")

    def _render_skill(self, item: Optional[QListWidgetItem]) -> None:
        it = (item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        it = it if isinstance(it, dict) else None
        if not it:
            return
        full = str(self._repo.get("full_name") or "")
        files = [str(f) for f in (it.get("files") or [])]
        lines = [
            f"技能：{it.get('name') or it.get('slug') or ''}",
            f"标识：{it.get('slug') or ''}",
            f"来源：{full}" + (f" · {it.get('dir')}" if it.get("dir") else ""),
            f"文件：{len(files)} 个附件"
            + (f"（{'、'.join(files[:8])}{'…' if len(files) > 8 else ''}）"
               if files else ""),
            f"含可执行脚本：{'是（只落盘不执行）' if it.get('has_scripts') else '否'}",
        ]
        if it.get("description"):
            lines.insert(2, f"说明：{it.get('description')}")
        lines.append("")
        lines.append("风险：medium —— 技能正文只作为提示词，但可能引导模型调用高危工具；"
                     "安装过程不会执行任何下载到的脚本。")
        self._detail.setPlainText("\n".join(lines))

    def _open_web(self) -> None:
        full = str(self._repo.get("full_name") or "")
        if not full:
            return
        url = str(self._repo.get("html_url") or f"https://github.com/{full}")
        QDesktopServices.openUrl(QUrl(url))

    # ---------------- 安装 ----------------
    def install_selected(self) -> None:
        item = self._skill_list.currentItem()
        it = (item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        if not isinstance(it, dict):
            self.status("先在右侧选中一个技能", warn=True)
            return
        self._install_one(it)

    def install_all(self) -> None:
        if not self._items:
            self.status("先点「🔍 列出技能」", warn=True)
            return
        if not self._confirm_many():
            return
        self._batch = True
        self._queue = list(self._items)
        self._done = 0
        self._errors: List[str] = []
        self._install_next()

    def _install_next(self) -> None:
        """批量安装的推进器：队列空了就打汇总（单装不走这里）。"""
        if not self._queue:
            self._busy = False
            msg = f"安装完成：成功 {self._done} 个"
            if self._errors:
                msg += f"，失败 {len(self._errors)} 个（{self._errors[0][:80]}）"
            self.status(msg, warn=bool(self._errors))
            self.page.refresh()
            self.page._auto_refresh_manager_lists()
            return
        it = self._queue.pop(0)
        self._do_install(it)

    def _install_one(self, it: Dict[str, Any]) -> None:
        if not self._confirm(it):
            return
        self._batch = False
        self._queue = []
        self._done = 0
        self._errors = []
        self._do_install(it)

    def _do_install(self, it: Dict[str, Any]) -> None:
        if self._busy:
            self.status("正在安装上一个技能，请稍候…", warn=True)
            return
        self._busy = True
        full = str(self._repo.get("full_name") or "")
        ref = str(self._repo.get("default_branch") or "")
        slug = str(it.get("slug") or "")
        self._start_wait(f"正在下载并安装 {slug or it.get('name')}…"
                         "（整包 tarball，大技能可能要几分钟，请勿关闭窗口）")
        self._run(self._api().catalog_install_skill, self._installed,
                  lambda m, s=slug: self._install_failed(m, s),
                  full, ref, str(it.get("dir") or ""), slug, True)

    def _installed(self, res: Any) -> None:
        self._stop_wait()
        self._busy = False
        res = res if isinstance(res, dict) else {}
        slug = str(res.get("slug") or "")
        size = int(res.get("bytes") or 0)
        skipped = int(res.get("skipped") or 0)
        self._done += 1
        txt = f"已安装：{slug or '（未知）'}"
        if size:
            txt += f" · {size / 1024:.0f} KB"
        if skipped:
            txt += f" · 跳过 {skipped} 个（越界或超预算）"
        self.page.refresh()
        self.page._auto_refresh_manager_lists()
        if self._batch:
            # 批量：把单条结果落到状态行，然后继续下一件（最后一件会打汇总）
            self.status(txt + (f" · 剩余 {len(self._queue)} 个"
                               if self._queue else ""))
            self._install_next()
        else:
            self.status(txt + " · 已写入本地技能目录，可在「技能」页看到")

    def _install_failed(self, msg: str, slug: str) -> None:
        self._stop_wait()
        self._busy = False
        if self._batch:
            self._errors.append(f"{slug}: {msg}")
            self._install_next()
            return
        self.status(f"安装失败：{msg}", warn=True)

    def _confirm(self, it: Dict[str, Any]) -> bool:
        """在线技能安装确认框（来源 / ⭐ / 文件数 / 脚本警示 / 风险）。"""
        full = str(self._repo.get("full_name") or "")
        files = [str(f) for f in (it.get("files") or [])]
        lines = [
            f"来源：{full}" + (f" · {it.get('dir')}" if it.get("dir") else ""),
            f"技能：{it.get('name') or it.get('slug') or ''}"
            f"（标识 {it.get('slug') or ''}）",
            f"⭐ star：{int(self._repo.get('stars') or 0)}",
            f"文件数：{len(files)}"
            + (f"（{'、'.join(files[:6])}{'…' if len(files) > 6 else ''}）"
               if files else ""),
            "风险等级：medium（技能正文只作为提示词，但可能引导模型调用高危工具）",
        ]
        if it.get("description"):
            lines.insert(2, f"说明：{it.get('description')}")
        if it.get("has_scripts"):
            lines.append("")
            lines.append("⚠ 该技能含 scripts/ 或可执行文件，运行时有额外风险。")
        lines.append("")
        lines.append("安装只落文件，不会执行任何下载到的脚本。是否继续？")
        ans = QMessageBox.question(
            self, "从在线来源安装技能", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes)
        return ans == QMessageBox.StandardButton.Yes

    def _confirm_many(self) -> bool:
        names = [str(i.get("slug") or i.get("name") or "") for i in self._items]
        lines = [
            f"来源：{self._repo.get('full_name') or ''}",
            f"将安装 {len(self._items)} 个技能：{'、'.join(names[:8])}"
            + ("…" if len(names) > 8 else ""),
            "",
            "逐个整包下载，同一目录下同名技能会覆盖。是否继续？",
        ]
        ans = QMessageBox.question(
            self, "批量安装技能", "\n".join(lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return ans == QMessageBox.StandardButton.Yes

    # ---------------- Token ----------------
    def _edit_token(self) -> None:
        # 不预填：Token 属密钥，后端也不下发（state 里没有它），
        # 留空提交 = 清空。用户重新粘贴完整值即可。
        text, ok = QInputDialog.getText(
            self, "GitHub Token（可选）",
            "未认证的 GitHub API 配额只有 60 次/小时，浏览几个仓库就会 403。\n"
            "填一个只读 PAT（classical token，无需勾选任何 scope）可升到 5000 次/小时。\n"
            "留空提交 = 清空（仍可用，限流时后端会自动改走整包快照扫描）。\n\n"
            "Token：",
            QLineEdit.EchoMode.Password, "")
        if not ok:
            return
        token = (text or "").strip()

        def save():
            return self._api().save_settings({"catalog": {"github_token": token}})

        self.status("正在保存 Token…")
        self._run(save, lambda _r: self.status(
            "Token 已保存" if token else "Token 已清空"),
            lambda m: self.status(f"保存失败：{m}", warn=True))


# --------------------------------------------------------------------------- #
# 在线 MCP 清单
# --------------------------------------------------------------------------- #
class OnlineMcpDialog(_CatalogDialog):
    """在线 MCP 清单：Registry 搜索 → 详情 → 生成配置并尝试连接。"""

    TITLE = "在线 MCP 清单"

    def __init__(self, page, parent: Optional[QWidget] = None):
        super().__init__(page, parent)
        self._items: List[Dict[str, Any]] = []

        self._root.addWidget(self._hint(
            "数据源：官方 MCP Registry。选中条目看右侧详情 →「安装」会写好 "
            "npx / http 配置并尝试连接；MCP 属高危（本地子进程 = 任意代码执行），"
            "运行时逐次审批。"))

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._search = QLineEdit()
        self._search.setObjectName("HubSearch")
        self._search.setPlaceholderText("搜索 MCP（名称 / 描述）")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _t: self._fill())
        bar.addWidget(self._search, 1)
        self._btn_refresh = QPushButton("⟳ 刷新清单")
        self._btn_refresh.setObjectName("MiniBtn")
        self._btn_refresh.clicked.connect(lambda _c=False: self.refresh(force=True))
        bar.addWidget(self._btn_refresh)
        self._root.addLayout(bar)

        self._banner = QLabel("")
        self._banner.setObjectName("PageSubtitle")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        self._root.addWidget(self._banner)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.setObjectName("HubSplitter")
        split.setChildrenCollapsible(False)

        left = QWidget()
        l_lay = QVBoxLayout(left)
        l_lay.setContentsMargins(0, 0, 0, 0)
        l_lay.setSpacing(4)
        self._count = QLabel("共 0 个服务")
        self._count.setObjectName("HubHeadCount")
        l_lay.addWidget(self._count)
        self._list = QListWidget()
        self._list.setObjectName("HubList")
        self._list.setWordWrap(False)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.currentItemChanged.connect(
            lambda cur, _prev: self._render(cur))
        self._list.itemDoubleClicked.connect(lambda _it: self.install_selected())
        l_lay.addWidget(self._list, 1)
        split.addWidget(left)

        right = QWidget()
        r_lay = QVBoxLayout(right)
        r_lay.setContentsMargins(0, 0, 0, 0)
        r_lay.setSpacing(4)
        self._detail = QPlainTextEdit()
        self._detail.setObjectName("HubDetail")
        self._detail.setReadOnly(True)
        self._detail.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._detail.setPlaceholderText("选中左侧条目查看传输方式 / 版本 / 依赖 / 风险")
        r_lay.addWidget(self._detail, 1)
        row = QHBoxLayout()
        row.setSpacing(4)
        self._btn_install = QPushButton("⬇ 安装并连接")
        self._btn_install.setToolTip("按 Registry 元数据生成配置，写入后立即尝试连接")
        self._btn_install.clicked.connect(lambda _c=False: self.install_selected())
        row.addWidget(self._btn_install, 1)
        self._btn_web = QPushButton("查看源")
        self._btn_web.setObjectName("MiniBtn")
        self._btn_web.setToolTip("打开该条目的上游主页 / 仓库")
        self._btn_web.clicked.connect(lambda _c=False: self._open_web())
        row.addWidget(self._btn_web)
        r_lay.addLayout(row)
        split.addWidget(right)
        split.setSizes([300, 380])
        self._root.addWidget(split, 1)
        self._finish_bottom()

    def refresh(self, force: bool = True) -> None:
        self._start_wait("正在获取在线 MCP 清单…")
        self._btn_refresh.setEnabled(False)
        self._run(self._api().catalog_mcp, self._apply,
                  lambda m: self._fail(m), "", "", bool(force))

    def _fail(self, msg: str) -> None:
        self._stop_wait()
        self._btn_refresh.setEnabled(True)
        self.status(f"获取 MCP 清单失败：{msg}", warn=True)

    def _apply(self, data: Any) -> None:
        self._stop_wait()
        self._btn_refresh.setEnabled(True)
        data = data if isinstance(data, dict) else {}
        self._items = [i for i in (data.get("items") or [])
                       if isinstance(i, dict)]
        self._fill()
        import time as _t
        fetched = float(data.get("fetched_at") or 0)
        when = (_t.strftime("%m-%d %H:%M", _t.localtime(fetched))
                if fetched else "")
        err = str(data.get("error") or "")
        if err:
            self._banner.setText(f"清单刷新失败：{err}"
                                 + (f" · 正在显示 {when} 的缓存" if when else ""))
            self._banner.setObjectName("CatalogWarn")
            self._banner.setVisible(True)
        elif bool(data.get("stale")) and when:
            self._banner.setText(f"正在显示 {when} 的缓存")
            self._banner.setObjectName("CatalogNote")
            self._banner.setVisible(True)
        else:
            self._banner.setVisible(False)
        self.status(f"清单已就绪：{len(self._items)} 个服务")

    def _fill(self) -> None:
        kw = (self._search.text() or "").strip().lower()
        rows = [i for i in self._items
                if not kw
                or kw in str(i.get("title") or "").lower()
                or kw in str(i.get("name") or "").lower()
                or kw in str(i.get("description") or "").lower()]
        self._list.blockSignals(True)
        self._list.clear()
        h = self._row_height(self._list)
        for it in rows:
            badge = CLASS_BADGE.get(str(it.get("free_class") or "unknown"),
                                    "[未知]")
            title = it.get("title") or it.get("registry_name") or ""
            item = QListWidgetItem(
                f"{badge} {title}\n{it.get('description') or ''}")
            item.setData(Qt.ItemDataRole.UserRole, it)
            item.setSizeHint(QSize(0, h))
            item.setToolTip(f"{it.get('registry_name') or ''}\n"
                            f"传输：{it.get('transport') or ''} · "
                            f"版本：{it.get('version') or ''}")
            self._list.addItem(item)
        self._list.blockSignals(False)
        self._count.setText(f"共 {len(rows)} 个服务"
                            + (f"（已筛选，总 {len(self._items)}）"
                               if len(rows) != len(self._items) else ""))
        if not rows:
            hint = ("（在线 MCP 目录为空：点「⟳ 刷新清单」或检查网络 / 代理）"
                    if not self._items
                    else "（没有匹配的条目：清空搜索词）")
            self._list.addItem(QListWidgetItem(hint))

    def _render(self, item: Optional[QListWidgetItem]) -> None:
        it = (item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        if not isinstance(it, dict):
            return
        suggest = it.get("suggest") if isinstance(it.get("suggest"), dict) else {}
        required = [str(e.get("name")) for e in (suggest.get("required_env") or [])
                    if isinstance(e, dict) and e.get("name")]
        optional = [str(e.get("name")) for e in (suggest.get("optional_env") or [])
                    if isinstance(e, dict) and e.get("name")]
        transport = str(suggest.get("transport") or it.get("transport") or "stdio")
        lines = [
            f"名称：{it.get('title') or it.get('name') or ''}",
            f"Registry：{it.get('registry_name') or ''}",
            f"版本：{it.get('version') or '（未标注）'}",
            f"传输：{'远程 HTTP' if transport == 'http' else '本地子进程（stdio）'}",
            f"免费分级：{CLASS_BADGE.get(str(it.get('free_class') or 'unknown'), '[未知]')}",
            f"必填密钥 / 变量：{'、'.join(required) if required else '无'}",
            f"可选变量：{'、'.join(optional) if optional else '无'}",
            f"启动命令：{suggest.get('command') or '（不适用）'}",
            f"风险等级：high（本地子进程 = 任意代码执行，运行时逐次审批）",
        ]
        if it.get("description"):
            lines.insert(1, f"说明：{it.get('description')}")
        if str(it.get("free_class") or "") == "unknown":
            lines.append("")
            lines.append("⚠ 该条目只登记了元数据（没有 packages / remotes），"
                         "无法自动生成配置，请点「查看源」手动配置。")
        self._detail.setPlainText("\n".join(lines))

    def _open_web(self) -> None:
        item = self._list.currentItem()
        it = (item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        if not isinstance(it, dict):
            return
        url = (it.get("homepage") or it.get("repository") or it.get("url") or "")
        if url:
            QDesktopServices.openUrl(QUrl(str(url)))

    def install_selected(self) -> None:
        item = self._list.currentItem()
        it = (item.data(Qt.ItemDataRole.UserRole) if item is not None else None)
        if not isinstance(it, dict):
            self.status("先在左侧选中一个条目", warn=True)
            return
        # 安装细节（必填 env 收集 / 确认 / 连接失败提示）复用页面里已验证的实现
        self.page._install_online_mcp_item(it, parent=self)
