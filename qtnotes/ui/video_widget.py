"""Видео в ленте: автоплей без звука в превью, полноэкранный просмотр по клику.

Требует QtMultimedia (PySide6-Addons). Если недоступен — MULTIMEDIA_OK=False,
и вызывающий код покажет видео как обычный файл.
"""

from __future__ import annotations

from PySide6.QtCore import QUrl, Qt, Signal

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    MULTIMEDIA_OK = True
except ImportError:  # pragma: no cover
    MULTIMEDIA_OK = False


if MULTIMEDIA_OK:

    class _Player:
        """Обёртка плеера с замьюченным выводом."""

        def __init__(self, parent, video_widget, muted: bool, loop: bool):
            self.player = QMediaPlayer(parent)
            self.audio = QAudioOutput(parent)
            self.audio.setMuted(muted)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(video_widget)
            if loop:
                self.player.setLoops(QMediaPlayer.Infinite)

        def play_file(self, path: str):
            self.player.setSource(QUrl.fromLocalFile(path))
            self.player.play()

    # окна полноэкранного просмотра держим здесь, иначе их удалит сборщик
    _OPEN_FULLSCREEN: list = []

    class FullscreenVideo(QVideoWidget):
        """Полноэкранный просмотр со звуком; Esc/клик — закрыть."""

        def __init__(self, path: str):
            super().__init__()
            self.setWindowTitle("Просмотр видео")
            self._p = _Player(self, self, muted=False, loop=False)
            self._p.play_file(path)
            _OPEN_FULLSCREEN.append(self)

        def keyPressEvent(self, e):  # noqa: N802
            if e.key() == Qt.Key_Escape:
                self.close()
            else:
                super().keyPressEvent(e)

        def mouseReleaseEvent(self, e):  # noqa: N802
            self.close()

        def closeEvent(self, e):  # noqa: N802
            self._p.player.stop()
            if self in _OPEN_FULLSCREEN:
                _OPEN_FULLSCREEN.remove(self)
            super().closeEvent(e)

    class VideoPreview(QVideoWidget):
        """Превью видео: автоплей, без звука, в цикле. Клик → полный экран."""

        clicked = Signal()

        def __init__(self, path: str, width: int):
            super().__init__()
            self._path = path
            self.setAspectRatioMode(Qt.KeepAspectRatio)
            self.set_width(width)
            self._p = _Player(self, self, muted=True, loop=True)
            self._p.play_file(path)
            self.setCursor(Qt.PointingHandCursor)

        def set_width(self, width: int) -> None:
            w = max(160, width)
            self.setFixedSize(w, int(w * 9 / 16))

        def mouseReleaseEvent(self, e):  # noqa: N802
            if e.button() == Qt.LeftButton and not (e.modifiers() & Qt.ControlModifier):
                fs = FullscreenVideo(self._path)
                fs.showFullScreen()
                fs.raise_()
                fs.activateWindow()
            super().mouseReleaseEvent(e)

        def contextMenuEvent(self, e):  # noqa: N802
            from .mediamenu import show_media_menu
            show_media_menu(self, e.globalPos(), self._path)
