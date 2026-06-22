"""Программная отрисовка иконок и аватарок (без внешних файлов и шрифтов)."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

# Палитра для автогенерации цвета аватарки папки по её имени.
_AVATAR_COLORS = [
    "#e56555", "#e9924d", "#e7c14f", "#67b35e", "#4db6ac",
    "#5aa7e0", "#5288c1", "#9b72d4", "#e57aa8", "#8a98a8",
]


def color_for_name(name: str) -> str:
    """Стабильный цвет аватарки по имени папки."""
    if not name:
        return _AVATAR_COLORS[0]
    return _AVATAR_COLORS[sum(ord(c) for c in name) % len(_AVATAR_COLORS)]


def folder_avatar(name: str, size: int = 48, color: str | None = None,
                  icon_id: str = "letter") -> QPixmap:
    """Круглая аватарка папки: цветной круг с иконкой (или первой буквой имени)."""
    from .folder_icons import paint_icon

    pm = QPixmap(size, size)
    pm.setDevicePixelRatio(1.0)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(color or color_for_name(name))))
    p.drawEllipse(0, 0, size, size)

    letter = (name.strip()[:1] or "?").upper()
    paint_icon(p, size, icon_id or "letter", letter=letter, color="#ffffff")
    p.end()
    return pm


def _new_canvas(size: int) -> tuple[QPixmap, QPainter]:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    return pm, p


def plus_icon(size: int = 28, color: str = "#7f91a4") -> QIcon:
    """Иконка «+» для кнопки новой папки."""
    pm, p = _new_canvas(size)
    pen = QPen(QColor(color))
    pen.setWidth(max(2, size // 12))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    m = size * 0.28
    p.drawLine(int(size / 2), int(m), int(size / 2), int(size - m))
    p.drawLine(int(m), int(size / 2), int(size - m), int(size / 2))
    p.end()
    return QIcon(pm)


def calendar_icon(size: int = 28, color: str = "#7f91a4") -> QIcon:
    """Иконка календаря для кнопки переключения вида."""
    pm, p = _new_canvas(size)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.6, size / 16))
    p.setPen(pen)
    m = size * 0.18
    body = QRectF(m, m * 1.4, size - 2 * m, size - m * 2.2)
    path = QPainterPath()
    path.addRoundedRect(body, size * 0.08, size * 0.08)
    p.drawPath(path)
    # верхняя линия-«шапка» и два «гвоздика»
    top_y = body.top() + body.height() * 0.28
    p.drawLine(int(body.left()), int(top_y), int(body.right()), int(top_y))
    p.setBrush(QColor(color))
    r = size * 0.05
    for fx in (0.34, 0.66):
        cx = body.left() + body.width() * fx
        p.drawEllipse(QRectF(cx - r, m * 0.6, r * 2, r * 2))
    p.end()
    return QIcon(pm)


def attach_icon(size: int = 26, color: str = "#7f91a4") -> QIcon:
    """Иконка скрепки для кнопки прикрепления файла."""
    pm, p = _new_canvas(size)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.8, size / 12))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    path = QPainterPath()
    x = size * 0.5
    top = size * 0.2
    bot = size * 0.74
    path.moveTo(x + size * 0.12, size * 0.32)
    path.lineTo(x + size * 0.12, bot)
    path.cubicTo(x + size * 0.12, size * 0.92, x - size * 0.28, size * 0.92, x - size * 0.28, bot)
    path.lineTo(x - size * 0.28, size * 0.3)
    path.cubicTo(x - size * 0.28, top, x + size * 0.04, top, x + size * 0.04, size * 0.3)
    path.lineTo(x + size * 0.04, bot)
    p.drawPath(path)
    p.end()
    return QIcon(pm)


def settings_icon(size: int = 28, color: str = "#7f91a4") -> QIcon:
    """Иконка шестерёнки для кнопки настроек."""
    import math

    from PySide6.QtCore import QPointF

    pm, p = _new_canvas(size)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.6, size / 14))
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    cx = cy = size / 2.0
    teeth = 8
    r_out = size * 0.36
    r_in = size * 0.26
    path = QPainterPath()
    steps = teeth * 2
    for i in range(steps + 1):
        ang = math.radians(i * 360.0 / steps)
        rr = r_out if i % 2 == 0 else r_in
        pt = QPointF(cx + rr * math.cos(ang), cy + rr * math.sin(ang))
        if i == 0:
            path.moveTo(pt)
        else:
            path.lineTo(pt)
    p.drawPath(path)
    # центральное отверстие
    hole = size * 0.12
    p.drawEllipse(QPointF(cx, cy), hole, hole)
    p.end()
    return QIcon(pm)


def search_icon(size: int = 22, color: str = "#7f91a4") -> QIcon:
    """Иконка лупы для строки поиска."""
    pm, p = _new_canvas(size)
    pen = QPen(QColor(color))
    pen.setWidthF(max(1.6, size / 12))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    d = size * 0.5
    p.drawEllipse(QRectF(size * 0.18, size * 0.18, d, d))
    p.drawLine(
        int(size * 0.18 + d), int(size * 0.18 + d),
        int(size * 0.82), int(size * 0.82),
    )
    p.end()
    return QIcon(pm)
