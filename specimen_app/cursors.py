"""趣味光标：程序生成的卡通光标，可在「设置」里替代默认箭头。

参考用户给的手势图思路，但只做礼貌趣味款（食指 / 手掌 / 钢笔 / 爪印 / 星星）。
仿 `icon.py` 用 `QImage` + `QPainter` 程序画图，不引入图片素材，打包无需额外处理。

`create_cursor_image()` 只用 `QImage`，import / 调用都不依赖 `QApplication`，测试安全。
`make_cursor()` 会建 `QPixmap`，**必须在有 GUI（QApplication 已建）时才调用**。
"""

from __future__ import annotations

import math

from PyQt5.QtCore import Qt, QPointF, QRectF
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
)

# key -> 显示名（设置对话框下拉用）。"default" = 系统默认箭头。
CURSOR_STYLE_OPTIONS: dict[str, str] = {
    "default": "默认箭头",
    "finger": "卡通食指",
    "palm": "卡通手掌",
    "pen": "钢笔",
    "paw": "爪印",
    "star": "星星",
}

# 每款光标的热点（命中点）坐标，基于 32×32 画布。
_HOTSPOTS: dict[str, tuple[int, int]] = {
    "finger": (14, 2),   # 食指指尖
    "palm": (16, 16),    # 手掌中心
    "pen": (3, 3),       # 笔尖
    "paw": (16, 16),     # 爪印中心
    "star": (16, 16),    # 星星中心
}

_CURSOR_SIZE = 32

# 配色
_SKIN = QColor("#ffcb6b")
_SKIN_LINE = QColor("#7a5a1e")
_PEN_BODY = QColor("#3a6ea5")
_PEN_NIB = QColor("#e8e8e8")
_PEN_LINE = QColor("#22405f")
_PAW = QColor("#a1887f")
_PAW_LINE = QColor("#4e342e")
_STAR = QColor("#ffd23f")
_STAR_LINE = QColor("#b8860b")


def cursor_hotspot(style: str) -> tuple[int, int]:
    """返回某款光标的热点坐标；未知款回退画布中心。"""
    return _HOTSPOTS.get(style, (_CURSOR_SIZE // 2, _CURSOR_SIZE // 2))


def _new_canvas(size: int) -> tuple[QImage, QPainter]:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    return img, painter


def _draw_finger(p: QPainter) -> None:
    """卡通食指：拳头在下、食指朝上，指尖在画布顶部。"""
    p.setPen(QPen(_SKIN_LINE, 1.6))
    p.setBrush(QBrush(_SKIN))
    # 拳头
    p.drawRoundedRect(QRectF(6, 14, 19, 15), 5, 5)
    # 食指（朝上的胶囊）
    p.drawRoundedRect(QRectF(10.5, 2, 7, 17), 3.5, 3.5)


def _draw_palm(p: QPainter) -> None:
    """卡通手掌：手掌 + 五指张开朝上。"""
    p.setPen(QPen(_SKIN_LINE, 1.5))
    p.setBrush(QBrush(_SKIN))
    # 手掌
    p.drawRoundedRect(QRectF(7, 14, 18, 14), 6, 6)
    # 五指：四长指 + 一拇指
    for i, x in enumerate((8.5, 12.5, 16.5, 20.5)):
        top = 6.0 if i in (1, 2) else 8.0  # 中间两指略长
        p.drawRoundedRect(QRectF(x, top, 3.4, 14 - (top - 6)), 1.7, 1.7)
    # 拇指（偏右下、略斜）
    p.save()
    p.translate(24, 18)
    p.rotate(35)
    p.drawRoundedRect(QRectF(-1.8, -1.8, 3.6, 11), 1.8, 1.8)
    p.restore()


def _draw_pen(p: QPainter) -> None:
    """钢笔：左上为笔尖的斜向钢笔。"""
    p.save()
    p.translate(3, 3)
    p.rotate(45)  # 笔身沿对角线
    # 笔杆
    p.setPen(QPen(_PEN_LINE, 1.4))
    p.setBrush(QBrush(_PEN_BODY))
    p.drawRoundedRect(QRectF(3.2, -2.6, 24, 5.2), 1.6, 1.6)
    # 笔尖（三角）
    nib = QPolygonF([QPointF(0, 0), QPointF(5.2, -2.6), QPointF(5.2, 2.6)])
    p.setBrush(QBrush(_PEN_NIB))
    p.drawPolygon(nib)
    p.restore()


def _draw_paw(p: QPainter) -> None:
    """爪印：掌垫 + 四趾垫。"""
    p.setPen(QPen(_PAW_LINE, 1.2))
    p.setBrush(QBrush(_PAW))
    # 掌垫
    p.drawEllipse(QRectF(9, 15, 14, 12))
    # 四趾垫
    for x, y, w, h in (
        (7.5, 8, 5.5, 7),
        (12.5, 4.5, 6, 7.5),
        (19, 4.5, 6, 7.5),
        (24, 8, 5.5, 7),
    ):
        p.drawEllipse(QRectF(x, y, w, h))


def _draw_star(p: QPainter) -> None:
    """五角星。"""
    cx, cy = 16.0, 16.0
    outer, inner = 13.0, 5.4
    pts: list[QPointF] = []
    for i in range(10):
        radius = outer if i % 2 == 0 else inner
        ang = -math.pi / 2 + i * math.pi / 5  # 从正上方起
        pts.append(QPointF(cx + radius * math.cos(ang), cy + radius * math.sin(ang)))
    p.setPen(QPen(_STAR_LINE, 1.4))
    p.setBrush(QBrush(_STAR))
    path = QPainterPath()
    path.addPolygon(QPolygonF(pts))
    path.closeSubpath()
    p.drawPath(path)


_DRAWERS = {
    "finger": _draw_finger,
    "palm": _draw_palm,
    "pen": _draw_pen,
    "paw": _draw_paw,
    "star": _draw_star,
}


def create_cursor_image(style: str, size: int = _CURSOR_SIZE) -> QImage | None:
    """程序画出某款光标的 QImage（透明底）。style="default"/未知 -> None。

    只用 QImage/QPainter，不依赖 QApplication —— import 与单测可安全调用。
    内部按 32×32 画，size 不同则等比缩放。
    """
    drawer = _DRAWERS.get(style)
    if drawer is None:
        return None
    img, painter = _new_canvas(_CURSOR_SIZE)
    drawer(painter)
    painter.end()
    if size != _CURSOR_SIZE:
        img = img.scaled(
            size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
    return img


def make_cursor(style: str) -> QCursor | None:
    """按 style 生成 QCursor；"default"/未知 -> None（调用方据此 unsetCursor）。

    会建 QPixmap，必须在 QApplication 已创建后调用。
    """
    img = create_cursor_image(style)
    if img is None:
        return None
    hx, hy = cursor_hotspot(style)
    return QCursor(QPixmap.fromImage(img), hx, hy)
