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
#
# 1.0.10 修正了判定口径：免费 = 官方价格页里输入与输出都为 0 元。
# 旧实现手工维护 KNOWN_FREE，把 Qwen3-14B（¥0.5→¥2）、DeepSeek-R1（¥4→¥16）
# 都标成了免费，用户拿价格页截图反馈后改掉。以下断言按**真实价格**写。
# --------------------------------------------------------------------------- #
def test_known_free_detection():
    assert model_info.is_free("Qwen/Qwen3-8B")                  # 0 / 0
    assert model_info.is_free("zai-org/GLM-5.3-Flash")          # 0 / 0
    assert model_info.is_free("deepseek-ai/DeepSeek-R1-0528-Qwen3-8B")
    # 用户反馈的错标：Qwen3-14B 官方 ¥0.5 输入 / ¥2 输出，不是免费
    assert not model_info.is_free("Qwen/Qwen3-14B")
    assert model_info.price_of("Qwen/Qwen3-14B") == (0.5, 2)
    # DeepSeek-R1 / V3 一直是收费的（¥4→16 / ¥2→8），旧清单也标错了
    assert not model_info.is_free("deepseek-ai/DeepSeek-R1")
    assert not model_info.is_free("deepseek-ai/DeepSeek-V3")
    assert not model_info.is_free("deepseek-ai/DeepSeek-V4-Flash")
    # 不再用「名字里带 free 就算免费」的启发式（那是误标来源之一）
    assert not model_info.is_free("some-vendor/model-FREE")
    # 价格表没收录 → 保守按收费处理
    assert not model_info.is_free("unknown/model-xyz")


def test_menu_label_marks_free():
    assert "免费" in model_info.menu_label("Qwen/Qwen3-8B")
    # 收费模型显示真实单价，一眼可辨，不用点开 tooltip
    paid_label = model_info.menu_label("Qwen/Qwen3-14B")
    assert "免费" not in paid_label
    assert "¥0.5" in paid_label and "2" in paid_label
    # 本地模型：标「本地·免费」（确实不计费，且与线上免费的语义区分开）
    assert "本地" in model_info.menu_label("Qwen/Qwen3-8B", local=True)
    # 免 Key 端点
    assert "免 Key" in model_info.menu_label("openai-fast", keyless=True)


def test_tooltip_has_intro_and_price():
    info = model_info.describe("deepseek-ai/DeepSeek-R1")
    tip = info["tooltip"]
    assert "能力：" in tip and "收费：" in tip
    assert "推理" in info["desc"]
    # R1 是 ¥4→¥16，不是免费
    assert info["free"] is False
    assert "按量计费" in info["price"]
    assert "4" in info["price"] and "16" in info["price"]
    free = model_info.describe("Qwen/Qwen3-8B")
    assert free["free"] is True
    assert "免费" in free["price"]
    paid = model_info.describe("deepseek-ai/DeepSeek-V4-Flash")
    assert paid["free"] is False
    assert "按量计费" in paid["price"]
    local = model_info.describe("qwen3-4b", local=True)
    assert "不计费" in local["price"]
    keyless = model_info.describe("openai-fast", keyless=True)
    assert "无需 API Key" in keyless["price"]


def test_unknown_model_falls_back_gracefully():
    info = model_info.describe("vendor/unknown-model-x")
    assert info["family"] == "通用模型"
    assert info["tooltip"].startswith("vendor/unknown-model-x")
    assert "收费：" in info["tooltip"]
