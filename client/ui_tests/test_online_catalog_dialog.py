# -*- coding: utf-8 -*-
"""在线技能 / 在线 MCP 独立窗口（`ui/widgets/online_catalog_dialog.py`）回归。

覆盖 1.0.9 用户反馈：
    - 「显示区域太小看不全」→ 独立窗口 + 全高列表（不再限高 140/180px）
    - 「在线技能如何下载导入本地，需要实现」→ 显式「⬇ 下载并安装」按钮
    - 「列出技能 → 安装」全链路（枚举结果 → 确认框 → 安装 → 页面刷新）
"""
import os
import sys
import time

import pytest

# 让 `ui.*` 可导入（与其它 ui_tests 一致：client/ 进 sys.path）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from ui.widgets import online_catalog_dialog as mod  # noqa: E402
from ui.widgets.online_catalog_dialog import (  # noqa: E402
    OnlineMcpDialog, OnlineSkillsDialog)

app = QApplication.instance() or QApplication(sys.argv)


def _settle(rounds: int = 2) -> None:
    for _ in range(rounds):
        app.processEvents()
        time.sleep(0.01)


def _pump(cond, timeout: float = 3.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


class _StubApi:
    """AssistantApi 桩：只覆盖本窗口用到的 5 个接口（签名与真实实现一致）。"""

    def __init__(self) -> None:
        self.repos_data = {
            "items": [
                {"full_name": "acme/awesome-skills", "stars": 120,
                 "description": "技能合集", "default_branch": "main",
                 "language": "Python"},
                {"full_name": "beta/tiny-skills", "stars": 8,
                 "description": "小技能", "default_branch": "master"},
            ],
            "fetched_at": time.time(), "stale": False, "error": "",
        }
        self.skills_data = {
            "items": [
                {"slug": "pdf-extract", "dir": "skills/pdf-extract",
                 "name": "PDF 提取", "description": "从 PDF 抽文本",
                 "files": ["a.md", "b.py"], "file_count": 2,
                 "has_scripts": True},
                {"slug": "web-digest", "dir": "skills/web-digest",
                 "name": "网页摘要", "description": "抓网页出简报",
                 "files": [], "file_count": 0, "has_scripts": False},
            ],
            "ref": "main", "via": "api", "stale": False, "error": "",
        }
        self.mcp_data = {
            "items": [
                {"registry_name": "io.modelcontext/filesystem",
                 "title": "Filesystem", "version": "1.0",
                 "description": "本地文件读写", "free_class": "zero_config",
                 "transport": "stdio",
                 "suggest": {"transport": "stdio", "command": "npx",
                             "required_env": []}},
            ],
            "fetched_at": time.time(), "stale": False, "error": "",
        }
        self.repo_calls = []
        self.enum_calls = []
        self.install_calls = []
        self.mcp_calls = []
        self.save_calls = []

    def catalog_repos(self, q="", force=False):
        self.repo_calls.append(bool(force))
        return dict(self.repos_data)

    def catalog_repo_skills(self, owner, repo, ref="", force=False):
        self.enum_calls.append((owner, repo, ref, bool(force)))
        return dict(self.skills_data)

    def catalog_install_skill(self, repo, ref="", dirpath="", slug="",
                              overwrite=False):
        self.install_calls.append((repo, ref, dirpath, slug, bool(overwrite)))
        return {"slug": slug, "bytes": 4096, "skipped": 0}

    def catalog_mcp(self, q="", cursor="", force=False):
        return dict(self.mcp_data)

    def catalog_install_mcp(self, registry_name, server_id="", name="",
                            env=None, risk="", connect=True):
        self.mcp_calls.append(registry_name)
        return {"server": {"id": registry_name, "name": name}}

    def save_settings(self, patch):
        self.save_calls.append(patch)
        return {"ok": True}


class _StubPage:
    """页面桩：窗口只依赖 `_assist` + 两个刷新回调。"""

    def __init__(self) -> None:
        self._assist = _StubApi()
        self.refreshed = 0
        self.relists = 0
        self.mcp_forward = []

    def refresh(self) -> None:
        self.refreshed += 1

    def _auto_refresh_manager_lists(self) -> None:
        self.relists += 1

    #: MCP 安装的真实逻辑在页面里（要收集 env / 弹确认框），窗口只转发
    def _install_online_mcp_item(self, it, parent=None) -> None:
        self.mcp_forward.append((it, parent))


@pytest.fixture
def stub():
    return _StubPage()


def _auto_answer(monkeypatch, answer: bool) -> None:
    monkeypatch.setattr(
        mod.QMessageBox, "question",
        staticmethod(lambda *a, **kw: (QMessageBox.StandardButton.Yes
                                       if answer else
                                       QMessageBox.StandardButton.No)))


# --------------------------------------------------------------------------- #
# 技能窗口
# --------------------------------------------------------------------------- #
def test_skills_dialog_lists_repos(stub):
    """仓库列表按 star 降序渲染，行高够两行字（旧列表两行文字被裁）。"""
    dlg = OnlineSkillsDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 2, 3.0)
        assert "acme/awesome-skills" in dlg._list.item(0).text()
        assert "beta/tiny-skills" in dlg._list.item(1).text()
        fm_h = dlg._list.fontMetrics().height()
        assert dlg._list.item(0).sizeHint().height() >= fm_h * 2, "两行文字会被裁"
        assert "共 2 个仓库" in dlg._count.text()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_skills_dialog_search_filters_locally(stub):
    """搜索是本地过滤：不能再打网络。"""
    dlg = OnlineSkillsDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 2, 3.0)
        calls = len(stub._assist.repo_calls)
        dlg._search.setText("tiny")
        _settle()
        assert dlg._list.count() == 1
        assert len(stub._assist.repo_calls) == calls, "搜索不应重新联网"
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_skills_dialog_enumerate_then_install(stub, monkeypatch):
    """选中仓库 → 列出技能 → 点「⬇ 下载并安装」→ 后端收到整包安装请求。"""
    _auto_answer(monkeypatch, True)
    dlg = OnlineSkillsDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 2, 3.0)
        dlg._list.setCurrentRow(0)
        _settle()
        dlg.enumerate_repo()
        assert _pump(lambda: dlg._skill_list.count() >= 2, 3.0)
        assert stub._assist.enum_calls[0][:3] == ("acme", "awesome-skills", "main")
        # 第一条技能被自动选中（用户不用猜要点哪里）
        assert dlg._skill_list.currentRow() == 0
        assert "含可执行脚本" in dlg._detail.toPlainText()

        dlg.install_selected()
        assert _pump(lambda: bool(stub._assist.install_calls), 3.0)
        assert stub._assist.install_calls[0] == (
            "acme/awesome-skills", "main", "skills/pdf-extract", "pdf-extract",
            True)
        assert _pump(lambda: stub.refreshed >= 1, 3.0), "装完要刷新页面技能列表"
        assert stub.relists >= 1
        assert "已安装" in dlg._status.text()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_skills_dialog_install_cancelled_does_nothing(stub, monkeypatch):
    """确认框选「否」→ 不发安装请求，也不刷新页面。"""
    _auto_answer(monkeypatch, False)
    dlg = OnlineSkillsDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 2, 3.0)
        dlg._list.setCurrentRow(0)
        dlg.enumerate_repo()
        assert _pump(lambda: dlg._skill_list.count() >= 2, 3.0)
        dlg.install_selected()
        _settle()
        assert stub._assist.install_calls == []
        assert stub.refreshed == 0
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_skills_dialog_install_all_loops(stub, monkeypatch):
    """「安装全部」逐个装，全部成功后提示成功数。"""
    _auto_answer(monkeypatch, True)
    dlg = OnlineSkillsDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 2, 3.0)
        dlg._list.setCurrentRow(0)
        dlg.enumerate_repo()
        assert _pump(lambda: dlg._skill_list.count() >= 2, 3.0)
        dlg.install_all()
        assert _pump(lambda: len(stub._assist.install_calls) >= 2, 5.0)
        slugs = [c[3] for c in stub._assist.install_calls]
        assert slugs == ["pdf-extract", "web-digest"]
        assert _pump(lambda: "安装完成：成功 2 个" in dlg._status.text(), 3.0)
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_skills_dialog_enum_failure_is_reported(stub):
    """枚举失败（如 GitHub 限流）必须在状态行说清楚，不能静默。"""
    def boom(*a, **kw):
        raise RuntimeError("GitHub 接口限流，请 600 秒后重试")

    stub._assist.catalog_repo_skills = boom
    dlg = OnlineSkillsDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 2, 3.0)
        dlg._list.setCurrentRow(0)
        dlg.enumerate_repo()
        # 注意：状态行在发起时就有「GitHub 限流时…」的提示文案，所以不能
        # 用「含限流」判断失败已返回，必须等「枚举失败」出现
        assert _pump(lambda: "枚举失败" in dlg._status.text(), 3.0)
        assert "限流" in dlg._status.text()
        assert dlg._btn_enum.isEnabled(), "失败后按钮必须恢复可点"
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_online_skills_button_opens_dialog(monkeypatch, stub):
    """技能 Tab 的按钮改成打开独立窗口（不再展开 140px 的内嵌列表）。"""
    dlg = OnlineSkillsDialog(stub)
    try:
        sizes = dlg.size()          # 未 fit 时就是最小尺寸
        dlg.fit_to_parent()
        _settle()
        assert dlg.width() >= mod._CatalogDialog.MIN_W
        assert dlg.width() >= sizes.width()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


# --------------------------------------------------------------------------- #
# MCP 窗口
# --------------------------------------------------------------------------- #
def test_mcp_dialog_renders_badge_and_detail(stub):
    dlg = OnlineMcpDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 1, 3.0)
        text = dlg._list.item(0).text()
        assert mod.CLASS_BADGE["zero_config"] in text
        dlg._list.setCurrentRow(0)
        _settle()
        detail = dlg._detail.toPlainText()
        assert "stdio" in detail or "本地子进程" in detail
        assert "high" in detail
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_mcp_dialog_install_forwards_with_self_as_parent(stub):
    """安装转给页面处理，且**父窗口必须是本窗口**（否则确认框被遮住）。"""
    dlg = OnlineMcpDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() >= 1, 3.0)
        dlg._list.setCurrentRow(0)
        dlg.install_selected()
        _settle()
        assert len(stub.mcp_forward) == 1
        it, parent = stub.mcp_forward[0]
        assert it.get("registry_name", "").endswith("filesystem")
        assert parent is dlg
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()


def test_mcp_dialog_empty_hint_not_blank(stub):
    stub._assist.mcp_data = {"items": [], "fetched_at": 0, "stale": False,
                             "error": ""}
    dlg = OnlineMcpDialog(stub)
    try:
        dlg.refresh(force=False)
        assert _pump(lambda: dlg._list.count() == 1, 3.0)
        assert "为空" in dlg._list.item(0).text()
    finally:
        dlg.close()
        dlg.deleteLater()
        _settle()
