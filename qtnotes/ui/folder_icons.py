"""Набор из 15 иконок для папок, отрисованных кодом (белым по цветному кругу)."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF

# Идентификаторы доступных иконок (порядок = порядок в палитре выбора).
ICON_IDS = [
    "letter",   # первая буква имени (по умолчанию)
    "dot",
    "ring",
    "square",
    "triangle",
    "diamond",
    "star",
    "heart",
    "hexagon",
    "pentagon",
    "plus",
    "cross",
    "check",
    "bolt",
    "moon",
]


def _polygon(cx: float, cy: float, r: float, n: int, rot: float = -90.0) -> QPolygonF:
    pts = []
    for i in range(n):
        a = math.radians(rot + i * 360.0 / n)
        pts.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
    return QPolygonF(pts)


def _star(cx: float, cy: float, r: float, n: int = 5) -> QPolygonF:
    pts = []
    for i in range(n * 2):
        rr = r if i % 2 == 0 else r * 0.45
        a = math.radians(-90 + i * 180.0 / n)
        pts.append(QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return QPolygonF(pts)


def paint_icon(painter: QPainter, size: int, icon_id: str, letter: str = "?",
               color: str = "#ffffff") -> None:
    """Нарисовать иконку поверх уже залитого круга (квадрат size×size)."""
    p = painter
    c = QColor(color)
    p.setRenderHint(QPainter.Antialiasing)
    cx = cy = size / 2.0
    r = size * 0.28

    if icon_id == "letter":
        from PySide6.QtGui import QFont
        f = QFont("Segoe UI", int(size * 0.42))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QPen(c))
        p.drawText(QRectF(0, 0, size, size), Qt.AlignCenter, (letter or "?")[:1].upper())
        return

    fill_brush = QBrush(c)
    line_pen = QPen(c)
    line_pen.setWidthF(max(2.0, size * 0.07))
    line_pen.setCapStyle(Qt.RoundCap)
    line_pen.setJoinStyle(Qt.RoundJoin)

    if icon_id == "dot":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawEllipse(QPointF(cx, cy), r, r)
    elif icon_id == "ring":
        pen = QPen(c)
        pen.setWidthF(size * 0.09)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), r, r)
    elif icon_id == "square":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        path = QPainterPath()
        path.addRoundedRect(QRectF(cx - r, cy - r, r * 2, r * 2), r * 0.25, r * 0.25)
        p.drawPath(path)
    elif icon_id == "triangle":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawPolygon(_polygon(cx, cy, r * 1.1, 3))
    elif icon_id == "diamond":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawPolygon(_polygon(cx, cy, r * 1.15, 4, rot=-90))
    elif icon_id == "star":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawPolygon(_star(cx, cy, r * 1.2))
    elif icon_id == "heart":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        path = QPainterPath()
        path.moveTo(cx, cy + r * 0.85)
        path.cubicTo(cx - r * 1.6, cy - r * 0.2, cx - r * 0.5, cy - r * 1.1, cx, cy - r * 0.35)
        path.cubicTo(cx + r * 0.5, cy - r * 1.1, cx + r * 1.6, cy - r * 0.2, cx, cy + r * 0.85)
        p.drawPath(path)
    elif icon_id == "hexagon":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawPolygon(_polygon(cx, cy, r * 1.1, 6, rot=0))
    elif icon_id == "pentagon":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawPolygon(_polygon(cx, cy, r * 1.1, 5))
    elif icon_id == "plus":
        p.setPen(line_pen)
        p.drawLine(QPointF(cx, cy - r), QPointF(cx, cy + r))
        p.drawLine(QPointF(cx - r, cy), QPointF(cx + r, cy))
    elif icon_id == "cross":
        p.setPen(line_pen)
        d = r * 0.75
        p.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        p.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))
    elif icon_id == "check":
        p.setPen(line_pen)
        p.drawPolyline(QPolygonF([
            QPointF(cx - r * 0.8, cy),
            QPointF(cx - r * 0.1, cy + r * 0.7),
            QPointF(cx + r * 0.9, cy - r * 0.7),
        ]))
    elif icon_id == "bolt":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawPolygon(QPolygonF([
            QPointF(cx + r * 0.2, cy - r),
            QPointF(cx - r * 0.6, cy + r * 0.15),
            QPointF(cx, cy + r * 0.15),
            QPointF(cx - r * 0.2, cy + r),
            QPointF(cx + r * 0.6, cy - r * 0.15),
            QPointF(cx, cy - r * 0.15),
        ]))
    elif icon_id == "moon":
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        path = QPainterPath()
        path.addEllipse(QPointF(cx, cy), r, r)
        cut = QPainterPath()
        cut.addEllipse(QPointF(cx + r * 0.5, cy - r * 0.25), r * 0.9, r * 0.9)
        p.drawPath(path.subtracted(cut))
    else:
        # неизвестная — нарисуем точку
        p.setPen(Qt.NoPen)
        p.setBrush(fill_brush)
        p.drawEllipse(QPointF(cx, cy), r, r)
