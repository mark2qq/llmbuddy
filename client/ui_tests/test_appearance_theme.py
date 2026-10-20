# -*- coding: utf-8 -*-
"""
背景色主题回归测试（2026-10-20 用户需求：设置模块可调背景色，保存立即生效）。

验证点：
    1. 默认/空背景 -> 基线深色样式（无覆盖，零行为变化）；
    2. 亮色背景（纯白/浅灰）-> 追加浅色覆盖（白色卡片、深色文字）；
    3. 自定义暗色背景 -> 仅替换主背景，保留深色控件；
    4. 颜色规范化与亮度判定；
    5. Config.background_color 序列化往返；
    6. 设置页选中色块 -> 保存写入 config（默认深色存空串）；
    7. apply_stylesheet() 立即把新样式应用到 QApplication。
"""
import os
import sys

_CLIENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _CLIENT_DIR in sys.path:
    sys.path.remove(_CLIENT_DIR)
sys.path.insert(0, _CLIENT_DIR)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from app_config import Config  # noqa: E402
from core.theme import (  # noqa: E402
    DEFAULT_DARK_BG, apply_stylesheet, build_stylesheet, is_light,
    normalize_color)
from ui.pages.settings_page import SettingsPage  # noqa: E402


class _FakeApi:
    pass


class _FakeLauncher:
    pass


# ---------------------------------------------------------------------------
# 主题生成
# ---------------------------------------------------------------------------
def test_default_background_is_baseline():
    """空背景/默认深色 -> 基线样式，不含任何覆盖段。"""
    for bg in ("", DEFAULT_DARK_BG, DEFAULT_DARK_BG.upper()):
        css = build_stylesheet(bg)
        assert css, "基线样式不应为空"
        assert "浅色主题覆盖" not in css
        assert "自定义暗色背景覆盖" not in css


def test_light_background_appends_light_override():
    """白色背景 -> 浅色覆盖：背景为白、文字转深色、卡片为白。"""
    css = build_stylesheet("#ffffff")
    assert "浅色主题覆盖" in css
    assert "#ffffff" in css
    assert "color: #1f2430" in css, "浅色主题必须有深色文字"
    assert "QGroupBox#Card { background-color: #ffffff" in css
    # 基线仍保留在前（覆盖是追加，不是替换）
    assert css.index("QGroupBox#Card") < css.index("浅色主题覆盖")


def test_custom_dark_background_only_overrides_base():
    """自定义暗色 -> 仅替换主背景，不启用浅色覆盖（文字仍为浅色）。"""
    css = build_stylesheet("#101820")
    assert "自定义暗色背景覆盖" in css
    assert "#101820" in css
    assert "浅色主题覆盖" not in css


def test_color_helpers():
    assert normalize_color("fff") == "#ffffff"
    assert normalize_color("#ABCDEF") == "#abcdef"
    assert normalize_color("") == ""
    assert normalize_color("bad!") == ""
    assert is_light("#ffffff") is True
    assert is_light("#101820") is False


def test_apply_stylesheet_takes_effect_immediately():
    """apply_stylesheet 之后 QApplication 已持有新样式（立即生效）。"""
    apply_stylesheet(app, "#ffffff")
    assert "浅色主题覆盖" in app.styleSheet()
    apply_stylesheet(app, "")
    assert "浅色主题覆盖" not in app.styleSheet()


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------
def test_config_background_color_roundtrip(tmp_path):
    cfg = Config()
    assert cfg.background_color == "", "默认应为空（沿用深色基线）"
    cfg.background_color = "#eef1f7"
    path = str(tmp_path / "config.json")
    cfg.save(path)
    loaded = Config.load(path)
    assert loaded.background_color == "#eef1f7"


def test_old_config_without_field_defaults_to_empty(tmp_path):
    """旧版 config.json 无 background_color 字段 -> 默认空，不报错。"""
    import json
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"gateway_port": 8000, "n_ctx": 4096}, f)
    cfg = Config.load(path)
    assert cfg.background_color == ""


# ---------------------------------------------------------------------------
# 设置页交互
# ---------------------------------------------------------------------------
def test_settings_page_shows_saved_background():
    cfg = Config()
    cfg.background_color = "#ffffff"
    page = SettingsPage(_FakeApi(), _FakeLauncher(), cfg)
    assert page._bg_color == "#ffffff"
    assert page._bg_btns["#ffffff"].isChecked()
    assert page._bg_value.text() == "#ffffff"


def test_settings_page_custom_color_and_save(monkeypatch):
    """自定义色保存 -> 写入 config.background_color 并发出变更信号。"""
    cfg = Config()
    page = SettingsPage(_FakeApi(), _FakeLauncher(), cfg)
    emitted = []
    page.config_changed.connect(lambda: emitted.append(True))
    page._select_bg("#e9f3e6")
    assert page._bg_color == "#e9f3e6"
    assert page._bg_btns["#e9f3e6"].isChecked()

    monkeypatch.setattr(cfg, "save", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    page._on_save()
    assert cfg.background_color == "#e9f3e6"
    assert emitted, "保存后应发出 config_changed（主窗口据此立即生效）"


def test_settings_page_default_dark_saved_as_empty(monkeypatch):
    cfg = Config()
    cfg.background_color = "#ffffff"
    page = SettingsPage(_FakeApi(), _FakeLauncher(), cfg)
    page._select_bg(DEFAULT_DARK_BG)
    assert page._bg_value.text() == "默认深色"
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: None))
    page._on_save()
    assert cfg.background_color == "", "默认深色应存空串（兼容旧配置）"
