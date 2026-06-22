"""Диалог создания/редактирования папки."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .folder_icons import ICON_IDS
from .graphics import color_for_name, folder_avatar
from .theme import EVENT_COLOR_HEXES


class _ColorDot(QPushButton):
    """Кружок выбора цвета папки."""

    def __init__(self, hexv: str):
        super().__init__()
        self.hexv = hexv
        self.setCheckable(True)
        self.setFixedSize(26, 26)
        self.setCursor(Qt.PointingHandCursor)
        self._restyle(False)

    def _restyle(self, checked: bool) -> None:
        border = "#ffffff" if checked else "transparent"
        self.setStyleSheet(
            f"QPushButton{{background:{self.hexv};border-radius:13px;"
            f"border:2px solid {border};}}"
        )

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self._restyle(checked)


class FolderDialog(QDialog):
    """Возвращает (name, caption, color, icon) при принятии."""

    def __init__(self, parent=None, name: str = "", caption: str = "",
                 color: str | None = None, icon: str = "letter"):
        super().__init__(parent)
        self.setWindowTitle("Папка")
        self.setMinimumWidth(380)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self._name = QLineEdit(name)
        self._name.setPlaceholderText("Название папки")
        self._name.textChanged.connect(self._on_name_changed)
        form.addRow("Название", self._name)

        self._caption = QLineEdit(caption)
        self._caption.setPlaceholderText("Короткая подпись (необязательно)")
        self._caption.setMaxLength(24)
        form.addRow("Подпись", self._caption)

        # выбор цвета
        self._color = color
        self._auto = color is None
        self._dots: list[_ColorDot] = []
        colors_row = QWidget()
        cr = QHBoxLayout(colors_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(6)
        for hexv in EVENT_COLOR_HEXES:
            dot = _ColorDot(hexv)
            dot.clicked.connect(lambda _=False, h=hexv: self._pick_color(h))
            cr.addWidget(dot)
            self._dots.append(dot)
        cr.addStretch(1)
        form.addRow("Цвет", colors_row)

        # выбор иконки (15 штук)
        self._icon = icon or "letter"
        self._icon_btns: dict[str, QPushButton] = {}
        self._icon_group = QButtonGroup(self)
        self._icon_group.setExclusive(True)
        icons_grid = QWidget()
        grid = QGridLayout(icons_grid)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for i, icon_id in enumerate(ICON_IDS):
            btn = QPushButton()
            btn.setCheckable(True)
            btn.setObjectName("IconPick")
            btn.setFixedSize(40, 40)
            btn.setIconSize(QSize(30, 30))
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, ic=icon_id: self._pick_icon(ic))
            self._icon_group.addButton(btn)
            self._icon_btns[icon_id] = btn
            grid.addWidget(btn, i // 8, i % 8)
        form.addRow("Иконка", icons_grid)

        root.addLayout(form)

        if color is not None:
            self._sync_dots()
        self._refresh_icons()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Сохранить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self._try_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._ok_btn = buttons.button(QDialogButtonBox.Ok)
        self._update_ok()

    # --- название ---

    def _on_name_changed(self) -> None:
        self._update_ok()
        self._refresh_icons()

    def _update_ok(self) -> None:
        self._ok_btn.setEnabled(bool(self._name.text().strip()))

    # --- цвет ---

    def _pick_color(self, hexv: str) -> None:
        self._color = hexv
        self._auto = False
        self._sync_dots()
        self._refresh_icons()

    def _sync_dots(self) -> None:
        for dot in self._dots:
            dot.setChecked(dot.hexv == self._color)

    # --- иконка ---

    def _pick_icon(self, icon_id: str) -> None:
        self._icon = icon_id
        self._icon_btns[icon_id].setChecked(True)

    def _refresh_icons(self) -> None:
        """Перерисовать превью иконок текущим цветом/буквой."""
        name = self._name.text().strip() or "?"
        color = self._effective_color(name)
        from PySide6.QtGui import QIcon
        for icon_id, btn in self._icon_btns.items():
            btn.setIcon(QIcon(folder_avatar(name, 30, color, icon_id)))
            btn.setChecked(icon_id == self._icon)

    def _effective_color(self, name: str) -> str:
        return self._color if not self._auto else color_for_name(name)

    # --- результат ---

    def _try_accept(self) -> None:
        if self._name.text().strip():
            self.accept()

    def values(self) -> tuple[str, str, str, str]:
        name = self._name.text().strip()
        caption = self._caption.text().strip()
        color = self._effective_color(name)
        return name, caption, color, self._icon
