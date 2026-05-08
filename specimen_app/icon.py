from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QIcon, QImage, QPainter, QPen, QColor, QBrush, QFont, QLinearGradient
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QPixmap


_ICON_SIZE = 256


def create_app_icon() -> QImage:
    """Generate a specimen bottle + label icon as QImage."""
    size = _ICON_SIZE
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(QColor(0, 0, 0, 0))

    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)

    cx, cy = size / 2, size / 2
    r = size * 0.46

    # Blue circular background with gradient
    gradient = QLinearGradient(cx - r, cy - r, cx + r, cy + r)
    gradient.setColorAt(0.0, QColor("#3a8fd4"))
    gradient.setColorAt(1.0, QColor("#1a5faa"))
    painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

    # --- Draw specimen bottle ---
    painter.setPen(QPen(QColor(255, 255, 255, 230), 2.5))
    painter.setBrush(QColor(255, 255, 255, 40))

    # Bottle body (rounded rectangle)
    bw = size * 0.22  # bottle width
    bh = size * 0.36  # bottle height
    bx = cx - bw / 2
    by = cy - bh / 2 + size * 0.04
    body_rect = QRectF(bx, by, bw, bh)
    painter.drawRoundedRect(body_rect, 6, 6)

    # Bottle neck
    nw = bw * 0.45
    nh = size * 0.09
    nx = cx - nw / 2
    ny = by - nh + 2
    painter.drawRect(QRectF(nx, ny, nw, nh))

    # Bottle cap
    cw = nw * 1.3
    ch = size * 0.04
    cap_rect = QRectF(cx - cw / 2, ny - ch + 2, cw, ch)
    painter.setBrush(QColor(255, 255, 255, 100))
    painter.drawRoundedRect(cap_rect, 2, 2)

    # Liquid inside bottle (filled area at bottom)
    liquid_top = by + bh * 0.35
    liquid_rect = QRectF(bx + 2, liquid_top, bw - 4, by + bh - liquid_top - 2)
    painter.setBrush(QColor(180, 220, 255, 80))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(liquid_rect, 4, 4)

    # --- Draw label tag (to the right of bottle) ---
    painter.setPen(QPen(QColor(255, 255, 255, 230), 2))
    painter.setBrush(QColor(255, 255, 255, 60))

    # Tag body
    tw = size * 0.20
    th = size * 0.24
    tx = cx + bw / 2 + size * 0.06
    ty = cy - th / 2 + size * 0.02
    tag_rect = QRectF(tx, ty, tw, th)
    painter.drawRoundedRect(tag_rect, 4, 4)

    # Tag hole
    hole_r = size * 0.015
    painter.setBrush(QColor("#1a5faa"))
    painter.drawEllipse(QRectF(tx + tw / 2 - hole_r, ty + hole_r * 1.5, hole_r * 2, hole_r * 2))

    # Tag lines (text placeholder)
    painter.setPen(QPen(QColor(255, 255, 255, 160), 1.5))
    line_y = ty + th * 0.3
    for i in range(3):
        lw = tw * (0.7 if i < 2 else 0.45)
        lx = tx + (tw - lw) / 2
        painter.drawLine(int(lx), int(line_y), int(lx + lw), int(line_y))
        line_y += th * 0.18

    painter.end()
    return img


def get_app_icon() -> QIcon:
    """Return the app icon as QIcon."""
    img = create_app_icon()
    return QIcon(QPixmap.fromImage(img))


def save_icon_files(output_dir: Path | str) -> list[Path]:
    """Save icon as PNG and ICO to output_dir."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    img = create_app_icon()

    png_path = output_dir / "app_icon.png"
    img.save(str(png_path), "PNG")

    ico_path = output_dir / "app_icon.ico"
    img.save(str(ico_path), "ICO")

    return [png_path, ico_path]


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    paths = save_icon_files(Path(__file__).parent.parent / "docs" / "icons")
    for p in paths:
        print(f"Saved: {p}")
