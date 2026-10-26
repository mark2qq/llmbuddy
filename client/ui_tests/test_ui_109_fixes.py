# -*- coding: utf-8 -*-
"""1.0.9 UI 修复单测（不依赖页面夹具，直接测控件与数据模块）。

覆盖用户 2026-10-26 提的三条界面问题：
    ① 助理消息（StepLine）居中 → 改左对齐 + 按内容宽度自动换行；
    ② 用户气泡「几个字就折行」→ 宽度按内容区宽度算 + 留字宽余量；
    ③ 线上模型要「免费」标识 + 悬停提示（模型介绍 / 收费口径）。
另外覆盖「思考过程超长拖死布局」的截断保护与崩溃诊断设施的落盘。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout  # noqa: E402

from core import model_info  # noqa: E402
from ui.widgets.assistant_cards import (ChatBubble, StepLine,  # noqa: E402
                                        ThinkingBlock)

app = QApplication.instance() or QApplication(sys.argv)


# --------------------------------------------------------------------------- #
# ① StepLine 左对齐
# --------------------------------------------------------------------------- #
def test_stepline_is_left_aligned():
    """技能阶段提示必须左对齐（原实现居中，长句在中间折行、两侧留白）。"""
    line = StepLine("技能[ppt-master] plan: 正在拆解任务…")
    assert line.label.alignment() & Qt.AlignLeft
    assert not (line.label.alignment() & Qt.AlignHCenter)


def test_error_line_also_left_aligned_and_wraps():
    line = StepLine("出错了：模型没有返回正文", error=True)
    assert line.label.alignment() & Qt.AlignLeft
    assert line.label.wordWrap() is True


# --------------------------------------------------------------------------- #
# ② 气泡宽度：按内容区宽度算，短句不折行
# --------------------------------------------------------------------------- #
def _host(width: int):
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    # 固定尺寸：顶层控件带布局时 show() 会被撑到 sizeHint，
    # 不固定就测不出「内容区宽度受限」的场景
    host.setFixedSize(width, 600)
    return host, lay


def test_user_bubble_short_text_single_line():
    """「继续完成任务」这种 6 字必须一行放下（原实现折成 6+1 两行）。"""
    host, lay = _host(900)
    bubble = ChatBubble("user", "继续完成任务")
    lay.addWidget(bubble)
    host.show()
    app.processEvents()
    fm = bubble.label.fontMetrics()
    need = fm.horizontalAdvance("继续完成任务")
    assert bubble.label.width() >= need, "气泡宽度不足以放下整行文字"
    assert bubble.label.width() <= bubble.MAX_WIDTH


def test_bubble_width_capped_by_content_area():
    """长文本封顶到内容区可用宽度（不是写死常量），保证能自动换行。"""
    host, lay = _host(420)
    long_text = "这是一段很长的说明文字，" * 12
    bubble = ChatBubble("assistant", long_text)
    lay.addWidget(bubble)
    host.show()
    app.processEvents()
    assert bubble.label.width() <= int(420 * 0.92)
    assert bubble.label.width() >= 220


def test_bubble_clear_resets_text():
    bubble = ChatBubble("assistant", "半截内容")
    bubble.clear()
    assert bubble.label.text() == ""
    bubble.append_text("新内容")
    assert bubble._buf == "新内容"


# --------------------------------------------------------------------------- #
# ③ 思考过程超长截断（防布局拖死 / 假死）
# --------------------------------------------------------------------------- #
def test_thinking_block_truncates_long_text():
    block = ThinkingBlock()
    block.append("思" * (ThinkingBlock.MAX_CHARS + 500))
    block.finish()
    shown = block.body.text()
    assert "前文已省略" in shown
    assert len(shown) < ThinkingBlock.MAX_CHARS + 200


def test_thinking_block_clear():
    block = ThinkingBlock()
    block.append("思考中")
    block.clear()
    assert block.body.text() == ""
    assert block._buf == ""


# --------------------------------------------------------------------------- #
# ④ 免费标识 + 悬停提示
# --------------------------------------------------------------------------- #
def test_known_free_detection():
    assert model_info.is_free("Qwen/Qwen3-8B")
    assert model_info.is_free("deepseek-ai/DeepSeek-R1")
    assert not model_info.is_free("deepseek-ai/DeepSeek-V4-Flash")
    assert model_info.is_free("some-vendor/model-FREE")


def test_menu_label_marks_free():
    assert "免费" in model_info.menu_label("Qwen/Qwen3-8B")
    assert "免费" not in model_info.menu_label("deepseek-ai/DeepSeek-V4-Flash")
    # 本地模型不计费，但也不该标「免费」（语义是本地/不计费）
    assert "免费" not in model_info.menu_label("Qwen/Qwen3-8B", local=True)


def test_tooltip_has_intro_and_price():
    info = model_info.describe("deepseek-ai/DeepSeek-R1")
    tip = info["tooltip"]
    assert "能力：" in tip and "收费：" in tip
    assert "推理" in info["desc"]
    assert info["free"] is True
    paid = model_info.describe("deepseek-ai/DeepSeek-V4-Flash")
    assert paid["free"] is False
    assert "按量计费" in paid["price"]
    local = model_info.describe("qwen3-4b", local=True)
    assert "不计费" in local["price"]


def test_unknown_model_falls_back_gracefully():
    info = model_info.describe("vendor/unknown-model-x")
    assert info["family"] == "通用模型"
    assert info["tooltip"].startswith("vendor/unknown-model-x")
    assert "收费：" in info["tooltip"]
