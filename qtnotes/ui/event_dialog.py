"""Диалог создания события календаря: название + один из 10 цветов."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import DEFAULT_EVENT_COLOR, EVENT_COLORS


class _ColorDot(QPushButton):
    def __init__(self, hexv: str, name: str):
        super().__init__()
        self.hexv = hexv
        self.setCheckable(True)
        self.setToolTip(name)
        self.setFixedSize(28, 28)
        self.setCursor(Qt.PointingHandCursor)
        self._restyle(False)

    def _restyle(self, checked: bool) -> None:
        border = "#ffffff" if checked else "transparent"
        self.setStyleSheet(
            f"QPushButton{{background:{self.hexv};border-radius:14px;"
            f"border:2px solid {border};}}"
        )

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self._restyle(checked)


class EventDialog(QDialog):
    """Возвращает (name, color). date — для заголовка."""

    def __init__(self, parent=None, date_label: str = "", color: str | None = None,
                 name: str = "", editing: bool = False):
        super().__init__(parent)
        title = "Изменить событие" if editing else "Новое событие"
        self.setWindowTitle(f"{title} · {date_label}" if date_label else title)
        self.setMinimumWidth(360)

        self._color = color or DEFAULT_EVENT_COLOR

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self._name = QLineEdit(name)
        self._name.setPlaceholderText("Название события")
        self._name.textChanged.connect(self._update_ok)
        form.addRow("Название", self._name)

        dots_row = QWidget()
        dr = QHBoxLayout(dots_row)
        dr.setContentsMargins(0, 0, 0, 0)
        dr.setSpacing(6)
        self._dots: list[_ColorDot] = []
        for name, hexv in EVENT_COLORS:
            dot = _ColorDot(hexv, name)
            dot.clicked.connect(lambda _=False, h=hexv: self._pick(h))
            dr.addWidget(dot)
            self._dots.append(dot)
        dr.addStretch(1)
        form.addRow("Цвет", dots_row)
        root.addLayout(form)
        self._sync()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Сохранить" if editing else "Создать")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._ok = buttons.button(QDialogButtonBox.Ok)
        self._update_ok()

    def _pick(self, hexv: str) -> None:
        self._color = hexv
        self._sync()

    def _sync(self) -> None:
        for dot in self._dots:
            dot.setChecked(dot.hexv == self._color)

    def _update_ok(self) -> None:
        self._ok.setEnabled(bool(self._name.text().strip()))

    def _try_accept(self) -> None:
        if self._name.text().strip():
            self.accept()

    def values(self) -> tuple[str, str]:
        return self._name.text().strip(), self._color
