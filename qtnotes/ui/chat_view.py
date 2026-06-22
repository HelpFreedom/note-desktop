"""Правая область: лента заметок выбранной папки."""

from __future__ import annotations

import mimetypes
import shutil

from PySide6.QtCore import QEvent, QSize, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..storage import index
from ..storage import search as search_mod
from ..storage import vault
from ..storage.models import Attachment, Note, new_id
from . import anim
from .graphics import attach_icon, search_icon
from .message_bubble import MessageBubble
from .rich_input import RichTextEdit, build_format_toolbar, image_from_mime
from .textutils import linkify_plain, linkify_references


def _snippet(text: str, limit: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


class _ResultRow(QFrame):
    """Строка результата поиска: папка + фрагмент текста, кликабельна."""

    clicked = Signal(str)  # note_id

    def __init__(self, note_id: str, folder_name: str, snippet: str):
        super().__init__()
        self.setObjectName("ResultRow")
        self._note_id = note_id
        self.setCursor(Qt.PointingHandCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(2)
        top = QLabel(folder_name or "—")
        top.setObjectName("ResultFolder")
        lay.addWidget(top)
        body = QLabel(snippet or "(без текста)")
        body.setWordWrap(True)
        lay.addWidget(body)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._note_id)
        super().mouseReleaseEvent(event)


def _build_pending_note(folder_id, pending, caption_plain, caption_html):
    """M7: собрать заметку из вложений (копирование/декод/шифрование blob'ов) — тяжёлая
    работа, выполняется в фоновом потоке. Без доступа к виджетам (caption уже извлечён)."""
    from pathlib import Path
    note = Note(id=new_id(), folder_id=folder_id)
    images = videos = files = 0
    for it in pending:
        adir = vault.attachments_dir(folder_id, note.id)
        if it["type"] == "image":
            name = f"image_{images + 1}.png"
            dest = adir / name
            it["image"].save(str(dest), "PNG")
            att = Attachment(file=name, mime="image/png", name=name,
                             size=dest.stat().st_size,
                             w=it["image"].width(), h=it["image"].height())
            images += 1
        else:
            sp = Path(it["path"])
            if not sp.is_file():
                continue
            mime = mimetypes.guess_type(sp.name)[0] or "application/octet-stream"
            dest = adir / sp.name
            try:
                shutil.copy2(sp, dest)
            except OSError:
                continue
            att = Attachment(file=sp.name, mime=mime, name=sp.name, size=sp.stat().st_size)
            if mime.startswith("image/"):
                img = QImage(str(dest))
                att.w, att.h = img.width(), img.height()
                images += 1
            elif mime.startswith("video/"):
                videos += 1
            else:
                files += 1
        note.attachments.append(att)
    if not note.attachments:
        return None
    total = images + videos + files
    note.kind = "album" if total > 1 else (
        "image" if images == 1 else ("video" if videos == 1 else "file"))
    if caption_plain:
        note.caption_html = caption_html
        note.plaintext = caption_plain
    else:
        note.plaintext = ", ".join(a.name for a in note.attachments)
    vault.save_note(note)
    return note


class _FolderLoadWorker(QThread):
    """H7: декрипт заметок папки вне UI-потока (с шифрованием — тяжело). Возвращает
    plain-данные (Note), бабблы создаёт UI-поток. gen отсекает устаревшую загрузку."""
    loaded = Signal(str, int, object)  # folder_id, gen, list[Note]

    def __init__(self, folder_id, gen, parent=None):
        super().__init__(parent)
        self._fid = folder_id
        self._gen = gen

    def run(self) -> None:
        try:
            notes = vault.list_notes(self._fid)
        except Exception:  # noqa: BLE001 — пустая лента лучше падения потока
            notes = []
        self.loaded.emit(self._fid, self._gen, notes)


class _IngestWorker(QThread):
    """M7: приём вложений (копия+декод+шифр blob'ов) вне UI-потока."""
    done = Signal(object)     # Note | None
    failed = Signal(str)

    def __init__(self, folder_id, pending, caption_plain, caption_html, parent=None):
        super().__init__(parent)
        self._fid = folder_id
        self._pending = pending
        self._cap_plain = caption_plain
        self._cap_html = caption_html

    def run(self) -> None:
        try:
            note = _build_pending_note(self._fid, self._pending, self._cap_plain, self._cap_html)
            self.done.emit(note)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ChatView(QWidget):
    """Заголовок + прокручиваемая лента пузырей + поле ввода снизу."""

    noteSubmitted = Signal(str)
    moveNotesRequested = Signal(list)        # список note_id для переноса
    noteReferenceActivated = Signal(str)     # клик по ссылке [[id]]

    def __init__(self):
        super().__init__()
        self._current_folder = None
        self._bubbles: list[MessageBubble] = []
        self._load_gen = 0          # H7: поколение загрузки (отсекает устаревший результат)
        self._load_worker = None    # H7: текущий фоновый загрузчик папки
        self._ingest_worker = None  # M7: текущий фоновый приём вложений
        self._selected: set[str] = set()
        # отложенный переход к заметке/восстановление выделения — применяются по
        # завершении чанкового рендера (F3): цель/бабблы могут быть ещё не созданы.
        # tuple (значение, load_gen) — старое поколение игнорируем.
        self._pending_scroll = None     # (note_id, gen)
        self._pending_select = None     # (set[str], gen)
        self._editing_note = None
        self._pending: list[dict] = []   # вложения, ожидающие отправки
        self._autoscroll = True   # «прилипание» ленты к низу (как в Telegram)
        self._vis_pending = False  # коалесинг обновления видимого окна медиа

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- заголовок ---
        header = QFrame()
        header.setObjectName("ChatHeader")
        header.setFixedHeight(54)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        self._title = QLabel("Выберите папку")
        self._title.setObjectName("ChatTitle")
        hl.addWidget(self._title)
        hl.addStretch(1)

        # поиск в шапке: переключатель области + поле
        self._scope_btn = QPushButton("В папке")
        self._scope_btn.setObjectName("Ghost")
        self._scope_btn.setCheckable(True)
        self._scope_btn.setCursor(Qt.PointingHandCursor)
        self._scope_btn.setToolTip("Область поиска: текущая папка / везде")
        self._scope_btn.toggled.connect(self._on_scope_toggled)
        hl.addWidget(self._scope_btn)

        self._search = QLineEdit()
        self._search.setObjectName("SearchField")
        self._search.setPlaceholderText("Поиск по тексту или дате…")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(260)
        self._search.addAction(search_icon(18), QLineEdit.LeadingPosition)
        self._search.textChanged.connect(self._on_search_changed)
        hl.addWidget(self._search)
        root.addWidget(header)

        # дебаунс поиска
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._run_search)

        # --- панель действий над выделенными заметками ---
        self._selbar = QFrame()
        self._selbar.setObjectName("SelectionBar")
        self._selbar.setFixedHeight(48)
        sb = QHBoxLayout(self._selbar)
        sb.setContentsMargins(16, 0, 16, 0)
        sb.setSpacing(8)
        self._sel_label = QLabel("Выбрано: 0")
        sb.addWidget(self._sel_label)
        sb.addStretch(1)
        self._move_btn = QPushButton("Переместить")
        self._move_btn.setObjectName("Ghost")
        self._move_btn.clicked.connect(self._move_selected)
        self._del_btn = QPushButton("Удалить")
        self._del_btn.clicked.connect(self._delete_selected)
        self._cancel_sel_btn = QPushButton("Отмена")
        self._cancel_sel_btn.setObjectName("Ghost")
        self._cancel_sel_btn.clicked.connect(self._clear_selection)
        sb.addWidget(self._move_btn)
        sb.addWidget(self._del_btn)
        sb.addWidget(self._cancel_sel_btn)
        self._selbar.hide()
        root.addWidget(self._selbar)

        # --- лента ---
        self._scroll = QScrollArea()
        self._scroll.setObjectName("ChatScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self._feed = QWidget()
        self._feed.setObjectName("ChatFeed")
        self._feed_layout = QVBoxLayout(self._feed)
        self._feed_layout.setContentsMargins(16, 16, 16, 16)
        self._feed_layout.setSpacing(8)
        self._feed_layout.addStretch(1)

        self._empty = QLabel("Здесь пока нет заметок.\nНапишите первую в поле ниже.")
        self._empty.setObjectName("EmptyState")
        self._empty.setAlignment(Qt.AlignCenter)
        self._feed_layout.insertWidget(0, self._empty)

        self._scroll.setWidget(self._feed)

        # «прилипание к низу»: когда раскладка ленты меняет диапазон прокрутки
        # (вставка пузырей, дозагрузка картинок), доводим до самого низа — но
        # только если пользователь не ушёл вверх читать историю.
        sbar = self._scroll.verticalScrollBar()
        sbar.rangeChanged.connect(self._on_scroll_range)
        sbar.valueChanged.connect(self._on_scroll_value)

        # страница результатов поиска
        self._results_scroll = QScrollArea()
        self._results_scroll.setObjectName("ChatScroll")
        self._results_scroll.setWidgetResizable(True)
        self._results_scroll.setFrameShape(QFrame.NoFrame)
        self._results_host = QWidget()
        self._results_host.setObjectName("ResultsHost")
        self._results_layout = QVBoxLayout(self._results_host)
        self._results_layout.setContentsMargins(16, 16, 16, 16)
        self._results_layout.setSpacing(6)
        self._results_layout.addStretch(1)
        self._results_scroll.setWidget(self._results_host)

        self._content_stack = QStackedWidget()
        self._content_stack.setObjectName("ChatContent")
        self._content_stack.addWidget(self._scroll)           # 0 — лента
        self._content_stack.addWidget(self._results_scroll)   # 1 — результаты
        root.addWidget(self._content_stack, 1)

        # --- ввод ---
        root.addWidget(self._build_input_bar())
        self._set_enabled(False)

        # авто-фокус: вставка/клик без предварительного выбора поля
        self._scroll.viewport().installEventFilter(self)
        paste_sc = QShortcut(QKeySequence.Paste, self)
        paste_sc.setContext(Qt.WidgetWithChildrenShortcut)
        paste_sc.activated.connect(self._paste_into_input)

        # Esc — отмена правки/поиска/выделения; Ctrl+F — фокус в поиск
        esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        esc.setContext(Qt.WidgetWithChildrenShortcut)
        esc.activated.connect(self._on_escape)
        find = QShortcut(QKeySequence.Find, self)
        find.setContext(Qt.WidgetWithChildrenShortcut)
        find.activated.connect(self._search.setFocus)

        # перетаскивание файлов в любую часть ленты
        self.setAcceptDrops(True)

        # фон главного окна — обои рабочего стола (как в Telegram-теме walogram)
        from .theme import wallpaper_path
        wp = wallpaper_path()
        self._wallpaper = QPixmap(wp) if wp else QPixmap()
        self._wall_scaled = None
        self._wall_for_size = None   # размер, под который посчитан _wall_scaled
        # прозрачность слоёв ленты задаётся в QSS по именам объектов
        # (#ChatContent/#ChatFeed/#ResultsHost), чтобы НЕ задеть пузыри.
        if not self._wallpaper.isNull():
            self._scroll.viewport().setAutoFillBackground(False)
            self._results_scroll.viewport().setAutoFillBackground(False)

    def _make_blurred(self, size):
        """Заполнить размер обоями (cover) с лёгким размытием (down/up-scale)."""
        from PySide6.QtCore import QSize
        cover = self._wallpaper.scaled(size, Qt.KeepAspectRatioByExpanding,
                                       Qt.SmoothTransformation)
        small = QSize(max(1, cover.width() // 14), max(1, cover.height() // 14))
        blurred = cover.scaled(small, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        return blurred.scaled(cover.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)

    def paintEvent(self, e):  # noqa: N802
        if not self._wallpaper.isNull():
            from PySide6.QtGui import QColor, QPainter
            p = QPainter(self)
            if self._wall_scaled is None or self._wall_for_size != self.size():
                self._wall_scaled = self._make_blurred(self.size())
                self._wall_for_size = self.size()
            x = (self.width() - self._wall_scaled.width()) // 2
            y = (self.height() - self._wall_scaled.height()) // 2
            # рисуем ТОЛЬКО грязный прямоугольник (дёшево при частичных обновлениях)
            r = e.rect()
            src = r.translated(-x, -y)
            p.drawPixmap(r, self._wall_scaled, src)
            p.fillRect(r, QColor(0, 0, 0, 165))  # затемнение
            return  # не даём теме перекрасить обои
        super().paintEvent(e)

    def _on_escape(self) -> None:
        if self._editing_note is not None:
            self._cancel_edit()
        elif self._search.text():
            self._clear_search()
        elif self._selected:
            self._clear_selection()

    # --- drag-n-drop файлов ---

    def dragEnterEvent(self, event):  # noqa: N802
        if self._current_folder is not None and (
            event.mimeData().hasUrls() or event.mimeData().hasImage()
        ):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        if self._current_folder is None:
            return
        md = event.mimeData()
        if md.hasUrls():
            paths = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if paths:
                self._attach_paths(paths)
                event.acceptProposedAction()
                return
        img = image_from_mime(md)
        if img is not None:
            self._attach_image(img)
            event.acceptProposedAction()

    def eventFilter(self, obj, event):  # noqa: N802
        # клик по пустой области ленты переводит фокус в поле ввода
        if obj is self._scroll.viewport() and event.type() == QEvent.MouseButtonPress:
            if self._current_folder is not None:
                self._field.setFocus()
        return super().eventFilter(obj, event)

    def _paste_into_input(self) -> None:
        if self._current_folder is None:
            return
        self._field.setFocus()
        self._field.paste()

    # --- поиск ---

    def _on_scope_toggled(self, checked: bool) -> None:
        self._scope_btn.setText("Везде" if checked else "В папке")
        if self._search.text().strip():
            self._run_search()

    def _on_search_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _run_search(self) -> None:
        query = self._search.text().strip()
        if not query:
            self._content_stack.setCurrentIndex(0)
            return
        folder_id = None
        if not self._scope_btn.isChecked() and self._current_folder is not None:
            folder_id = self._current_folder.id
        hits = search_mod.search(query, folder_id=folder_id)
        self._show_results(hits)

    def _show_results(self, hits) -> None:
        while self._results_layout.count() > 1:
            item = self._results_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        if not hits:
            lbl = QLabel("Ничего не найдено")
            lbl.setObjectName("EmptyState")
            lbl.setAlignment(Qt.AlignCenter)
            self._results_layout.insertWidget(0, lbl)
        else:
            names = {f.id: f.name for f in vault.list_folders()}
            for hit in hits:
                row = _ResultRow(hit.note.id, names.get(hit.folder_id, ""),
                                 _snippet(hit.note.plaintext))
                row.clicked.connect(self._open_result)
                self._results_layout.insertWidget(self._results_layout.count() - 1, row)
        self._content_stack.setCurrentIndex(1)

    def _open_result(self, note_id: str) -> None:
        self._search.clear()  # вернёт обычную ленту
        self.noteReferenceActivated.emit(note_id)

    def _clear_search(self) -> None:
        self._search.blockSignals(True)
        self._search.clear()
        self._search.blockSignals(False)
        self._content_stack.setCurrentIndex(0)

    # --- UI ввода ---

    def _build_input_bar(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("InputBar")
        col = QVBoxLayout(wrap)
        col.setContentsMargins(10, 6, 10, 8)
        col.setSpacing(4)

        # баннер режима редактирования (скрыт по умолчанию)
        self._edit_banner = QFrame()
        self._edit_banner.setObjectName("EditBanner")
        eb = QHBoxLayout(self._edit_banner)
        eb.setContentsMargins(10, 4, 6, 4)
        self._edit_label = QLabel("Редактирование заметки")
        eb.addWidget(self._edit_label)
        eb.addStretch(1)
        cancel = QPushButton("Отмена")
        cancel.setObjectName("Ghost")
        cancel.clicked.connect(self._cancel_edit)
        eb.addWidget(cancel)
        self._edit_banner.hide()
        col.addWidget(self._edit_banner)

        # лоток ожидающих вложений (фото/видео/файлы) с подписью
        self._tray = QFrame()
        self._tray.setObjectName("PendingTray")
        self._tray_layout = QHBoxLayout(self._tray)
        self._tray_layout.setContentsMargins(6, 4, 6, 4)
        self._tray_layout.setSpacing(6)
        self._tray_layout.addStretch(1)
        self._tray.hide()
        col.addWidget(self._tray)

        self._field = RichTextEdit(growing=True, submit_on_enter=True)
        self._field.setObjectName("InputField")
        self._field.setPlaceholderText("Напишите заметку…  (Enter — отправить, Shift+Enter — перенос)")
        self._field.submitted.connect(self._submit)
        self._field.imagePasted.connect(self._attach_image)
        self._field.filesPasted.connect(self._attach_paths)

        # панель форматирования
        col.addWidget(build_format_toolbar(self._field))

        row = QHBoxLayout()
        row.setSpacing(8)

        self._attach = QToolButton()
        self._attach.setObjectName("SidebarButton")
        self._attach.setIcon(attach_icon())
        self._attach.setIconSize(QSize(24, 24))
        self._attach.setFixedSize(38, 38)
        self._attach.setCursor(Qt.PointingHandCursor)
        self._attach.setToolTip("Прикрепить файл")
        self._attach.clicked.connect(self._attach_files)
        row.addWidget(self._attach)

        row.addWidget(self._field, 1)

        self._send = QPushButton("Отправить")
        self._send.clicked.connect(self._submit)
        row.addWidget(self._send)

        col.addLayout(row)
        return wrap

    def _set_enabled(self, on: bool) -> None:
        self._field.setEnabled(on)
        self._send.setEnabled(on)
        self._attach.setEnabled(on)

    # --- лента ---

    def _has_unsaved(self) -> bool:
        """M2: есть ли несохранённый ввод (текст/вложения/правка) для защиты от потери."""
        return (bool(self._field.toPlainText().strip()) or bool(self._pending)
                or self._editing_note is not None)

    def _confirm_discard(self) -> bool:
        from PySide6.QtWidgets import QMessageBox
        r = QMessageBox.question(
            self, "Несохранённый ввод", "Сбросить набранный текст и вложения?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
        return r == QMessageBox.StandardButton.Discard

    def _error(self, title: str, e: Exception) -> None:
        """M3: видимая ошибка вместо тихого провала."""
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "QtNotes", f"{title}:\n{e}")

    def refresh_folder(self, folder) -> None:
        """F1 (раунд-3): синк-обновление ленты ТЕКУЩЕЙ папки БЕЗ discard-гарда и без
        сброса ввода. Раньше входящий синк шёл через show_folder с новым объектом папки →
        всплывала модалка «Сбросить несохранённое?» и, при отмене, лента не обновлялась.
        Здесь сохраняем набранный текст/вложения/правку и лишь перезагружаем бабблы."""
        if folder is None or self._current_folder is None:
            return
        if folder.id != self._current_folder.id:
            return
        self._current_folder = folder  # поля папки могли измениться (имя/цвет)
        self._title.setText(folder.name)
        keep_sel = set(self._selected)   # снимок выделения — _clear_bubbles его очистит
        self._clear_bubbles()
        self._load_gen += 1  # H7: пометить новую загрузку (старый результат отбросить)
        if keep_sel:
            self._pending_select = (keep_sel, self._load_gen)  # вернём после ре-рендера
        worker = _FolderLoadWorker(folder.id, self._load_gen, self)
        worker.loaded.connect(self._on_folder_loaded)
        worker.finished.connect(worker.deleteLater)
        self._load_worker = worker
        worker.start()

    def show_folder(self, folder) -> None:
        # M2: не терять несохранённый ввод при переключении папки (молча очищалось)
        if (folder is not self._current_folder and self._has_unsaved()
                and not self._confirm_discard()):
            return
        self._cancel_edit()
        self._clear_search()
        self._clear_pending()
        self._current_folder = folder
        self._clear_bubbles()
        self._load_gen += 1  # H7: помечаем новую загрузку (старый результат отбросим)
        if folder is None:
            self._title.setText("Выберите папку")
            self._set_enabled(False)
            self._empty.show()
            return
        self._title.setText(folder.name)
        self._set_enabled(True)
        self._empty.hide()
        # H7: декрипт заметок вне UI-потока (с шифрованием — тяжело); бабблы — по готовности
        worker = _FolderLoadWorker(folder.id, self._load_gen, self)
        worker.loaded.connect(self._on_folder_loaded)
        worker.finished.connect(worker.deleteLater)
        self._load_worker = worker
        worker.start()

    def _on_folder_loaded(self, folder_id, gen, notes) -> None:
        if gen != self._load_gen:
            return  # пользователь уже переключил папку — устаревший результат игнорируем
        # F3 (раунд-3): создаём бабблы ЧАНКАМИ, уступая event-loop между ними — большая
        # папка (тысячи заметок) не морозит UI-поток на конструировании виджетов разом.
        self._render_chunk(list(notes), 0, gen)

    def _render_chunk(self, notes, start, gen) -> None:
        if gen != self._load_gen:
            return  # папку переключили — прекращаем рендер устаревшего списка
        chunk = 40
        end = min(start + chunk, len(notes))
        for i in range(start, end):
            self._add_bubble(notes[i], scroll=False)
        if end < len(notes):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._render_chunk(notes, end, gen))
            return
        # последний чанк — финализируем ленту
        self._update_empty()
        self._restore_pending_selection(gen)
        # отложенный переход к заметке (F3): теперь все бабблы есть. Если цель так и не
        # нашлась — её нет в этой папке, сбрасываем и прокручиваем к низу как обычно.
        if not self._try_scroll_pending(gen):
            if self._pending_scroll is not None and self._pending_scroll[1] == gen:
                self._pending_scroll = None
            self._scroll_to_bottom(animated=False)
        self._schedule_media_update()
        self._field.setFocus()

    def _restore_pending_selection(self, gen) -> None:
        """Вернуть выделение, снятое при sync-refresh (refresh_folder), на уцелевшие
        бабблы — чтобы синк не сбрасывал выбор пользователя посреди действия."""
        ps = self._pending_select
        if ps is None or ps[1] != gen or gen != self._load_gen:
            return
        keep, _ = ps
        self._pending_select = None
        self._selected = {b.note.id for b in self._bubbles if b.note.id in keep}
        for b in self._bubbles:
            b.set_selected(b.note.id in self._selected)
        self._update_selbar()

    def _clear_bubbles(self) -> None:
        for b in self._bubbles:
            b.set_active(False)   # остановить видео/гиф до удаления
            b.setParent(None)
            b.deleteLater()
        self._bubbles.clear()
        self._selected.clear()
        self._update_selbar()

    def _add_bubble(self, note: Note, scroll: bool = True) -> None:
        bubble = MessageBubble(note)
        bubble.deleteRequested.connect(self._on_delete_note)
        bubble.editRequested.connect(self._on_edit_note)
        bubble.toggleSelectionRequested.connect(self._toggle_selection)
        bubble.referenceActivated.connect(self.noteReferenceActivated.emit)
        bubble.set_max_width(self._bubble_max_width())
        self._feed_layout.insertWidget(self._feed_layout.count() - 1, bubble)
        self._bubbles.append(bubble)
        self._update_empty()
        if scroll:
            self._scroll_to_bottom(animated=True)
        self._schedule_media_update()

    # --- адаптивная ширина ---

    def _bubble_max_width(self) -> int:
        # пузырь занимает всю ширину поля (минус отступы ленты)
        vw = self._scroll.viewport().width()
        return max(200, vw - 32)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        w = self._bubble_max_width()
        for b in self._bubbles:
            b.set_max_width(w)
        self._schedule_media_update()

    # --- выделение ---

    def _toggle_selection(self, note_id: str) -> None:
        if note_id in self._selected:
            self._selected.discard(note_id)
        else:
            self._selected.add(note_id)
        for b in self._bubbles:
            if b.note.id == note_id:
                b.set_selected(note_id in self._selected, animate=True)
        self._update_selbar()

    def _clear_selection(self) -> None:
        self._selected.clear()
        for b in self._bubbles:
            b.set_selected(False)
        self._update_selbar()

    def _update_selbar(self) -> None:
        n = len(self._selected)
        self._sel_label.setText(f"Выбрано: {n}")
        self._selbar.setVisible(n > 0)

    def _delete_selected(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if not self._selected:
            return
        ids = set(self._selected)
        n = len(ids)
        # ссылки на удаляемые из заметок ВНЕ выборки станут нерабочими
        ext_refs: set[str] = set()
        for nid in ids:
            ext_refs.update(r for r in index.referrers(nid) if r not in ids)
        text = f"Удалить выбранные заметки ({n})? Это необратимо."
        if ext_refs:
            text += (f"\n\nНа некоторые из них ссылаются другие заметки "
                     f"({len(ext_refs)}) — эти ссылки перестанут работать.")
        reply = QMessageBox.question(
            self, "Удалить заметки", text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        self._selbar.hide()
        for b in list(self._bubbles):
            if b.note.id in ids:
                vault.delete_note(b.note)
                self._remove_bubble_animated(b)
        self._selected.clear()
        self._update_selbar()

    def _remove_bubble_animated(self, b: MessageBubble) -> None:
        if b not in self._bubbles:
            return
        self._bubbles.remove(b)

        def done():
            b.setParent(None)
            b.deleteLater()
            # лента полупрозрачна (обои рисует ChatView снизу): после удаления
            # пузыря надо перерисовать стек, иначе остаются «прилипшие» пиксели
            self._feed_layout.activate()
            self._scroll.viewport().update()
            self.update()
            self._update_empty()
            self._schedule_media_update()   # позиции сдвинулись — пересчитать окно

        anim.collapse_remove(b, done)

    def _move_selected(self) -> None:
        if self._selected:
            self.moveNotesRequested.emit(list(self._selected))

    def selected_ids(self) -> list[str]:
        return list(self._selected)

    # --- навигация по ссылкам ---

    def scroll_to_note(self, note_id: str) -> None:
        # переходим к конкретной заметке — не прилипаем к низу
        self._autoscroll = False
        # F3: рендер идёт чанками — цель может быть ещё не создана. Запоминаем как
        # отложенную; пробуем сразу, а финал чанкового рендера добьёт (см. _render_chunk).
        self._pending_scroll = (note_id, self._load_gen)
        gen = self._load_gen
        QTimer.singleShot(0, lambda: self._try_scroll_pending(gen))

    def _try_scroll_pending(self, gen) -> bool:
        """Прокрутить к отложенной цели, если её баббл уже отрендерен. Возвращает True,
        если цель найдена и обработана. Если нет — НЕ сбрасывает отложенность (поздние
        чанки ещё могут её создать); финал рендера решит, что цели нет."""
        ps = self._pending_scroll
        if ps is None or ps[1] != gen or gen != self._load_gen:
            return False
        note_id = ps[0]
        for b in self._bubbles:
            if b.note.id == note_id:
                self._pending_scroll = None
                bar = self._scroll.verticalScrollBar()
                anim.smooth_scroll(bar, max(0, b.y() - 40))
                b.set_selected(True, animate=True)
                QTimer.singleShot(
                    1100, lambda bb=b: bb.set_selected(bb.note.id in self._selected)
                )
                return True
        return False

    def _update_empty(self) -> None:
        self._empty.setVisible(not self._bubbles and self._current_folder is not None)

    # --- прокрутка/прилипание к низу ---

    def _on_scroll_range(self, _vmin: int, vmax: int) -> None:
        # диапазон вырос (новый пузырь / картинка догрузилась) — если «прилипли»
        # к низу, остаёмся внизу. Это и даёт показ снизу при открытии папки.
        if self._autoscroll:
            self._scroll.verticalScrollBar().setValue(vmax)
        self._schedule_media_update()

    def _on_scroll_value(self, value: int) -> None:
        bar = self._scroll.verticalScrollBar()
        # у самого низа (с запасом) → продолжаем прилипать; ушёл вверх → нет
        self._autoscroll = value >= bar.maximum() - 48
        self._schedule_media_update()

    # --- виртуализация: тяжёлое медиа живёт только у видимых пузырей ---

    def _schedule_media_update(self) -> None:
        # коалесим частые события прокрутки в один проход за такт цикла событий
        if self._vis_pending:
            return
        self._vis_pending = True
        QTimer.singleShot(0, self._update_visible_media)

    def _update_visible_media(self) -> None:
        self._vis_pending = False
        if not self._bubbles:
            return
        # форсируем пересчёт раскладки: иначе сразу после добавления/перезагрузки
        # пузырей (например, при синке новых медиа) их y()/height() ещё не посчитаны,
        # пузырь ошибочно считается невидимым и медиа не грузится (тёмное место до
        # ручной прокрутки).
        self._feed_layout.activate()
        bar = self._scroll.verticalScrollBar()
        vh = self._scroll.viewport().height()
        margin = vh // 2   # окно активности: видимая область ± пол-экрана
        # КОРЕНЬ БАГА: сразу после перезагрузки ленты (в т.ч. при синке) диапазон и
        # позиция скроллбара ещё НЕ пересчитаны QScrollArea (это происходит отложенно),
        # поэтому bar.value()/maximum() — стейл, и окно видимости оказывается не там →
        # нижние видимые пузыри не активируются (тёмное место до ручной прокрутки).
        # Когда лента «прилипла» к низу, берём окно из РЕАЛЬНОЙ геометрии пузырей
        # (низ контента), а не из ненадёжного скроллбара.
        if self._autoscroll:
            content_bottom = max(b.y() + b.height() for b in self._bubbles)
            win_bot = content_bottom + margin
            win_top = content_bottom - vh - margin
        else:
            top = bar.value()
            win_top = top - margin
            win_bot = top + vh + margin
        n_active = 0
        for b in self._bubbles:
            y = b.y()
            active = (y + b.height()) >= win_top and y <= win_bot
            b.set_active(active)
            if active:
                n_active += 1
        import os
        if os.environ.get("QTNOTES_DEBUG_MEDIA"):
            print(f"[media] bubbles={len(self._bubbles)} active={n_active} "
                  f"autoscroll={self._autoscroll} win=({win_top},{win_bot}) "
                  f"vh={vh} bar.value={bar.value()} bar.max={bar.maximum()}", flush=True)

    def _scroll_to_bottom(self, animated: bool = False) -> None:
        # включаем прилипание и доводим до низа; rangeChanged добьёт, когда
        # раскладка/картинки досчитают высоту (singleShot — для мгновенного случая)
        self._autoscroll = True

        def go():
            bar = self._scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        QTimer.singleShot(0, go)

    # --- действия ---

    def _submit(self) -> None:
        if self._current_folder is None:
            return
        plaintext = self._field.toPlainText().strip()
        if self._editing_note is not None:
            self._save_edit(plaintext)
            return
        if self._pending:
            self._send_pending(plaintext)
            return
        if not plaintext:
            return
        html = self._field.toHtml() if self._field.has_formatting() else linkify_plain(plaintext)
        note = Note.create_text(self._current_folder.id, html, plaintext)
        try:
            vault.save_note(note)
        except Exception as e:  # noqa: BLE001 — M3: показать ошибку, не терять ввод
            self._error("Не удалось сохранить заметку", e)
            return
        self._add_bubble(note)
        self._reset_input()
        self.noteSubmitted.emit(plaintext)

    def _reset_input(self) -> None:
        """Очистить поле, сбросить «прилипшее» форматирование, вернуть фокус."""
        from PySide6.QtGui import QTextCharFormat
        self._field.clear()
        self._field.setCurrentCharFormat(QTextCharFormat())
        self._field.setFocus()

    # --- инлайн-редактирование (в основном поле, без модального окна) ---

    def _save_edit(self, plaintext: str) -> None:
        note = self._editing_note
        html = self._field.toHtml() if self._field.has_formatting() else linkify_plain(plaintext)
        if note.kind == "text":
            note.html = html
        else:
            note.caption_html = html
        note.plaintext = plaintext
        note.touch()
        try:
            vault.save_note(note)
        except Exception as e:  # noqa: BLE001 — M3
            self._error("Не удалось сохранить изменения", e)
            return
        for b in self._bubbles:
            if b.note.id == note.id:
                b.render_note()
                b.set_max_width(self._bubble_max_width())
                break
        self._cancel_edit()

    def _cancel_edit(self) -> None:
        if self._editing_note is None and not self._edit_banner.isVisible():
            return
        self._editing_note = None
        self._edit_banner.hide()
        self._send.setText("Отправить")
        self._reset_input()

    # --- вложения откладываются в лоток до отправки (как в Telegram) ---

    def _attach_files(self) -> None:
        if self._current_folder is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Прикрепить файлы")
        if paths:
            self._attach_paths(paths)

    def _attach_paths(self, paths: list[str]) -> None:
        from pathlib import Path
        if self._current_folder is None:
            return
        for p in paths:
            sp = Path(p)
            if sp.is_file():
                self._pending.append({"id": new_id(), "type": "path",
                                      "path": str(sp), "name": sp.name})
        self._refresh_tray()
        self._field.setFocus()

    def _attach_image(self, image: QImage) -> None:
        if self._current_folder is None or image.isNull():
            return
        self._pending.append({"id": new_id(), "type": "image",
                              "image": image, "name": "изображение"})
        self._refresh_tray()
        self._field.setFocus()

    def _remove_pending(self, pid: str) -> None:
        self._pending = [it for it in self._pending if it["id"] != pid]
        self._refresh_tray()

    def _clear_pending(self) -> None:
        self._pending = []
        self._refresh_tray()

    def _refresh_tray(self) -> None:
        while self._tray_layout.count() > 1:
            item = self._tray_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()
        for it in self._pending:
            self._tray_layout.insertWidget(self._tray_layout.count() - 1, self._make_pending_chip(it))
        self._tray.setVisible(bool(self._pending))

    def _make_pending_chip(self, it: dict) -> QWidget:
        chip = QFrame()
        chip.setObjectName("PendingChip")
        lay = QHBoxLayout(chip)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        mime = mimetypes.guess_type(it["name"])[0] or ""
        if it["type"] == "image" or mime.startswith("image/"):
            thumb = QLabel()
            if it["type"] == "image":
                pm = QPixmap.fromImage(it["image"])
            else:
                pm = QPixmap(it["path"])
            if not pm.isNull():
                thumb.setPixmap(pm.scaled(44, 44, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation))
            thumb.setFixedSize(44, 44)
            thumb.setScaledContents(True)
            lay.addWidget(thumb)
        else:
            icon = "🎬" if mime.startswith("video/") else "📄"
            lbl = QLabel(f"{icon} {it['name'][:18]}")
            lay.addWidget(lbl)
        rm = QPushButton("✕")
        rm.setObjectName("Ghost")
        rm.setFixedSize(20, 20)
        rm.setCursor(Qt.PointingHandCursor)
        rm.clicked.connect(lambda _=False, pid=it["id"]: self._remove_pending(pid))
        lay.addWidget(rm)
        return chip

    def _send_pending(self, caption: str) -> None:
        # M7: тяжёлый приём (копия/декод/шифр blob'ов) уходит в фоновый поток. caption-html
        # извлекаем здесь (доступ к виджету), снимок вложений передаём в поток.
        folder_id = self._current_folder.id
        caption_html = ((self._field.toHtml() if self._field.has_formatting()
                         else linkify_plain(caption)) if caption else "")
        pending = list(self._pending)
        self._clear_pending()     # лоток очищаем сразу (как на мобилке)
        self._set_sending(True)
        worker = _IngestWorker(folder_id, pending, caption, caption_html, self)
        worker.done.connect(self._on_ingest_done)
        worker.failed.connect(self._on_ingest_failed)
        worker.finished.connect(worker.deleteLater)
        self._ingest_worker = worker
        worker.start()

    def _set_sending(self, on: bool) -> None:
        if self._editing_note is None:
            self._send.setText("Отправка…" if on else "Отправить")
        self._send.setEnabled(not on)

    def _on_ingest_done(self, note) -> None:
        self._set_sending(False)
        if note is not None:
            self._add_bubble(note)

    def _on_ingest_failed(self, msg: str) -> None:
        self._set_sending(False)
        self._error("Не удалось отправить вложения", Exception(msg))
        self._reset_input()

    def _on_edit_note(self, note_id: str) -> None:
        for b in self._bubbles:
            if b.note.id == note_id:
                note = b.note
                content = note.html if note.kind == "text" else note.caption_html
                self._editing_note = note
                self._field.setHtml(content or "")
                self._edit_label.setText(
                    "Изменение заметки" if note.kind == "text" else "Изменение подписи"
                )
                self._edit_banner.show()
                self._send.setText("Сохранить")
                self._field.setFocus()
                cur = self._field.textCursor()
                cur.movePosition(cur.MoveOperation.End)
                self._field.setTextCursor(cur)
                break

    def _on_delete_note(self, note_id: str) -> None:
        refs = [r for r in index.referrers(note_id) if r != note_id]
        if refs and not self._confirm_referenced_delete(len(refs)):
            return
        for b in list(self._bubbles):
            if b.note.id == note_id:
                vault.delete_note(b.note)
                self._selected.discard(note_id)
                self._remove_bubble_animated(b)
                break
        self._update_selbar()

    def _confirm_referenced_delete(self, ref_count: int) -> bool:
        from PySide6.QtWidgets import QMessageBox
        reply = QMessageBox.question(
            self, "Удаление заметки",
            f"На эту заметку ссылаются другие заметки ({ref_count}).\n"
            "После удаления эти ссылки перестанут работать. Удалить?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        return reply == QMessageBox.Yes
