"""Виджет отрисовки QR-кода из булевой матрицы (segno → Qt)."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget


class QrView(QWidget):
    """Рисует матрицу модулей QR. Фон всегда светлый — QR нужен контраст."""

    def __init__(self, matrix=None, parent=None):
        super().__init__(parent)
        self._matrix = matrix or []
        self.setMinimumSize(240, 240)

    def set_matrix(self, matrix) -> None:
        self._matrix = matrix or []
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(280, 280)

    def paintEvent(self, e):  # noqa: N802
        if not self._matrix:
            return
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("white"))
        n = len(self._matrix)
        quiet = 2                       # тихая зона по краям, в модулях
        total = n + quiet * 2
        cell = max(1, min(self.width(), self.height()) // total)
        side = cell * total
        offx = (self.width() - side) // 2
        offy = (self.height() - side) // 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("black"))
        for r, row in enumerate(self._matrix):
            for c, val in enumerate(row):
                if val:
                    p.drawRect(offx + (c + quiet) * cell, offy + (r + quiet) * cell,
                               cell, cell)
