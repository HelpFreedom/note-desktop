"""Лёгкие анимации интерфейса: появление, удаление, плавная прокрутка, пульс."""

from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtWidgets import QGraphicsOpacityEffect

FAST = 150


def fade_in(widget, duration: int = FAST) -> None:
    """Плавное появление виджета (opacity 0 → 1)."""
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setStartValue(0.0)
    anim.setEndValue(1.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def collapse_remove(widget, on_done, duration: int = FAST) -> None:
    """Лёгкое схлопывание по высоте, затем вызов on_done() (без opacity-эффекта)."""
    # снять минимальную высоту, иначе пузыри с медиа (видео/картинка имеют
    # фиксированную min-высоту) не сожмутся и исчезнут рывком
    widget.setMinimumHeight(0)
    collapse = QPropertyAnimation(widget, b"maximumHeight", widget)
    collapse.setDuration(duration)
    collapse.setStartValue(widget.height())
    collapse.setEndValue(0)
    collapse.setEasingCurve(QEasingCurve.InCubic)
    collapse.finished.connect(on_done)
    collapse.start(QAbstractAnimation.DeleteWhenStopped)


def smooth_scroll(scrollbar, target: int, duration: int = 220) -> None:
    """Плавная прокрутка полосы к значению target."""
    target = max(scrollbar.minimum(), min(scrollbar.maximum(), target))
    anim = QPropertyAnimation(scrollbar, b"value", scrollbar)
    anim.setDuration(duration)
    anim.setStartValue(scrollbar.value())
    anim.setEndValue(target)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def flash(widget, color: str, duration: int = 280) -> None:
    """Короткая вспышка цветом поверх виджета (отклик на выделение)."""
    from PySide6.QtWidgets import QFrame
    overlay = QFrame(widget)
    overlay.setStyleSheet(f"background:{color};border-radius:14px;")
    overlay.setGeometry(widget.rect())
    overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    overlay.show()
    eff = QGraphicsOpacityEffect(overlay)
    overlay.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", overlay)
    anim.setDuration(duration)
    anim.setStartValue(0.45)
    anim.setEndValue(0.0)
    anim.setEasingCurve(QEasingCurve.OutCubic)
    anim.finished.connect(overlay.deleteLater)
    anim.start(QAbstractAnimation.DeleteWhenStopped)


def pulse(widget, duration: int = 160) -> None:
    """Короткий «пульс» прозрачности — отклик на действие (выделение)."""
    eff = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(eff)
    anim = QPropertyAnimation(eff, b"opacity", widget)
    anim.setDuration(duration)
    anim.setKeyValueAt(0.0, 1.0)
    anim.setKeyValueAt(0.5, 0.55)
    anim.setKeyValueAt(1.0, 1.0)
    anim.finished.connect(lambda: widget.setGraphicsEffect(None))
    anim.start(QAbstractAnimation.DeleteWhenStopped)
