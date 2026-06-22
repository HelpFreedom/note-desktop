"""Контекстное меню медиа-элемента: копировать в буфер / копировать путь."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu


def show_media_menu(widget, global_pos, path, pixmap=None) -> bool:
    """Показать меню для медиа. pixmap задаётся для изображений (копировать как картинку).

    Возвращает True, если меню показано/обработано.
    """
    menu = QMenu(widget)
    act_img = None
    if pixmap is not None and not pixmap.isNull():
        act_img = menu.addAction("Копировать изображение")
    act_file = menu.addAction("Копировать файл")
    act_path = menu.addAction("Копировать путь к файлу")
    chosen = menu.exec(global_pos)
    if chosen is None:
        return True

    cb = QGuiApplication.clipboard()
    if act_img is not None and chosen == act_img:
        cb.setPixmap(pixmap)
    elif chosen == act_file:
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(path))])
        cb.setMimeData(md)
    elif chosen == act_path:
        cb.setText(str(path))
    return True
