"""Календарь в стиле Google: сетка месяца на русском, неделя с понедельника,
события видны прямо в ячейках, перенос событий между датами drag-n-drop."""

from __future__ import annotations

from PySide6.QtCore import QDate, QMimeData, Qt, Signal
from PySide6.QtGui import QColor, QDrag
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..storage import vault
from .event_dialog import EventDialog

_FMT = "yyyy-MM-dd"
_MONTHS = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
           "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
_MAX_VISIBLE = 3
EVENT_MIME = "application/x-qtnote-event"


def _contrast_text(hex_color: str) -> str:
    """Чёрный или белый текст в зависимости от яркости фона."""
    c = QColor(hex_color)
    luma = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
    return "#1b1b1b" if luma > 150 else "#ffffff"


class EventChip(QLabel):
    """Цветная плашка события внутри ячейки дня. Drag → перенос, ПКМ → меню."""

    deleteRequested = Signal(str)
    editRequested = Signal(str)

    def __init__(self, event):
        super().__init__()
        # ВАЖНО: не присваивать self.event — это затрёт QWidget.event() и
        # сломает доставку событий (PySide вызывает event() как виртуальный).
        self._event = event
        self.setObjectName("CalEventChip")
        self.setText(event.name)
        self.setToolTip(event.name)
        self.setWordWrap(False)
        self.setTextFormat(Qt.PlainText)
        self.setFixedHeight(18)
        self.setStyleSheet(
            f"#CalEventChip{{background:{event.color};color:{_contrast_text(event.color)};"
            f"border-radius:4px;padding:1px 5px;font-size:11px;}}"
        )
        self.setCursor(Qt.OpenHandCursor)
        self._press = None

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):  # noqa: N802
        if not (e.buttons() & Qt.LeftButton) or self._press is None:
            return
        if (e.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(EVENT_MIME, self._event.id.encode("utf-8"))
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.exec(Qt.MoveAction)

    def contextMenuEvent(self, e):  # noqa: N802
        menu = QMenu(self)
        act_edit = menu.addAction("Изменить событие")
        act_del = menu.addAction("Удалить событие")
        chosen = menu.exec(e.globalPos())
        if chosen == act_edit:
            self.editRequested.emit(self._event.id)
        elif chosen == act_del:
            self.deleteRequested.emit(self._event.id)


class DayCell(QFrame):
    """Ячейка дня: номер + список событий. Принимает дропы и двойной клик."""

    addRequested = Signal(str)            # date_str
    eventDropped = Signal(str, str)       # event_id, new_date_str

    def __init__(self, qdate: QDate, in_month: bool, is_today: bool):
        super().__init__()
        self.qdate = qdate
        self.date_str = qdate.toString(_FMT)
        self.setObjectName("DayCell")
        self.setProperty("dim", "true" if not in_month else "false")
        self.setProperty("today", "true" if is_today else "false")
        self.setAcceptDrops(True)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 4)
        lay.setSpacing(2)

        num = QLabel(str(qdate.day()))
        num.setObjectName("DayNumberToday" if is_today else "DayNumber")
        num.setAlignment(Qt.AlignCenter if is_today else (Qt.AlignLeft | Qt.AlignTop))
        lay.addWidget(num, alignment=Qt.AlignLeft)

        self._events_box = QVBoxLayout()
        self._events_box.setSpacing(2)
        lay.addLayout(self._events_box)
        lay.addStretch(1)
        self._count = 0

    def add_chip(self, chip: EventChip) -> None:
        if self._count < _MAX_VISIBLE:
            self._events_box.addWidget(chip)
        else:
            if self._count == _MAX_VISIBLE:
                more = QLabel()
                more.setObjectName("MoreEvents")
                # I4: подсказка, что по клику на день видны все события (клик по ячейке
                # открывает день; ярлык — дочерний, клик пробрасывается в DayCell).
                more.setToolTip("Нажмите на день, чтобы увидеть все события")
                self._events_box.addWidget(more)
                self._more = more
            self._more.setText(f"+ ещё {self._count - _MAX_VISIBLE + 1}")
            chip.deleteLater()
        self._count += 1

    # --- drag-n-drop ---

    def dragEnterEvent(self, e):  # noqa: N802
        if e.mimeData().hasFormat(EVENT_MIME):
            e.acceptProposedAction()
            self._set_hover(True)

    def dragLeaveEvent(self, e):  # noqa: N802
        self._set_hover(False)

    def dropEvent(self, e):  # noqa: N802
        self._set_hover(False)
        if e.mimeData().hasFormat(EVENT_MIME):
            eid = bytes(e.mimeData().data(EVENT_MIME)).decode("utf-8")
            self.eventDropped.emit(eid, self.date_str)
            e.acceptProposedAction()

    def _set_hover(self, on: bool) -> None:
        self.setProperty("drop", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseDoubleClickEvent(self, e):  # noqa: N802
        self.addRequested.emit(self.date_str)


class CalendarView(QWidget):
    """Месячная сетка с навигацией и событиями в ячейках."""

    def __init__(self):
        super().__init__()
        today = QDate.currentDate()
        self._year = today.year()
        self._month = today.month()
        self._by_date: dict[str, list] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # навигация
        nav = QFrame()
        nav.setObjectName("CalHeader")
        nav.setFixedHeight(54)
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(16, 0, 16, 0)
        nl.setSpacing(8)
        prev = QPushButton("‹")
        prev.setObjectName("Ghost")
        prev.setFixedWidth(36)
        prev.clicked.connect(self._prev_month)
        nxt = QPushButton("›")
        nxt.setObjectName("Ghost")
        nxt.setFixedWidth(36)
        nxt.clicked.connect(self._next_month)
        self._title = QLabel()
        self._title.setObjectName("ChatTitle")
        today_btn = QPushButton("Сегодня")
        today_btn.setObjectName("Ghost")
        today_btn.clicked.connect(self._go_today)
        nl.addWidget(prev)
        nl.addWidget(nxt)
        nl.addWidget(self._title)
        nl.addStretch(1)
        nl.addWidget(today_btn)
        root.addWidget(nav)

        # шапка дней недели
        wd = QFrame()
        wd.setObjectName("WeekHeader")
        wl = QHBoxLayout(wd)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)
        for i, name in enumerate(_WEEKDAYS):
            lbl = QLabel(name)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setObjectName("WeekendLabel" if i >= 5 else "WeekdayLabel")
            wl.addWidget(lbl)
        root.addWidget(wd)

        # сетка
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(1)
        root.addWidget(self._grid_host, 1)

        self.reload()

    # --- данные / построение ---

    def reload(self) -> None:
        self._by_date = {}
        for ev in vault.list_events():
            self._by_date.setdefault(ev.date, []).append(ev)
        self._build_month()

    def _build_month(self) -> None:
        # очистить сетку
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        self._title.setText(f"{_MONTHS[self._month]} {self._year}")
        first = QDate(self._year, self._month, 1)
        # понедельник на/перед первым числом (dayOfWeek: Пн=1..Вс=7)
        start = first.addDays(-(first.dayOfWeek() - 1))
        today = QDate.currentDate()

        for row in range(6):
            self._grid.setRowStretch(row, 1)
            for col in range(7):
                self._grid.setColumnStretch(col, 1)
                d = start.addDays(row * 7 + col)
                cell = DayCell(d, d.month() == self._month, d == today)
                cell.addRequested.connect(self._add_event)
                cell.eventDropped.connect(self._move_event)
                for ev in self._by_date.get(d.toString(_FMT), []):
                    chip = EventChip(ev)
                    chip.deleteRequested.connect(self._delete_event)
                    chip.editRequested.connect(self._edit_event)
                    cell.add_chip(chip)
                self._grid.addWidget(cell, row, col)

    # --- навигация ---

    def _prev_month(self) -> None:
        self._month -= 1
        if self._month < 1:
            self._month = 12
            self._year -= 1
        self._build_month()

    def _next_month(self) -> None:
        self._month += 1
        if self._month > 12:
            self._month = 1
            self._year += 1
        self._build_month()

    def _go_today(self) -> None:
        t = QDate.currentDate()
        self._year, self._month = t.year(), t.month()
        self._build_month()

    def wheelEvent(self, e):  # noqa: N802
        # колесо листает месяцы
        if e.angleDelta().y() > 0:
            self._prev_month()
        else:
            self._next_month()
        e.accept()

    # --- действия ---

    def _date_label(self, date_str: str) -> str:
        qd = QDate.fromString(date_str, _FMT)
        return f"{qd.day()} {_MONTHS[qd.month()].lower()} {qd.year()}"

    def _add_event(self, date_str: str) -> None:
        dlg = EventDialog(self, date_label=self._date_label(date_str))
        if dlg.exec():
            name, color = dlg.values()
            vault.add_event(date_str, name, color)
            self.reload()

    def _edit_event(self, event_id: str) -> None:
        ev = next((e for e in vault.list_events() if e.id == event_id), None)
        if ev is None:
            return
        dlg = EventDialog(self, date_label=self._date_label(ev.date),
                          color=ev.color, name=ev.name, editing=True)
        if dlg.exec():
            name, color = dlg.values()
            vault.update_event(event_id, name=name, color=color)
            self.reload()

    def _delete_event(self, event_id: str) -> None:
        vault.delete_event(event_id)
        self.reload()

    def _move_event(self, event_id: str, new_date: str) -> None:
        vault.update_event(event_id, date=new_date)
        self.reload()
