# -*- coding: utf-8 -*-
"""
输入框粘贴图片（2026-10-26，用户需求）。

用户原话：「要有复制粘贴图片的功能，也就是截图后可以直接粘贴到这边对话框里
作为即将提交的信息内容」。

需求拆成三件事：
    1. **接得住** —— 截图后在输入框按 Ctrl+V，图片要能进来（原生 QTextEdit
       对 `image/*` 的 MIME 默认返回 False，粘贴等于什么都没发生）；
    2. **看得见** —— 粘进来的图片要在输入框上方显示缩略图，能看出是哪张、
       能单独删掉，不能像黑盒一样"不知道到底附没附上"；
    3. **发得出** —— 发送时图片已经落盘成文件，路径进消息正文（`@路径`），
       模型可用 fs_read / OCR 技能处理；同时路径随 payload 一起交给后端记录。

设计取舍：
    · 不做「把 base64 塞进消息正文」—— 正文会被落库进会话历史，几十 KB 的
       base64 会污染历史、还会把上下文窗口瞬间吃满（本机 4B/32B 模型尤其明显）。
    · 落盘位置 `%LOCALAPPDATA%\\llmBuddy\\assistant\\attachments\\YYYYMMDD\\`，
      与后端的会话目录同源，用户能在「打开工作区」附近找到原图。
    · 粘贴走**文件**而不是内存 QImage：QImage 没有文件名，落盘后才有能被
      `fs_read` 引用的稳定路径。
"""
from __future__ import annotations

import os
import time
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QTextEdit, QWidget)

#: 粘贴/拖入时认作图片的扩展名（从资源管理器复制文件到输入框也走这条路）
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}

#: 缩略图边长（px）
THUMB = 42


# --------------------------------------------------------------------------- #
# 落盘
# --------------------------------------------------------------------------- #
def attachments_dir() -> str:
    """本次运行的图片落盘目录（按日期分子目录，避免单目录堆几千张）。"""
    import app_config  # 延迟导入：widget 单测里不需要完整客户端环境

    if app_config.is_frozen():
        base = os.path.join(app_config._user_data_dir(), "assistant", "attachments")
    else:
        # 开发态不往仓库里写图：放到系统临时目录
        import tempfile
        base = os.path.join(tempfile.gettempdir(), "llmBuddy-attachments")
    day = time.strftime("%Y%m%d")
    path = os.path.join(base, day)
    os.makedirs(path, exist_ok=True)
    return path


def save_image(img: QImage, prefix: str = "paste") -> str:
    """把 QImage 存成 PNG 并返回绝对路径。失败抛 OSError（调用方负责提示）。"""
    if not isinstance(img, QImage) or img.isNull():
        raise OSError("剪贴板里没有可用的图片")
    safe_prefix = "".join(c for c in str(prefix or "paste") if c.isalnum() or c in "-_") or "paste"
    name = f"{safe_prefix}-{time.strftime('%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
    path = os.path.join(attachments_dir(), name)
    if not img.save(path, "PNG"):
        raise OSError(f"图片保存失败：{path}")
    return path


def _image_from_mime(md) -> Optional[QImage]:
    """从 QMimeData 里取一张 QImage：优先剪贴板位图，其次本地图片文件 URL。"""
    if md is None:
        return None
    try:
        if md.hasImage():
            data = md.imageData()
            if isinstance(data, QImage) and not data.isNull():
                return data
            if isinstance(data, QPixmap) and not data.isNull():
                return data.toImage()
            if data is not None:
                img = QImage(data) if not isinstance(data, str) else QImage(data)
                if isinstance(img, QImage) and not img.isNull():
                    return img
    except Exception:  # noqa: BLE001 —— 剪贴板内容千奇百怪，取不到就当没有
        pass
    if md.hasUrls():
        for url in md.urls():
            try:
                if not url.isLocalFile():
                    continue
                path = url.toLocalFile()
                if os.path.splitext(path)[1].lower() in IMAGE_EXTS:
                    img = QImage(path)
                    if not img.isNull():
                        return img
            except Exception:  # noqa: BLE001
                continue
    return None


# --------------------------------------------------------------------------- #
# 输入框：接得住粘贴的图片
# --------------------------------------------------------------------------- #
class PasteImageEdit(QTextEdit):
    """QTextEdit 子类：MIME 里带图片时**拦截**，发信号而不是插入文本。

    拦截点选在 `canInsertFromMimeData` / `insertFromMimeData` 而不是
    `eventFilter` 里判 Ctrl+V：这样 Ctrl+V、右键「粘贴」、拖拽图片三种入口
    都走同一套逻辑，不会漏。
    """

    image_pasted = Signal(object)   # QImage
    prompt_requested = Signal()     # 右键菜单「插入提示词…」被点
    memory_requested = Signal()     # 右键菜单「记忆…」被点

    def contextMenuEvent(self, ev) -> None:      # noqa: N802 (Qt 命名)
        """标准右键菜单 + 「插入提示词…」+「记忆…」。

        底栏不再放「📝 提示词」按钮（用户要求收紧底栏），提示词库入口改到这里：
        输入框右键 → 插入提示词，功能不失联，也不占底栏宽度。记忆同理（1.0.12）：
        它是「关于这个助理/这个空间长期记住什么」的配置，不属于任务操作。
        """
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        act = menu.addAction("插入提示词…")
        act.triggered.connect(self.prompt_requested.emit)
        act = menu.addAction("记忆…")
        act.setToolTip("查看 / 编辑长期记忆（全局 MEMORY.md、当前空间记忆、每日日志）")
        act.triggered.connect(self.memory_requested.emit)
        menu.exec(ev.globalPos())
        menu.deleteLater()

    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802 (Qt 命名)
        if _image_from_mime(source) is not None:
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        img = _image_from_mime(source)
        if img is not None:
            self.image_pasted.emit(img)
            return
        super().insertFromMimeData(source)

    def has_image_in_clipboard(self) -> bool:
        """给测试 / 右键菜单用：剪贴板里是否真有图片。"""
        from PySide6.QtWidgets import QApplication
        cb = QApplication.clipboard()
        return cb is not None and _image_from_mime(cb.mimeData()) is not None


# --------------------------------------------------------------------------- #
# 缩略图条
# --------------------------------------------------------------------------- #
class _Chip(QFrame):
    """单张附件：缩略图 + 文件名 + 右上角删除。"""

    removed = Signal(str)

    def __init__(self, path: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("AttachChip")
        self.path = path
        self.setToolTip(path)
        self.setCursor(Qt.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 3, 4, 3)
        lay.setSpacing(4)

        thumb = QLabel()
        thumb.setObjectName("AttachThumb")
        thumb.setFixedSize(THUMB, THUMB)
        thumb.setAlignment(Qt.AlignCenter)
        pix = QPixmap(path)
        if not pix.isNull():
            thumb.setPixmap(pix.scaled(THUMB, THUMB, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation))
        else:
            thumb.setText("图")
        lay.addWidget(thumb)

        size = ""
        if not pix.isNull():
            size = f"{pix.width()}×{pix.height()}"
        text = QLabel(f"{os.path.basename(path)}\n{size}".strip())
        text.setObjectName("AttachName")
        lay.addWidget(text)

        btn = QPushButton("×")
        btn.setObjectName("AttachDel")
        btn.setFixedSize(16, 16)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("移除这张图片（不会删除磁盘上的文件）")
        btn.clicked.connect(lambda _c=False: self.removed.emit(self.path))
        lay.addWidget(btn, 0, Qt.AlignTop)


class AttachmentBar(QWidget):
    """输入框上方的附件预览条：没有附件时整体隐藏（不占布局空间）。"""

    changed = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("AttachBar")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 2)
        self._lay.setSpacing(6)
        self._lay.addStretch(1)
        self.setVisible(False)

    # ---------------- 数据 ----------------
    def paths(self) -> List[str]:
        out: List[str] = []
        for i in range(self._lay.count()):
            w = self._lay.itemAt(i).widget()
            if isinstance(w, _Chip):
                out.append(w.path)
        return out

    def count(self) -> int:
        return len(self.paths())

    # ---------------- 操作 ----------------
    def add(self, path: str) -> bool:
        """加一张图；已在条里的同一路径不重复加（粘贴两次同一张只留一张）。"""
        path = str(path or "")
        if not path or not os.path.isfile(path):
            return False
        if path in self.paths():
            return False
        chip = _Chip(path, self)
        chip.removed.connect(self.remove)
        # 插在末尾 stretch 之前：stretch 保持在最后，卡片自然靠左
        self._lay.insertWidget(self._lay.count() - 1, chip)
        self.setVisible(True)
        self.changed.emit()
        return True

    def remove(self, path: str) -> None:
        for i in range(self._lay.count()):
            w = self._lay.itemAt(i).widget()
            if isinstance(w, _Chip) and w.path == path:
                self._lay.takeAt(i)
                w.setParent(None)
                w.deleteLater()
                break
        if self.count() == 0:
            self.setVisible(False)
        self.changed.emit()

    def clear(self) -> None:
        while True:
            chips = [self._lay.itemAt(i).widget() for i in range(self._lay.count())]
            chip = next((c for c in chips if isinstance(c, _Chip)), None)
            if chip is None:
                break
            self._lay.removeWidget(chip)
            chip.setParent(None)
            chip.deleteLater()
        self.setVisible(False)
        self.changed.emit()
