"""Левый сайдбар: узкая вертикальная колонка с папками и кнопками."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QMenu,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .graphics import calendar_icon, folder_avatar, plus_icon, settings_icon

SIDEBAR_WIDTH = 78
AVATAR_SIZE = 50


class FolderItem(QFrame):
    """Один элемент списка папок: круглая аватарка + короткая подпись."""

    clicked = Signal(str)           # folder_id
    editRequested = Signal(str)     # folder_id
    deleteRequested = Signal(str)   # folder_id
    exportRequested = Signal(str)   # folder_id

    def __init__(self, folder_id: str, name: str, caption: str,
                 color: str | None = None, icon: str = "letter"):
        super().__init__()
        self.folder_id = folder_id
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(SIDEBAR_WIDTH - 8)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 6)
        lay.setSpacing(3)
        lay.setAlignment(Qt.AlignHCenter)

        self._avatar = QLabel()
        self._avatar.setPixmap(folder_avatar(name, AVATAR_SIZE, color, icon))
        self._avatar.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._avatar)

        text = caption or name
        self._caption = QLabel(self._elide(text))
        self._caption.setAlignment(Qt.AlignCenter)
        self._caption.setToolTip(name)
        lay.addWidget(self._caption)

        self.set_active(False)

    @staticmethod
    def _elide(text: str, limit: int = 10) -> str:
        text = text.strip()
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def set_active(self, active: bool) -> None:
        self.setProperty("active", "true" if active else "false")
        # перечитать стиль после смены свойства
        self.style().unpolish(self)
        self.style().polish(self)

    def update_data(self, name: str, caption: str, color: str | None = None,
                    icon: str = "letter") -> None:
        self._avatar.setPixmap(folder_avatar(name, AVATAR_SIZE, color, icon))
        self._caption.setText(self._elide(caption or name))
        self._caption.setToolTip(name)

    def mouseReleaseEvent(self, event):  # noqa: N802 (Qt naming)
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit(self.folder_id)
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        menu = QMenu(self)
        act_edit = menu.addAction("Изменить папку")
        act_export = menu.addAction("Экспортировать папку…")
        menu.addSeparator()
        act_del = menu.addAction("Удалить папку")
        chosen = menu.exec(event.globalPos())
        if chosen == act_edit:
            self.editRequested.emit(self.folder_id)
        elif chosen == act_export:
            self.exportRequested.emit(self.folder_id)
        elif chosen == act_del:
            self.deleteRequested.emit(self.folder_id)


def _sidebar_button(icon, tooltip: str) -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("SidebarButton")
    btn.setIcon(icon)
    btn.setIconSize(QSize(28, 28))
    btn.setFixedSize(SIDEBAR_WIDTH - 18, SIDEBAR_WIDTH - 18)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


class Sidebar(QFrame):
    """Колонка папок и управляющих кнопок."""

    folderSelected = Signal(str)
    folderEditRequested = Signal(str)
    folderDeleteRequested = Signal(str)
    folderExportRequested = Signal(str)
    newFolderRequested = Signal()
    importRequested = Signal()
    exportAllRequested = Signal()
    calendarRequested = Signal()
    settingsRequested = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("Sidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        self._items: dict[str, FolderItem] = {}
        self._active_id: str | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(4, 8, 4, 8)
        root.setSpacing(6)

        # Прокручиваемая область со списком папок + кнопкой «новая папка».
        self._scroll = QScrollArea()
        self._scroll.setObjectName("SidebarScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setFrameShape(QFrame.NoFrame)

        inner = QWidget()
        self._list = QVBoxLayout(inner)
        self._list.setContentsMargins(0, 0, 0, 0)
        self._list.setSpacing(4)
        self._list.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        self._new_btn = _sidebar_button(plus_icon(), "Добавить")
        self._new_btn.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(self._new_btn)
        act_new = QAction("Новая папка", menu)
        act_new.triggered.connect(self.newFolderRequested.emit)
        menu.addAction(act_new)
        menu.addSeparator()
        act_import = QAction("Импортировать архив…", menu)
        act_import.triggered.connect(self.importRequested.emit)
        menu.addAction(act_import)
        act_export_all = QAction("Экспортировать всё…", menu)
        act_export_all.triggered.connect(self.exportAllRequested.emit)
        menu.addAction(act_export_all)
        self._new_btn.setMenu(menu)
        self._new_menu = menu
        self._list.addWidget(self._new_btn, alignment=Qt.AlignHCenter)

        self._hint = QLabel("папки\nпока нет")
        self._hint.setObjectName("SidebarHint")
        self._hint.setAlignment(Qt.AlignHCenter)
        self._hint.setWordWrap(True)
        self._list.addWidget(self._hint, alignment=Qt.AlignHCenter)

        self._list.addStretch(1)

        self._scroll.setWidget(inner)
        root.addWidget(self._scroll, 1)

        # Кнопки внизу колонки: календарь и настройки.
        self._cal_btn = _sidebar_button(calendar_icon(), "Календарь")
        self._cal_btn.clicked.connect(self.calendarRequested.emit)
        root.addWidget(self._cal_btn, alignment=Qt.AlignHCenter)

        self._settings_btn = _sidebar_button(settings_icon(), "Настройки")
        self._settings_btn.clicked.connect(self.settingsRequested.emit)
        root.addWidget(self._settings_btn, alignment=Qt.AlignHCenter)

    # --- управление списком папок ---

    def set_folders(self, folders: list) -> None:
        """folders: список объектов с полями id, name, caption, color.

        F2 (раунд-3): diff-обновление вместо destroy/recreate всех аватарок. На каждый
        входящий синк-op это вызывалось и пересоздавало весь сайдбар → мерцание и потеря
        выделения. Теперь существующие элементы обновляются на месте, лишние удаляются,
        новые создаются, и порядок выравнивается под список."""
        self._hint.setVisible(not folders)
        new_ids = {f.id for f in folders}

        # удалить пропавшие
        for fid in list(self._items.keys()):
            if fid not in new_ids:
                item = self._items.pop(fid)
                item.setParent(None)
                item.deleteLater()

        # создать/обновить и расставить по порядку
        for pos, f in enumerate(folders):
            item = self._items.get(f.id)
            if item is None:
                item = FolderItem(
                    f.id, f.name, getattr(f, "caption", ""),
                    getattr(f, "color", None), getattr(f, "icon", "letter"),
                )
                item.clicked.connect(self._on_item_clicked)
                item.editRequested.connect(self.folderEditRequested.emit)
                item.deleteRequested.connect(self.folderDeleteRequested.emit)
                item.exportRequested.connect(self.folderExportRequested.emit)
                self._items[f.id] = item
            else:
                item.update_data(f.name, getattr(f, "caption", ""),
                                 getattr(f, "color", None), getattr(f, "icon", "letter"))
            # выставить виджет в нужную позицию (insertWidget переставляет, если уже в layout)
            self._list.insertWidget(pos, item, alignment=Qt.AlignHCenter)

        for fid, item in self._items.items():
            item.set_active(fid == self._active_id)

    def select_folder(self, folder_id: str | None) -> None:
        self._active_id = folder_id
        for fid, item in self._items.items():
            item.set_active(fid == folder_id)

    def _on_item_clicked(self, folder_id: str) -> None:
        self.select_folder(folder_id)
        self.folderSelected.emit(folder_id)
