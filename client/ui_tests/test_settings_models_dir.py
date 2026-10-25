# -*- coding: utf-8 -*-
"""
设置页「模型目录」与原生对话框回归测试（2026-10-25）。

背景：
    1) 用户反馈「模型库请获取设置中的模型目录中的模型」——模型目录改了以后
       模型库还列旧目录，根因是后端 MODELS_DIR 在启动时从环境变量读成常量，
       光保存 config.json 不重启进程就不生效。
    2) crash.log 里 `0x8001010E RPC_E_WRONG_THREAD` 是 COM 套间错误：
       QFileDialog / QColorDialog 的**静态便捷方法**在 Windows 上走系统原生
       对话框，冻结包里一调用就把进程带走。这两处必须 DontUseNativeDialog。

覆盖：
    - 模型目录区有「浏览…」按钮，且走自绘对话框（断言 DontUseNativeDialog）；
    - 取色器同样走自绘对话框（回归：曾用 QColorDialog.getColor 静态方法）；
    - 目录不存在 / 空目录 / 有 gguf 三种提示文案；
    - 改模型目录保存后触发后端重启，只改背景色则不重启。
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (QApplication, QFileDialog, QColorDialog,  # noqa: E402
                               QDialog)

app = QApplication.instance() or QApplication(sys.argv)

from app_config import Config  # noqa: E402
from ui.pages import settings_page as sp_mod  # noqa: E402
from ui.pages.settings_page import SettingsPage  # noqa: E402


class _FakeApi:
    pass


class _FakeLauncher:
    def __init__(self):
        self.stopped = 0
        self.started = 0

    def stop(self):
        self.stopped += 1

    def start(self):
        self.started += 1
        return True


def _make_page(tmp_path, monkeypatch):
    cfg = Config()
    cfg.models_dir = str(tmp_path)
    # 隔离：save() 默认写模块级 CONFIG_PATH（client/config.json，开发态真实
    # 配置）。不隔离的话每次跑测试都把 tmp_path 写进真配置，之后
    # python main.py 会读到不存在的 models_dir（曾经就这么脏了）。
    monkeypatch.setattr("app_config.CONFIG_PATH",
                        str(tmp_path / "config.json"))
    page = SettingsPage(_FakeApi(), _FakeLauncher(), cfg)
    # 保存/重启不走真实弹窗
    monkeypatch.setattr(sp_mod.QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(sp_mod.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(page, "_restart_backend",
                        lambda models_dir_changed=False:
                        setattr(page, "_restart_flag", models_dir_changed))
    return page


def test_models_dir_row_has_browse_button(tmp_path, monkeypatch):
    """模型目录旁边要有「浏览…」，否则用户只能手打长路径。"""
    page = _make_page(tmp_path, monkeypatch)
    assert page._browse_btn.text().startswith("浏览")
    assert page._models_dir.text() == str(tmp_path)


def test_browse_uses_non_native_dialog(tmp_path, monkeypatch):
    """浏览框必须是自绘的：原生对话框会以 0x8001010E 杀进程。"""
    page = _make_page(tmp_path, monkeypatch)
    seen = {}

    class _FakeFd:
        # 代码里写的是 QFileDialog.Option.xxx / FileMode.xxx（模块级名字），
        # 替换成假类后这些枚举要原样转过去，否则直接 AttributeError
        Option = QFileDialog.Option
        FileMode = QFileDialog.FileMode
        DialogLabel = QFileDialog.DialogLabel

        def __init__(self, *a, **kw):
            seen["init"] = True

        def setOption(self, opt, on=True):
            seen[str(opt)] = on

        def setFileMode(self, mode):
            seen["mode"] = mode

        def setLabelText(self, *a):
            pass

        def exec(self):
            return 0  # 取消

        def selectedFiles(self):
            return []

    monkeypatch.setattr(sp_mod, "QFileDialog", _FakeFd)
    page._browse_models_dir()
    assert seen.get("init"), "应实例化 QFileDialog（而非静态方法）"
    keys = " ".join(seen.keys())
    assert "DontUseNativeDialog" in keys, "必须显式关闭原生对话框"


def test_browse_writes_picked_dir(tmp_path, monkeypatch):
    """选中目录后写回输入框并刷新提示。"""
    page = _make_page(tmp_path, monkeypatch)
    picked = tmp_path / "picked"
    picked.mkdir()

    class _FakeFd:
        Option = QFileDialog.Option
        FileMode = QFileDialog.FileMode
        DialogLabel = QFileDialog.DialogLabel

        def __init__(self, *a, **kw):
            pass

        def setOption(self, *a, **kw):
            pass

        def setFileMode(self, *a, **kw):
            pass

        def setLabelText(self, *a, **kw):
            pass

        def exec(self):
            return QDialog.DialogCode.Accepted

        def selectedFiles(self):
            return [str(picked)]

    monkeypatch.setattr(sp_mod, "QFileDialog", _FakeFd)
    monkeypatch.setattr(sp_mod, "QDialog", QDialog)
    page._browse_models_dir()
    assert page._models_dir.text() == str(picked)


def test_color_dialog_is_non_native(tmp_path, monkeypatch):
    """取色器回归：QColorDialog.getColor 静态方法 = 原生框 = 崩溃。"""
    page = _make_page(tmp_path, monkeypatch)
    seen = {}

    class _FakeCd:
        ColorDialogOption = QColorDialog.ColorDialogOption

        def __init__(self, *a, **kw):
            seen["init"] = True

        def setOption(self, opt, on=True):
            seen[str(opt)] = on

        def setWindowTitle(self, *a):
            pass

        def exec(self):
            return QDialog.DialogCode.Rejected

        def selectedColor(self):
            return None

    monkeypatch.setattr(sp_mod, "QColorDialog", _FakeCd)
    page._on_pick_color()
    assert seen.get("init"), "应实例化 QColorDialog（不能用静态便捷方法）"
    assert "DontUseNativeDialog" in " ".join(seen.keys())


def test_models_hint_missing_dir(tmp_path, monkeypatch):
    page = _make_page(tmp_path, monkeypatch)
    page._models_dir.setText(str(tmp_path / "nope"))
    text = page._models_hint.text()
    assert "不存在" in text


def test_models_hint_counts_gguf(tmp_path, monkeypatch):
    (tmp_path / "a.gguf").write_bytes(b"1")
    (tmp_path / "b.gguf").write_bytes(b"2")
    (tmp_path / "readme.txt").write_text("x")
    page = _make_page(tmp_path, monkeypatch)
    page._refresh_models_hint()
    text = page._models_hint.text()
    assert "2 个模型" in text
    assert "a.gguf" in text


def test_models_hint_empty_dir(tmp_path, monkeypatch):
    page = _make_page(tmp_path, monkeypatch)
    page._refresh_models_hint()
    assert "还没有" in page._models_hint.text()


def test_save_with_new_models_dir_restarts_backend(tmp_path, monkeypatch):
    """模型目录变了必须重启后端，否则 LLM_MODELS_DIR 还是旧值。"""
    page = _make_page(tmp_path, monkeypatch)
    newdir = tmp_path / "another"
    newdir.mkdir()
    page._models_dir.setText(str(newdir))
    page._on_save()
    assert getattr(page, "_restart_flag", None) is True
    assert page.config.models_dir == str(newdir)


def test_save_theme_only_does_not_restart(tmp_path, monkeypatch):
    """只改外观不该把正在推理的后端重启掉。"""
    page = _make_page(tmp_path, monkeypatch)
    page._select_bg("#ffffff")
    page._on_save()
    assert getattr(page, "_restart_flag", "missing") == "missing"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-q"])
