# -*- coding: utf-8 -*-
"""
界面主题（theme.py）——背景色可配置 + 立即生效。

设计：
    styles.qss 是深色基线主题（#0f1117 背景）。用户可在「设置 → 界面外观」
    选择背景色（深色 / 浅灰 / 纯白 / 护眼绿 / 任意自定义色）。应用方式为
    「基线 QSS + 覆盖 QSS」：

        background_color 为空或 #0f1117（默认）
            -> 直接用基线样式（零改动，行为与之前完全一致）
        亮色背景（相对亮度 >= 150，如白色 / 浅灰）
            -> 追加浅色覆盖：白色卡片、深色文字、浅色边框，保证可读性
        其它暗色背景
            -> 仅把窗口/页面底色替换为用户所选色，保留深色主题的控件与文字

    apply_stylesheet(app, color) 会重新 setStyleSheet，Qt 会重新 polish 全部
    控件，因此保存设置后立即生效（无需重启客户端）。
"""
import os
from typing import Optional

from app_config import resource_path

# 默认（深色）背景色：与 styles.qss 的 QWidget 背景一致
DEFAULT_DARK_BG = "#0f1117"

# 设置页预设色块（名称, 颜色）——浅色系用于「不喜欢纯黑背景」的常见诉求
PRESET_BACKGROUNDS = [
    ("深色", DEFAULT_DARK_BG),
    ("浅灰", "#eef1f7"),
    ("纯白", "#ffffff"),
    ("护眼绿", "#e9f3e6"),
]

_base_cache: Optional[str] = None


def normalize_color(value: str) -> str:
    """规范化颜色：#RGB / #RRGGBB / 空 -> #rrggbb 或 ""（非法）。"""
    v = (value or "").strip()
    if not v:
        return ""
    if not v.startswith("#"):
        v = "#" + v
    hexpart = v[1:]
    if len(hexpart) == 3:
        hexpart = "".join(c * 2 for c in hexpart)
    if len(hexpart) != 6:
        return ""
    try:
        int(hexpart, 16)
    except ValueError:
        return ""
    return "#" + hexpart.lower()


def _rgb(color: str):
    c = normalize_color(color)
    if not c:
        return None
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def luminance(color: str) -> float:
    """相对亮度（0-255，YIQ 经验公式）；非法颜色返回 0。"""
    rgb = _rgb(color)
    if rgb is None:
        return 0.0
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def is_light(color: str) -> bool:
    """是否为亮色背景（决定是否启用浅色文字/控件覆盖）。"""
    return luminance(color) >= 150.0


def read_base_stylesheet() -> str:
    """读取基线深色样式（带进程内缓存）。"""
    global _base_cache
    if _base_cache is not None:
        return _base_cache
    path = resource_path(os.path.join("resources", "styles.qss"))
    text = ""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            text = ""
    _base_cache = text
    return text


# ---------------------------------------------------------------------------
# 浅色覆盖（__BG__ 由用户所选背景色替换）
# ---------------------------------------------------------------------------
_LIGHT_OVERRIDE = """
/* ============ 浅色主题覆盖（用户选择亮色背景时追加） ============ */
QWidget { background-color: __BG__; color: #1f2430; }
QMainWindow, QDialog { background-color: __BG__; }
QCheckBox, QRadioButton { background: transparent; color: #232a38; }

/* 侧栏 */
#NavPanel { background-color: #eef1f7; border-right: 1px solid #dbe0ea; }
#NavTitle { background: transparent; }
#NavTitleLabel { color: #1f2430; }
#NavVersionLabel { color: #7b8496; }
#NavButton { color: #3a4152; }
#NavButton:hover { background-color: #e2e7f0; color: #12161f; }
#NavButton:checked { background-color: #dbe4fb; color: #1b3a8f;
                     border-left: 3px solid #4a6cf7; }

/* 卡片 */
QGroupBox#Card { background-color: #ffffff; border: 1px solid #dde2ec; }
QGroupBox#Card::title { color: #5a6275; }

/* 按钮 */
QPushButton { background-color: #f3f5fa; color: #232a38;
              border: 1px solid #d5dbe7; }
QPushButton:hover { background-color: #e8ecf5; border-color: #c3cbdb; }
QPushButton:pressed { background-color: #dfe4ef; }
QPushButton:disabled { background-color: #f2f4f8; color: #aab1c0;
                       border-color: #e3e7f0; }
#PrimaryBtn { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
              stop:0 #4a6cf7, stop:1 #7c4dff); color: #ffffff; border: none; }
#PrimaryBtn:hover { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                     stop:0 #5b7bff, stop:1 #8d63ff); }
#DangerBtn { background-color: #fde8ee; border: 1px solid #f3c2d2;
             color: #c02a55; }
#DangerBtn:hover { background-color: #fbd9e3; }
#DownloadBtn { background-color: transparent; color: #3a5bd9; }
#DownloadBtn:hover { background-color: rgba(74, 108, 247, 0.10);
                     color: #2c47b8; }
#CellBtn { background-color: transparent; color: #2b3242;
           border: 1px solid #d5dbe7; }
#CellBtn:hover { background-color: #eef1f7; border-color: #c3cbdb; }
#LocalBadge { background-color: rgba(31, 143, 87, 0.10); color: #1f8f57;
              border: 1px solid rgba(31, 143, 87, 0.30); }
#TinyDangerBtn { background-color: #fde8ee; border: 1px solid #f3c2d2;
                 color: #c02a55; }
#TinyDangerBtn:hover { background-color: #fbd9e3; }
#ModelAddBtn { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
               stop:0 #4a6cf7, stop:1 #7c4dff); color: #ffffff; border: none; }
#ModelDropBtn { background-color: #f3f5fa; color: #232a38;
                border: 1px solid #d5dbe7; }
#ModelDropBtn:hover { background-color: #e8ecf5; border-color: #c3cbdb; }
#ModelChip { background-color: #f7f9fd; border: 1px solid #e1e6f0;
             color: #2b3242; }
#SendCircleBtn { background-color: #dbe4fb; }
#SendCircleBtn:hover { background-color: #cdd9fa; }
#RowBtn { background-color: #4a6cf7; color: #ffffff; }
#RowBtn:hover { background-color: #5b7bff; }
#RowBtn:disabled { background-color: #eef1f7; color: #a9b0bf; }
#RowDangerBtn { background-color: #fde8ee; color: #c02a55;
                border: 1px solid #f3c2d2; }
#RowDangerBtn:hover { background-color: #fbd9e3; }
#RowCancelBtn { background-color: #f3f5fa; color: #2b3242;
                border: 1px solid #d5dbe7; }
#RowCancelBtn:hover { background-color: #e8ecf5; }

/* 输入类 */
QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit {
    background-color: #ffffff; color: #1f2430; border: 1px solid #d5dbe7;
}
QLineEdit:read-only, QTextEdit:read-only { color: #4b5365; }
QComboBox QAbstractItemView { background-color: #ffffff; color: #1f2430;
    selection-background-color: #e2e9fb; border: 1px solid #d5dbe7; }
QSpinBox::up-button, QSpinBox::down-button { background-color: #eef1f7; }

/* 标签 */
QLabel { color: #2b3242; }
#InfoLabel { color: #3b4354; }
#MutedLabel { color: #6b7385; }

/* 状态栏 */
#StatusBar { background-color: #eef1f7; border-top: 1px solid #dbe0ea; }
#StatusBar QLabel { color: #5a6275; }

/* 表格 / 列表 */
QTableWidget, QListWidget { background-color: #ffffff;
    border: 1px solid #d5dbe7; gridline-color: #e8ecf4; }
QTableWidget::item, QListWidget::item { border-bottom: 1px solid #eef1f7; }
QTableWidget::item:selected, QListWidget::item:selected {
    background-color: #e2e9fb; color: #12161f; }
QHeaderView::section { background-color: #f4f6fb; color: #5a6275;
    border-bottom: 1px solid #dbe0ea; }

/* 对话气泡 */
#UserBubble { background-color: #4a6cf7; color: #ffffff; }
#AssistantBubble { background-color: #f4f6fb; color: #1f2430;
                   border: 1px solid #dde2ec; }
#ChatInput { background-color: #ffffff; }
#ChatScroll { background-color: __BG__; }

/* 已加载模型卡片 */
#LoadedScroll { background: transparent; border: none; }
#LoadedCard { background-color: #f7f9fd; border: 1px solid #e1e6f0; }
#LoadedName { color: #1f2430; }

/* 进度条 / 滚动条 */
QProgressBar { background-color: #eef1f7; border: 1px solid #d5dbe7;
               color: #3b4354; }
QScrollBar:vertical { background: transparent; }
QScrollBar::handle:vertical { background-color: rgba(35, 45, 65, 18%); }
QScrollBar::handle:vertical:hover { background-color: rgba(35, 45, 65, 34%); }
#OverlayScrollBar { background: transparent; }
#OverlayScrollBar::handle:vertical {
    background-color: rgba(35, 45, 65, 22%); }
#OverlayScrollBar::handle:vertical:hover {
    background-color: rgba(35, 45, 65, 40%); }

/* 日志 / 测试结果 */
#LogView { background-color: #fbfcfe; color: #2b3242;
           border: 1px solid #dbe0ea; }
#TestResult { background-color: #fbfcfe; color: #2b3242;
              border: 1px solid #dbe0ea; }

/* 菜单 */
QMenu { background-color: #ffffff; border: 1px solid #dbe0ea; }
QMenu::item { color: #232a38; }
QMenu::item:selected { background-color: #e2e9fb; }
QMenu#ModelMenu { background-color: #ffffff; border: 1px solid #dbe0ea; }
QMenu#ModelMenu::item { color: #232a38; }
QMenu#ModelMenu::item:selected { background-color: #e2e9fb; }
QMenu#ModelMenu::item:disabled { color: #8a93a8; }
QMenu#ModelMenu::separator { background-color: #e5e9f2; }
QMenu#TrayMenu { background-color: #ffffff; border: 1px solid #dbe0ea; }
QMenu#TrayMenu::item { color: #232a38; }
QMenu#TrayMenu::item:selected { background-color: #e2e9fb; }
QMenu#TrayMenu::item:disabled { color: #a9b0bf; }
QToolTip { background-color: #ffffff; color: #1f2430;
           border: 1px solid #d5dbe7; }
"""


def build_stylesheet(background_color: str = "") -> str:
    """按背景色构造完整样式表（基线 + 必要覆盖）。"""
    base = read_base_stylesheet()
    bg = normalize_color(background_color)
    if not bg or bg == DEFAULT_DARK_BG:
        # 默认深色：完全沿用基线，保证既有观感零变化
        return base
    if is_light(bg):
        return base + "\n" + _LIGHT_OVERRIDE.replace("__BG__", bg)
    # 自定义暗色：只替换主背景，保留深色主题的控件与文字
    return base + (
        "\n/* 自定义暗色背景覆盖 */\n"
        f"QWidget, QMainWindow, QDialog, #ChatScroll {{ background-color: {bg}; }}\n"
    )


def apply_stylesheet(app, background_color: str = "") -> None:
    """把主题应用到 QApplication（重复调用即「立即生效」）。"""
    if app is None:
        return
    app.setStyleSheet(build_stylesheet(background_color))
