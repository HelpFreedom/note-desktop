"""Поле ввода с форматированием: авто-растущий QTextEdit + панель кнопок.

Переиспользуется в поле ввода ленты и в диалоге редактирования заметки.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import (
    QFont,
    QImage,
    QKeySequence,
    QPixmap,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import QFrame, QHBoxLayout, QTextEdit, QToolButton

from .theme import PALETTE


def image_from_mime(source) -> QImage | None:
    """Безопасно извлечь QImage из mime-данных (буфер может отдать QPixmap)."""
    if not source.hasImage():
        return None
    data = source.imageData()
    if isinstance(data, QImage):
        img = data
    elif isinstance(data, QPixmap):
        img = data.toImage()
    else:
        return None
    return img if not img.isNull() else None


class RichTextEdit(QTextEdit):
    """QTextEdit, который растёт по содержимому в пределах [min, max] высоты.

    Enter отправляет (сигнал submitted), Shift+Enter — перенос строки.
    Авто-рост и submit-by-enter включаются флагами.

    Вставка/дроп изображения или файлов перехватываются и отдаются наружу
    сигналами imagePasted/filesPasted (текст и форматирование вставляются как есть).
    """

    submitted = Signal()
    imagePasted = Signal(QImage)
    filesPasted = Signal(list)

    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802
        if source.hasImage() or source.hasUrls():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802
        # картинка из буфера/дропа → отдельная заметка-картинка
        img = image_from_mime(source)
        if img is not None:
            self.imagePasted.emit(img)
            return
        # файлы → вложения
        if source.hasUrls():
            paths = [u.toLocalFile() for u in source.urls() if u.isLocalFile()]
            if paths:
                self.filesPasted.emit(paths)
                return
        # обычный текст/HTML/эмодзи — вставляем с сохранением форматирования
        super().insertFromMimeData(source)

    def __init__(self, growing: bool = False, submit_on_enter: bool = False,
                 min_height: int = 44, max_height: int = 180):
        super().__init__()
        self._growing = growing
        self._submit_on_enter = submit_on_enter
        self._min_h = min_height
        self._max_h = max_height
        # ссылки в поле ввода — цветом темы
        self.document().setDefaultStyleSheet(f"a {{ color: {PALETTE['link']}; }}")
        if growing:
            self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.setFixedHeight(min_height)
            self.document().contentsChanged.connect(self._adjust_height)

    def _adjust_height(self) -> None:
        doc_h = self.document().size().height()
        margins = self.contentsMargins()
        extra = margins.top() + margins.bottom() + 12
        target = int(min(self._max_h, max(self._min_h, doc_h + extra)))
        if target != self.height():
            self.setFixedHeight(target)

    def keyPressEvent(self, event):  # noqa: N802
        if self._submit_on_enter and event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and not (event.modifiers() & Qt.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    # --- применение форматирования к выделению/курсору ---

    def _merge_format(self, fmt: QTextCharFormat) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.WordUnderCursor)
        cursor.mergeCharFormat(fmt)
        self.mergeCurrentCharFormat(fmt)
        self.setFocus()

    def toggle_bold(self) -> None:
        fmt = QTextCharFormat()
        bold = self.fontWeight() <= QFont.Normal
        fmt.setFontWeight(QFont.Bold if bold else QFont.Normal)
        self._merge_format(fmt)

    def toggle_italic(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(not self.fontItalic())
        self._merge_format(fmt)

    def toggle_underline(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontUnderline(not self.fontUnderline())
        self._merge_format(fmt)

    def toggle_strike(self) -> None:
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(not self.currentCharFormat().fontStrikeOut())
        self._merge_format(fmt)

    def has_formatting(self) -> bool:
        """Есть ли в документе хоть какое-то символьное форматирование."""
        block = self.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    cf = frag.charFormat()
                    if (cf.fontWeight() > QFont.Normal or cf.fontItalic()
                            or cf.fontUnderline() or cf.fontStrikeOut()):
                        return True
                it += 1
            block = block.next()
        return False


def _fmt_button(text: str, tooltip: str, shortcut, slot) -> QToolButton:
    btn = QToolButton()
    btn.setObjectName("FmtButton")
    btn.setText(text)
    btn.setToolTip(f"{tooltip} ({QKeySequence(shortcut).toString()})")
    btn.setCursor(Qt.PointingHandCursor)
    btn.setFixedSize(QSize(30, 28))
    btn.clicked.connect(slot)
    return btn


def build_format_toolbar(editor: RichTextEdit) -> QFrame:
    """Панель кнопок Ж/К/Ч/зачёркивание для редактора."""
    bar = QFrame()
    bar.setObjectName("FormatToolbar")
    lay = QHBoxLayout(bar)
    lay.setContentsMargins(6, 2, 6, 2)
    lay.setSpacing(4)

    b = _fmt_button("Ж", "Жирный", QKeySequence.Bold, editor.toggle_bold)
    b.setStyleSheet("font-weight:bold;")
    i = _fmt_button("К", "Курсив", QKeySequence.Italic, editor.toggle_italic)
    i.setStyleSheet("font-style:italic;")
    u = _fmt_button("Ч", "Подчёркнутый", QKeySequence.Underline, editor.toggle_underline)
    u.setStyleSheet("text-decoration:underline;")
    s = _fmt_button("S", "Зачёркнутый", QKeySequence("Ctrl+Shift+S"), editor.toggle_strike)
    s.setStyleSheet("text-decoration:line-through;")

    for w in (b, i, u, s):
        lay.addWidget(w)
    lay.addStretch(1)

    # горячие клавиши
    from PySide6.QtGui import QShortcut
    QShortcut(QKeySequence.Bold, editor, editor.toggle_bold)
    QShortcut(QKeySequence.Italic, editor, editor.toggle_italic)
    QShortcut(QKeySequence.Underline, editor, editor.toggle_underline)
    QShortcut(QKeySequence("Ctrl+Shift+S"), editor, editor.toggle_strike)
    return bar
