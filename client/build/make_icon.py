# -*- coding: utf-8 -*-
"""
生成 llmBuddy 应用图标（client/build/make_icon.py）。

设计：圆角方形渐变底（靛蓝->紫）+ 白色机器人小伙伴头像
（圆角方头、两只眼睛、微笑、天线小圆、两侧耳机），
小尺寸下保留高对比轮廓，16px 仍可辨识。

产物（client/resources/）：
    llmbuddy.png      256x256（窗口/托盘图标源图）
    llmbuddy.ico      16/24/32/48/64/128/256（exe / 安装包图标）
"""
import os
import struct
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QGuiApplication, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QRadialGradient)

RES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "resources")


def draw_icon(size: int) -> QImage:
    """按目标尺寸绘制图标（几何按 256 基准等比缩放）。"""
    s = size / 256.0
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    # ---- 圆角方形渐变底 ----
    body = QRectF(6 * s, 6 * s, 244 * s, 244 * s)
    grad = QLinearGradient(body.topLeft(), body.bottomRight())
    grad.setColorAt(0.0, QColor("#4F6BFF"))
    grad.setColorAt(0.55, QColor("#6D5CF6"))
    grad.setColorAt(1.0, QColor("#A855F7"))
    path = QPainterPath()
    path.addRoundedRect(body, 56 * s, 56 * s)
    p.fillPath(path, grad)

    # 左上柔光（提升质感，小尺寸自然弱化）
    glow = QRadialGradient(QPointF(70 * s, 60 * s), 180 * s)
    glow.setColorAt(0.0, QColor(255, 255, 255, 46))
    glow.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(path, glow)

    # ---- 天线（头前先画，被头部压住根部） ----
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(QRectF(124 * s, 40 * s, 8 * s, 34 * s), 4 * s, 4 * s)
    p.setBrush(QColor("#FFD166"))
    p.drawEllipse(QPointF(128 * s, 36 * s), 13 * s, 13 * s)

    # ---- 两侧耳机 ----
    p.setBrush(QColor("#FFFFFF"))
    p.drawRoundedRect(QRectF(28 * s, 112 * s, 22 * s, 60 * s), 11 * s, 11 * s)
    p.drawRoundedRect(QRectF(206 * s, 112 * s, 22 * s, 60 * s), 11 * s, 11 * s)

    # ---- 头部 ----
    head = QRectF(50 * s, 74 * s, 156 * s, 132 * s)
    p.setBrush(QColor("#FFFFFF"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(head, 34 * s, 34 * s)

    # ---- 眼睛（大圆角，表情友好） ----
    p.setBrush(QColor("#252A3F"))
    eye_w, eye_h = 26 * s, 32 * s
    p.drawRoundedRect(QRectF(90 * s, 118 * s, eye_w, eye_h), 11 * s, 11 * s)
    p.drawRoundedRect(QRectF(140 * s, 118 * s, eye_w, eye_h), 11 * s, 11 * s)
    # 眼神高光
    p.setBrush(QColor(255, 255, 255, 200))
    p.drawEllipse(QRectF(97 * s, 123 * s, 9 * s, 9 * s))
    p.drawEllipse(QRectF(147 * s, 123 * s, 9 * s, 9 * s))

    # ---- 微笑 ----
    pen = QPen(QColor("#252A3F"))
    pen.setWidthF(8 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    smile = QPainterPath()
    smile.moveTo(104 * s, 168 * s)
    smile.quadTo(128 * s, 186 * s, 152 * s, 168 * s)
    p.drawPath(smile)

    # ---- 脸颊（淡粉圆点，伙伴感） ----
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 138, 158, 150))
    p.drawEllipse(QPointF(82 * s, 172 * s), 8 * s, 6 * s)
    p.drawEllipse(QPointF(174 * s, 172 * s), 8 * s, 6 * s)

    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def write_ico(path: str, images):
    """写 ICO（Vista+ 支持 PNG 压缩条目）。images: [(size, png_bytes)]"""
    n = len(images)
    header = struct.pack("<HHH", 0, 1, n)
    entries = b""
    payload = b""
    offset = 6 + 16 * n
    for size, data in images:
        w = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        payload += data
    with open(path, "wb") as f:
        f.write(header + entries + payload)


def main() -> None:
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841
    os.makedirs(RES_DIR, exist_ok=True)

    big = draw_icon(256)
    png_path = os.path.join(RES_DIR, "llmbuddy.png")
    big.save(png_path, "PNG")
    print("写入", png_path)

    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [(sz, png_bytes(draw_icon(sz))) for sz in sizes]
    ico_path = os.path.join(RES_DIR, "llmbuddy.ico")
    write_ico(ico_path, images)
    print(f"写入 {ico_path}（{len(sizes)} 种尺寸）")


if __name__ == "__main__":
    main()
