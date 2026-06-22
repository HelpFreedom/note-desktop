"""Виджет одной заметки в ленте — «пузырь» в стиле Telegram.

Виртуализация по содержимому: оболочка пузыря (текст, чипы файлов, заглушки
медиа точного размера) строится всегда — она дешёвая. Тяжёлые ресурсы (декод
картинки, QMovie для GIF, VideoPreview/QMediaPlayer для видео) создаются ТОЛЬКО
когда пузырь активен (находится в окне видимости ленты) и освобождаются, когда
он уходит далеко. Заглушки резервируют точный размер по сохранённым w/h, поэтому
загрузка/выгрузка медиа не сдвигает раскладку — прокрутка не дёргается.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, QUrl, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QGuiApplication,
    QImageReader,
    QMovie,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..storage import vault
from .mediamenu import show_media_menu
from .textutils import colorize_links, linkify_references, strip_theme_overrides
from .theme import PALETTE
from .video_widget import MULTIMEDIA_OK, VideoPreview

IMG_MAX_W = 720   # картинки не растягиваем шире этого, даже если пузырь шире


def _fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.astimezone().strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return ""


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ГБ"


class _FileChip(QFrame):
    """Кликабельная плашка файла: имя + размер, открывает файл по клику."""

    def __init__(self, path, name: str, size: int):
        super().__init__()
        self.setObjectName("FileChip")
        self._path = str(path)
        self._missing = not Path(self._path).exists()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        if self._missing:
            # вложение пропало с диска — показываем явно, не делаем кликабельным
            self.setProperty("missing", "true")
            label = QLabel(f"📄 {name}   ·   файл отсутствует")
        else:
            self.setCursor(Qt.PointingHandCursor)
            label = QLabel(f"📄 {name}   ·   {_human_size(size)}")
        label.setContextMenuPolicy(Qt.NoContextMenu)
        lay.addWidget(label)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if (not self._missing and event.button() == Qt.LeftButton
                and not (event.modifiers() & Qt.ControlModifier)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        show_media_menu(self, event.globalPos(), self._path)


class _MediaLabel(QLabel):
    """Метка с картинкой/GIF. ПКМ → копировать изображение/файл/путь.

    Картинка для копирования загружается лениво: пузырь мог быть неактивен и
    пиксмап ещё не декодирован — тогда читаем его с диска по запросу меню.
    """

    def __init__(self, path):
        super().__init__()
        self._path = str(path)
        self._copy_pixmap = None   # для статичных изображений (когда загружены)
        self._movie = None         # для GIF (берём текущий кадр)

    def contextMenuEvent(self, event):  # noqa: N802
        if self._movie is not None:
            pm = self._movie.currentPixmap()
        elif self._copy_pixmap is not None:
            pm = self._copy_pixmap
        else:
            pm = QPixmap(self._path)   # ленивая загрузка для копирования
            if pm.isNull():
                pm = None
        show_media_menu(self, event.globalPos(), self._path, pm)


class _NoteText(QLabel):
    """Текст заметки: выделение мышью + курсор-палец над ссылкой."""

    def __init__(self):
        super().__init__()
        self._over_link = False
        self.setMouseTracking(True)
        self.linkHovered.connect(self._on_hover)

    def _on_hover(self, href: str) -> None:
        self._over_link = bool(href)
        self.setCursor(Qt.PointingHandCursor if self._over_link else Qt.IBeamCursor)

    def mouseMoveEvent(self, e):  # noqa: N802
        super().mouseMoveEvent(e)
        if self._over_link:
            self.setCursor(Qt.PointingHandCursor)


class MessageBubble(QWidget):
    """Пузырь заметки. Выровнен вправо (как исходящие в Telegram)."""

    deleteRequested = Signal(str)
    editRequested = Signal(str)
    toggleSelectionRequested = Signal(str)   # note_id (Ctrl+клик)
    referenceActivated = Signal(str)          # note_id (клик по [[id]])

    def __init__(self, note):
        super().__init__()
        self.note = note
        self._selected = False
        self._content_w = 560
        # слоты медиа: dict(kind, path, w, h, widget, loaded, [movie|video])
        self._media: list[dict] = []
        self._active = False   # активен ли пузырь (загружены ли тяжёлые ресурсы)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        bubble = QFrame()
        bubble.setObjectName("Bubble")
        bubble.setContextMenuPolicy(Qt.NoContextMenu)
        bubble.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        bubble.installEventFilter(self)
        self._bubble = bubble

        self._inner = QVBoxLayout(bubble)
        self._inner.setContentsMargins(12, 9, 12, 7)
        self._inner.setSpacing(5)

        outer.addWidget(bubble, 1)

        self._time = QLabel()
        self._time.setObjectName("BubbleTime")
        self._time.setAlignment(Qt.AlignRight)
        self._time.setContextMenuPolicy(Qt.NoContextMenu)

        self.render_note()

    # --- адаптивная ширина ---

    def _media_img_w(self) -> int:
        return max(80, min(self._content_w - 28, IMG_MAX_W))

    def _slot_size(self, slot: dict, img_w: int) -> tuple[int, int]:
        """Зарезервированный размер заглушки/медиа под текущую ширину."""
        if slot["kind"] == "video":
            width = max(160, img_w)
            return width, int(width * 9 / 16)
        w0, h0 = slot["w"], slot["h"]
        if w0 > 0 and h0 > 0:
            width = min(img_w, w0)
            return width, max(1, round(h0 * width / w0))
        return img_w, img_w   # неизвестные размеры — квадрат-заглушка

    def set_max_width(self, width: int) -> None:
        self._content_w = max(200, width)
        self._bubble.setMaximumWidth(self._content_w)
        img_w = self._media_img_w()
        for slot in self._media:
            self._apply_slot_size(slot, img_w)

    def _apply_slot_size(self, slot: dict, img_w: int) -> None:
        w, h = self._slot_size(slot, img_w)
        widget = slot["widget"]
        widget.setFixedSize(w, h)
        if not slot["loaded"]:
            return
        if slot["kind"] == "image":
            pm = widget._copy_pixmap
            if pm is not None and not pm.isNull():
                widget.setPixmap(pm.scaledToWidth(w, Qt.SmoothTransformation)
                                 if pm.width() > w else pm)
        elif slot["kind"] == "gif":
            movie = slot.get("movie")
            if movie is not None:
                movie.setScaledSize(QSize(w, h))
        elif slot["kind"] == "video":
            vp = slot.get("video")
            if vp is not None:
                vp.set_width(w)

    # --- активность: ленивая загрузка/выгрузка тяжёлого медиа ---

    def set_active(self, active: bool) -> None:
        if active == self._active:
            return
        self._active = active
        self._refresh_active()

    def _refresh_active(self) -> None:
        img_w = self._media_img_w()
        for slot in self._media:
            if self._active and not slot["loaded"]:
                self._load_slot(slot, img_w)
            elif not self._active and slot["loaded"]:
                self._unload_slot(slot)

    def _load_slot(self, slot: dict, img_w: int) -> None:
        if slot["loaded"]:
            return
        w, h = self._slot_size(slot, img_w)
        kind = slot["kind"]
        if kind == "image":
            lbl = slot["widget"]
            pm = QPixmap(slot["path"])
            if not pm.isNull():
                lbl.setPixmap(pm.scaledToWidth(w, Qt.SmoothTransformation)
                              if pm.width() > w else pm)
                lbl._copy_pixmap = pm
            else:
                lbl.setText("[изображение недоступно]")
        elif kind == "gif":
            lbl = slot["widget"]
            movie = QMovie(slot["path"])
            movie.jumpToFrame(0)
            movie.setScaledSize(QSize(w, h))
            lbl.setMovie(movie)
            lbl._movie = movie
            movie.start()
            slot["movie"] = movie
        elif kind == "video":
            if MULTIMEDIA_OK and slot.get("video") is None:
                vp = VideoPreview(slot["path"], w)
                slot["widget"].layout().addWidget(vp, alignment=Qt.AlignHCenter)
                slot["video"] = vp
        slot["loaded"] = True

    def _unload_slot(self, slot: dict) -> None:
        if not slot["loaded"]:
            return
        kind = slot["kind"]
        if kind == "image":
            lbl = slot["widget"]
            lbl.clear()
            lbl._copy_pixmap = None
        elif kind == "gif":
            lbl = slot["widget"]
            movie = slot.get("movie")
            if movie is not None:
                movie.stop()
            lbl.clear()
            lbl._movie = None
            if movie is not None:
                movie.deleteLater()
            slot["movie"] = None
        elif kind == "video":
            vp = slot.get("video")
            if vp is not None:
                vp.setParent(None)
                vp.deleteLater()
                slot["video"] = None
        slot["loaded"] = False

    def _unload_all(self) -> None:
        for slot in self._media:
            self._unload_slot(slot)

    # --- выделение ---

    def set_selected(self, selected: bool, animate: bool = False) -> None:
        self._selected = selected
        self._bubble.setProperty("selected", "true" if selected else "false")
        self._bubble.style().unpolish(self._bubble)
        self._bubble.style().polish(self._bubble)

    def is_selected(self) -> bool:
        return self._selected

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() == QEvent.MouseButtonPress and (event.modifiers() & Qt.ControlModifier):
            self.toggleSelectionRequested.emit(self.note.id)
            return True
        return super().eventFilter(obj, event)

    # --- отрисовка ---

    def _dims(self, att, path) -> tuple[int, int]:
        """Размеры изображения: из вложения, иначе из заголовка файла (дёшево)."""
        if att.w > 0 and att.h > 0:
            return att.w, att.h
        size = QImageReader(str(path)).size()
        if size.isValid():
            return size.width(), size.height()
        return 0, 0

    def render_note(self) -> None:
        self._unload_all()
        while self._inner.count():
            item = self._inner.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._time:
                w.deleteLater()
        self._media.clear()

        note = self.note
        for att in note.attachments:
            # access_path: при шифровании отдаёт расшифрованный tmpfs-файл (QPixmap/
            # QMovie/плеер/внешнее открытие требуют реальный файл, не шифртекст).
            path = vault.attachment_access_path(note, att)
            if path is None:
                # blob зашифрован, но расшифровать не удалось — НЕ показываем шифртекст
                self._inner.addWidget(self._make_unavailable_chip(att))
                continue
            mime = att.mime or ""
            is_gif = str(path).lower().endswith(".gif") or mime == "image/gif"
            if mime.startswith("image/") and not is_gif:
                self._add_media_slot("image", att, path)
            elif is_gif:
                self._add_media_slot("gif", att, path)
            elif mime.startswith("video/") and MULTIMEDIA_OK:
                self._add_video_slot(att, path)
            else:
                chip = _FileChip(path, att.name or att.file, att.size or 0)
                chip.installEventFilter(self)
                self._inner.addWidget(chip)

        body = note.html if note.kind == "text" else note.caption_html
        if body:
            self._inner.addWidget(self._make_text_label(body))

        self._time.setText(_fmt_time(note.modified or note.created))
        self._inner.addWidget(self._time)

        img_w = self._media_img_w()
        for slot in self._media:
            self._apply_slot_size(slot, img_w)
        self._refresh_active()

    def _make_unavailable_chip(self, att) -> QLabel:
        """Плашка «вложение недоступно» — когда blob не расшифровался. Не кликабельна,
        не несёт шифртекста."""
        name = att.name or att.file or "вложение"
        lbl = QLabel(f"🔒 {name}   ·   недоступно")
        lbl.setObjectName("FileChip")
        lbl.setProperty("missing", "true")
        lbl.setContextMenuPolicy(Qt.NoContextMenu)
        return lbl

    def _add_media_slot(self, kind: str, att, path) -> None:
        lbl = _MediaLabel(path)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.installEventFilter(self)
        w0, h0 = self._dims(att, path)
        slot = {"kind": kind, "path": str(path), "w": w0, "h": h0,
                "widget": lbl, "loaded": False, "movie": None}
        self._media.append(slot)
        self._inner.addWidget(lbl, alignment=Qt.AlignHCenter)

    def _add_video_slot(self, att, path) -> None:
        holder = QFrame()
        holder.setObjectName("VideoHolder")
        holder.installEventFilter(self)
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        slot = {"kind": "video", "path": str(path), "w": att.w, "h": att.h,
                "widget": holder, "loaded": False, "video": None}
        self._media.append(slot)
        self._inner.addWidget(holder, alignment=Qt.AlignHCenter)

    def _make_text_label(self, html: str) -> QLabel:
        lbl = _NoteText()
        lbl.setObjectName("BubbleText")
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        lbl.setOpenExternalLinks(False)
        lbl.linkActivated.connect(self._on_link)
        pal = lbl.palette()
        pal.setColor(QPalette.Link, QColor(PALETTE["link"]))
        lbl.setPalette(pal)
        lbl.setContextMenuPolicy(Qt.NoContextMenu)
        lbl.installEventFilter(self)
        lbl.setText(colorize_links(strip_theme_overrides(linkify_references(html)), PALETTE["link"]))
        return lbl

    def _on_link(self, href: str) -> None:
        if href.startswith("qtnote:"):
            self.referenceActivated.emit(href[len("qtnote:"):])
        else:
            QDesktopServices.openUrl(QUrl(href))

    def contextMenuEvent(self, event):  # noqa: N802
        menu = QMenu(self)
        act_edit = menu.addAction("Изменить")
        act_copy = menu.addAction("Копировать текст")
        act_id = menu.addAction("Копировать ID-ссылку")
        menu.addSeparator()
        act_del = menu.addAction("Удалить заметку")
        chosen = menu.exec(event.globalPos())
        if chosen == act_edit:
            self.editRequested.emit(self.note.id)
        elif chosen == act_copy:
            QGuiApplication.clipboard().setText(self.note.plaintext or self.note.caption_html)
        elif chosen == act_id:
            QGuiApplication.clipboard().setText(f"[[{self.note.id}]]")
        elif chosen == act_del:
            self.deleteRequested.emit(self.note.id)
