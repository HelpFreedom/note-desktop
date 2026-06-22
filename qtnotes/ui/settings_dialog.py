"""Диалог настроек: шрифт, размер, путь к хранилищу."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFontComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import config


class _EncryptMigrationWorker(QThread):
    """Фоновая перешифровка при включении шифрования: setup_pin + бэкап + migrate_encrypt.
    Тяжёлая работа уходит с UI-потока; результат/ошибка — сигналом в UI-поток."""

    done = Signal(dict, str)   # (stats, backup_path)
    failed = Signal(str)

    def __init__(self, pin: str, backend, parent=None):
        super().__init__(parent)
        self._pin = pin
        self._backend = backend

    def run(self) -> None:  # выполняется в отдельном потоке
        try:
            from ..crypto import unlock
            from ..storage import migrate
            unlock.setup_pin(self._pin, self._backend)  # шифрование + MK в сессию
            backup = migrate.backup_zip()               # бэкап ДО перешифровки (owned/erasable)
            stats = migrate.migrate_encrypt()           # зашифровать существующие данные
            # успех → плейнтекст-бэкап больше не нужен и не должен лежать на диске
            migrate.cleanup_backup(backup)
            self.done.emit(stats, "")
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class SettingsDialog(QDialog):
    """Редактирует настройки приложения. values() -> (family, size, vault_path)."""

    def __init__(self, parent=None, sync_manager=None):
        super().__init__(parent)
        self._sync_manager = sync_manager
        self.setWindowTitle("Настройки")
        self.setMinimumWidth(440)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(10)

        self._font = QFontComboBox()
        self._font.setCurrentFont(self._font.font())
        from PySide6.QtGui import QFont
        self._font.setCurrentFont(QFont(config.font_family()))
        form.addRow("Шрифт", self._font)

        self._size = QSpinBox()
        self._size.setRange(9, 28)
        self._size.setValue(config.font_size())
        self._size.setSuffix("  px")
        form.addRow("Размер шрифта", self._size)

        # путь хранилища
        path_row = QWidget()
        pr = QHBoxLayout(path_row)
        pr.setContentsMargins(0, 0, 0, 0)
        self._vault = QLineEdit(str(config.vault_dir()))
        self._vault.setReadOnly(True)
        browse = QPushButton("Выбрать…")
        browse.setObjectName("Ghost")
        browse.clicked.connect(self._browse)
        pr.addWidget(self._vault, 1)
        pr.addWidget(browse)
        form.addRow("Папка хранилища", path_row)

        hint = QLabel("Изменение папки переключает приложение на её содержимое.\n"
                      "Чтобы перенести данные — скопируйте папку хранилища целиком.")
        hint.setObjectName("EmptyState")
        hint.setWordWrap(True)

        root.addLayout(form)
        root.addWidget(hint)

        if self._sync_manager is not None:
            from PySide6.QtCore import Qt
            sync_btn = QPushButton("🔄  Синхронизация устройств")
            sync_btn.setMinimumHeight(40)
            sync_btn.setCursor(Qt.PointingHandCursor)
            sync_btn.setStyleSheet(
                "QPushButton{background:#2b5278;color:#e9edf0;border-radius:8px;"
                "padding:8px;font-weight:bold;}"
                "QPushButton:hover{background:#33608c;}")
            sync_btn.clicked.connect(self._open_sync)
            root.addWidget(sync_btn)

        from PySide6.QtCore import Qt
        enc_btn = QPushButton(self._encryption_label())
        enc_btn.setMinimumHeight(40)
        enc_btn.setCursor(Qt.PointingHandCursor)
        enc_btn.setStyleSheet(
            "QPushButton{background:#2b5278;color:#e9edf0;border-radius:8px;"
            "padding:8px;font-weight:bold;}"
            "QPushButton:hover{background:#33608c;}")
        enc_btn.clicked.connect(self._open_encryption)
        self._enc_btn = enc_btn
        root.addWidget(enc_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "Папка хранилища", self._vault.text())
        if path:
            self._vault.setText(path)

    def _open_sync(self) -> None:
        from .sync_dialog import SyncDialog
        SyncDialog(self._sync_manager, self).exec()

    def _encryption_label(self) -> str:
        from ..crypto import unlock
        return ("🔒  Локальное шифрование: включено"
                if unlock.is_configured()
                else "🔒  Локальное шифрование (ПИН)…")

    def _open_encryption(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from ..crypto import unlock
        if unlock.is_configured():
            QMessageBox.information(
                self, "Локальное шифрование",
                "Шифрование включено. ПИН запрашивается при запуске приложения.")
            return

        warn = (
            "Будет включено локальное шифрование этого хранилища.\n\n"
            "• ПИН задаётся сейчас и потребуется при каждом запуске.\n"
            "• Восстановления ПИНа НЕТ: забыли — данные на этом устройстве недоступны "
            "(восстановление возможно со второго, синхронизированного устройства).\n"
            "• Новые заметки шифруются сразу; полное шифрование уже существующих данных "
            "будет добавлено отдельной командой.\n\n"
            "Рекомендуется включать на новом/пустом хранилище. Продолжить?")
        if QMessageBox.question(self, "Включить шифрование?", warn) != QMessageBox.Yes:
            return

        try:
            backend = unlock.default_backend()
        except RuntimeError as e:
            QMessageBox.critical(self, "Локальное шифрование", str(e))
            return

        from .pin_dialog import PinSetupDialog
        dlg = PinSetupDialog(self)
        if dlg.exec() != QDialog.Accepted or not dlg.pin():
            return

        # Тяжёлая часть (setup_pin + бэкап + перешифровка всех данных) — в фоновом потоке,
        # чтобы UI не «висел» на большом хранилище. Модальный прогресс без отмены (прерывать
        # перешифровку на середине нельзя). Результат/ошибка приходят сигналом в UI-поток.
        from PySide6.QtWidgets import QProgressDialog
        from PySide6.QtCore import Qt

        worker = _EncryptMigrationWorker(dlg.pin(), backend, self)
        prog = QProgressDialog("Шифрование данных…", "", 0, 0, self)
        prog.setWindowTitle("Локальное шифрование")
        prog.setWindowModality(Qt.WindowModal)
        prog.setCancelButton(None)            # прерывать перешифровку нельзя
        prog.setMinimumDuration(0)
        prog.setValue(0)

        result: dict = {}
        worker.done.connect(lambda stats, backup: result.update(stats=stats, backup=backup))
        worker.failed.connect(lambda msg: result.update(error=msg))
        worker.finished.connect(prog.reset)
        worker.start()
        prog.exec()                            # крутится, пока поток не завершится
        worker.wait()

        if "error" in result:
            QMessageBox.critical(self, "Локальное шифрование",
                                 f"Не удалось включить шифрование: {result['error']}")
            return

        stats = result["stats"]
        self._enc_btn.setText(self._encryption_label())
        QMessageBox.information(
            self, "Готово",
            "Шифрование включено, существующие данные зашифрованы.\n\n"
            f"Зашифровано: папок {stats['folders']}, заметок {stats['notes']}, "
            f"вложений {stats['blobs']}.\n\n"
            "Временная резервная копия (плейнтекст) удалена после успешной перешифровки.\n"
            "Перезапустите приложение — ПИН будет запрашиваться при старте.")

    def values(self) -> tuple[str, int, str]:
        return self._font.currentFont().family(), self._size.value(), self._vault.text()
