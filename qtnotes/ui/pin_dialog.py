"""Экраны ПИН-кода: ввод (numpad + точки), настройка при первом запуске, разблокировка.

Стиль согласован с темой приложения (theme.PALETTE). Поддержка клавиатуры (цифры,
Backspace, Enter). Экран разблокировки показывает неудачи и обратный отсчёт блокировки.

Эти классы — чистый UI: вся криптология/решения снаружи (через колбэки), чтобы их
можно было собрать без GUI и переиспользовать.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

PIN_LEN = 5


def _p(key: str, fallback: str) -> str:
    return theme.PALETTE.get(key, fallback)


class _Dots(QWidget):
    """Индикатор введённых цифр: PIN_LEN точек, заполняются по мере ввода."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setSpacing(14)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        self._dots: list[QLabel] = []
        for _ in range(PIN_LEN):
            d = QLabel()
            d.setFixedSize(16, 16)
            self._dots.append(d)
            lay.addWidget(d)
        self.set_count(0)

    def set_count(self, n: int) -> None:
        accent = _p("accent", "#4a82bd")
        empty_border = _p("text_secondary", "#6b7c8e")
        for i, d in enumerate(self._dots):
            if i < n:
                d.setStyleSheet(f"background:{accent};border-radius:8px;")
            else:
                d.setStyleSheet(
                    f"background:transparent;border:2px solid {empty_border};"
                    "border-radius:8px;")


class PinEntry(QWidget):
    """Поле ввода ПИН: точки + numpad. Эмитит completed(pin) при наборе PIN_LEN цифр."""

    completed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pin = ""
        root = QVBoxLayout(self)
        root.setSpacing(16)
        root.setAlignment(Qt.AlignCenter)

        self._prompt = QLabel()
        self._prompt.setAlignment(Qt.AlignCenter)
        self._prompt.setStyleSheet(f"color:{_p('text', '#eef2f6')};font-size:16px;")
        root.addWidget(self._prompt)

        self._dots = _Dots()
        root.addWidget(self._dots, 0, Qt.AlignCenter)

        self._status = QLabel()
        self._status.setAlignment(Qt.AlignCenter)
        self._status.setMinimumHeight(20)
        self._status.setStyleSheet(f"color:{_p('danger', '#e06b6b')};font-size:13px;")
        root.addWidget(self._status)

        self._pad = self._build_pad()
        root.addWidget(self._pad, 0, Qt.AlignCenter)

    def _build_pad(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        grid.setSpacing(12)
        positions = [(str(n), (i // 3, i % 3)) for i, n in enumerate(range(1, 10))]
        positions.append(("0", (3, 1)))
        for label, (r, c) in positions:
            grid.addWidget(self._digit_button(label), r, c)
        back = self._digit_button("⌫")  # не цифра → _digit_button не подключал clicked
        back.clicked.connect(self._backspace)
        grid.addWidget(back, 3, 2)
        return w

    def _digit_button(self, label: str) -> QPushButton:
        b = QPushButton(label)
        b.setFixedSize(64, 64)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.setStyleSheet(
            f"QPushButton{{background:{_p('field_bg', '#1a232e')};"
            f"color:{_p('text', '#eef2f6')};border:none;border-radius:32px;"
            "font-size:22px;}"
            f"QPushButton:hover{{background:{_p('accent', '#4a82bd')};}}"
            f"QPushButton:pressed{{background:{_p('accent_hover', '#5a93ce')};}}"
            f"QPushButton:disabled{{color:{_p('text_secondary', '#6b7c8e')};}}")
        if label.isdigit():
            b.clicked.connect(lambda _=False, d=label: self._add_digit(d))
        return b

    # --- ввод ---

    def _add_digit(self, d: str) -> None:
        if len(self._pin) >= PIN_LEN:
            return
        self._status.setText("")
        self._pin += d
        self._dots.set_count(len(self._pin))
        if len(self._pin) == PIN_LEN:
            pin, self._pin = self._pin, ""
            # сброс точек делает вызывающий (после обработки), чтобы успеть показать полный набор
            QTimer.singleShot(60, lambda: self.completed.emit(pin))

    def _backspace(self) -> None:
        if self._pin:
            self._pin = self._pin[:-1]
            self._dots.set_count(len(self._pin))

    def reset(self) -> None:
        self._pin = ""
        self._dots.set_count(0)

    def set_prompt(self, text: str) -> None:
        self._prompt.setText(text)

    def set_status(self, text: str, error: bool = True) -> None:
        color = _p("danger", "#e06b6b") if error else _p("text_secondary", "#6b7c8e")
        self._status.setStyleSheet(f"color:{color};font-size:13px;")
        self._status.setText(text)

    def set_pad_enabled(self, on: bool) -> None:
        self._pad.setEnabled(on)

    def keyPressEvent(self, e):  # noqa: N802 — поддержка физической клавиатуры
        key = e.key()
        if Qt.Key_0 <= key <= Qt.Key_9:
            self._add_digit(chr(key))
        elif key in (Qt.Key_Backspace, Qt.Key_Delete):
            self._backspace()
        else:
            super().keyPressEvent(e)


class PinSetupDialog(QDialog):
    """Настройка ПИНа при первом запуске: ввод + подтверждение. pin() — результат."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Создание ПИН-кода")
        self.setModal(True)
        self._pin: str | None = None
        self._first: str | None = None

        root = QVBoxLayout(self)
        self._entry = PinEntry()
        self._entry.set_prompt("Придумайте 5-значный ПИН")
        self._entry.completed.connect(self._on_completed)
        root.addWidget(self._entry)
        self._entry.setFocus()

    def _on_completed(self, pin: str) -> None:
        from ..crypto import keyvault
        if self._first is None:
            try:
                keyvault.validate_pin(pin)
            except keyvault.PinError as e:
                self._entry.reset()
                self._entry.set_status(str(e))
                return
            self._first = pin
            self._entry.reset()
            self._entry.set_status("")
            self._entry.set_prompt("Повторите ПИН")
        else:
            if pin != self._first:
                self._first = None
                self._entry.reset()
                self._entry.set_prompt("Придумайте 5-значный ПИН")
                self._entry.set_status("ПИН не совпал — попробуйте снова")
                return
            self._pin = pin
            self.accept()

    def pin(self) -> str | None:
        return self._pin


class PinUnlockDialog(QDialog):
    """Ввод ПИНа при старте. check(pin) -> UnlockResult; remaining() -> сек блокировки.

    OK → accept(); WRONG/DURESS → сообщение и сброс; LOCKED → обратный отсчёт.
    Кнопка «Выход» закрывает приложение (reject)."""

    def __init__(self, check, remaining, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Введите ПИН")
        self.setModal(True)
        self._check = check
        self._remaining = remaining

        root = QVBoxLayout(self)
        self._entry = PinEntry()
        self._entry.set_prompt("Введите ПИН для разблокировки")
        self._entry.completed.connect(self._on_completed)
        root.addWidget(self._entry)

        exit_btn = QPushButton("Выход")
        exit_btn.setObjectName("Ghost")
        exit_btn.setCursor(Qt.PointingHandCursor)
        exit_btn.setFocusPolicy(Qt.NoFocus)
        exit_btn.clicked.connect(self.reject)
        root.addWidget(exit_btn, 0, Qt.AlignCenter)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self._entry.setFocus()
        self._refresh_lock()  # вдруг уже в блокировке

    def _on_completed(self, pin: str) -> None:
        from ..crypto.keyvault import UnlockStatus
        res = self._check(pin)
        self._entry.reset()
        if res.status is UnlockStatus.OK:
            self.accept()
        elif res.status is UnlockStatus.WIPED:
            # превышен лимит неверных ПИНов — данные стёрты, открываем пустое приложение
            self.accept()
        elif res.status is UnlockStatus.LOCKED:
            self._start_lock()
        else:
            # WRONG или DURESS (полный duress-сценарий — Ф6; пока выглядит как неверный)
            if self._remaining() > 0:
                self._start_lock()
            else:
                self._entry.set_status("Неверный ПИН")

    def _refresh_lock(self) -> None:
        if self._remaining() > 0:
            self._start_lock()

    def _start_lock(self) -> None:
        self._entry.set_pad_enabled(False)
        self._tick()
        if not self._timer.isActive():
            self._timer.start()

    def _tick(self) -> None:
        left = self._remaining()
        if left <= 0:
            self._timer.stop()
            self._entry.set_pad_enabled(True)
            self._entry.set_status("")
            return
        m, s = divmod(left, 60)
        self._entry.set_status(f"Слишком много попыток. Повторите через {m:02d}:{s:02d}")

    def keyPressEvent(self, e):  # noqa: N802 — не закрывать по Esc (это гейт)
        if e.key() == Qt.Key_Escape:
            return
        super().keyPressEvent(e)
