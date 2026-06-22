"""Главное окно: узкий сайдбар слева + переключаемая основная область справа."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCursor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QStackedWidget,
    QWidget,
)

from .. import config
from ..storage import exporter, vault
from .calendar_view import CalendarView
from .chat_view import ChatView
from .folder_dialog import FolderDialog
from .settings_dialog import SettingsDialog
from .sidebar import Sidebar
from .sync_manager import SyncManager


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("QtNotes")
        self.resize(1080, 720)
        self.setMinimumSize(720, 480)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.sidebar = Sidebar()
        layout.addWidget(self.sidebar)

        # Основная область: лента заметок / календарь.
        self.stack = QStackedWidget()
        self.chat = ChatView()
        self.calendar_page = CalendarView()
        self.stack.addWidget(self.chat)            # index 0
        self.stack.addWidget(self.calendar_page)   # index 1
        layout.addWidget(self.stack, 1)

        self.setCentralWidget(central)

        # Сигналы сайдбара.
        self.sidebar.folderSelected.connect(self._on_folder_selected)
        self.sidebar.folderEditRequested.connect(self._on_edit_folder)
        self.sidebar.folderDeleteRequested.connect(self._on_delete_folder)
        self.sidebar.calendarRequested.connect(self._show_calendar)
        self.sidebar.newFolderRequested.connect(self._on_new_folder)
        self.sidebar.settingsRequested.connect(self._on_settings)
        self.sidebar.folderExportRequested.connect(self._on_export_folder)
        self.sidebar.importRequested.connect(self._on_import)
        self.sidebar.exportAllRequested.connect(self._on_export_all)

        # Сигналы ленты.
        self.chat.moveNotesRequested.connect(self._on_move_notes)
        self.chat.noteReferenceActivated.connect(self._on_reference)

        # Загрузка папок из хранилища.
        self._folders: dict[str, object] = {}
        self._reload_folders()

        # Синхронизация (опциональна, по умолчанию выключена).
        self.sync = SyncManager(self)
        self.sync.remoteChanged.connect(self._on_remote_changed, Qt.QueuedConnection)
        self._sync_refresh = QTimer(self)
        self._sync_refresh.setSingleShot(True)
        self._sync_refresh.setInterval(300)
        self._sync_refresh.timeout.connect(self._do_sync_refresh)
        if config.sync_enabled():
            self.sync.start()

    @staticmethod
    def _placeholder(text: str) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        label = QLabel(text)
        label.setObjectName("EmptyState")
        lay.addWidget(label)
        return w

    # --- папки ---

    def _reload_folders(self, select_id: str | None = None) -> None:
        folders = vault.list_folders()
        self._folders = {f.id: f for f in folders}
        self.sidebar.set_folders(folders)
        if select_id and select_id in self._folders:
            self.sidebar.select_folder(select_id)
            self._on_folder_selected(select_id)

    def _on_folder_selected(self, folder_id: str) -> None:
        folder = self._folders.get(folder_id)
        self.chat.show_folder(folder)
        self.stack.setCurrentIndex(0)

    def _show_calendar(self) -> None:
        self.sidebar.select_folder(None)
        self.calendar_page.reload()
        self.stack.setCurrentIndex(1)

    # --- перенос заметок и навигация по ссылкам ---

    def _on_move_notes(self, note_ids: list) -> None:
        current = self.chat._current_folder
        targets = [f for f in self._folders.values()
                   if current is None or f.id != current.id]
        if not targets:
            QMessageBox.information(self, "Перенос", "Нет других папок для переноса.")
            return
        menu = QMenu(self)
        menu.addAction("Переместить в папку:").setEnabled(False)
        menu.addSeparator()
        actions = {menu.addAction(f.name): f.id for f in targets}
        chosen = menu.exec(QCursor.pos())
        target_id = actions.get(chosen)
        if not target_id:
            return
        for nid in note_ids:
            note = self._find_note_in_current(nid)
            if note is not None:
                vault.move_note(note, target_id)
        if current is not None:
            self.chat.show_folder(current)

    def _find_note_in_current(self, note_id: str):
        for b in self.chat._bubbles:
            if b.note.id == note_id:
                return b.note
        return None

    def _on_reference(self, note_id: str) -> None:
        target = vault.find_note(note_id)
        if target is None:
            QMessageBox.information(
                self, "Ссылка",
                f"Заметка #{note_id[:6]} не найдена — возможно, она была удалена.")
            return
        current = self.chat._current_folder
        if current is not None and current.id == target.folder_id:
            self.chat.scroll_to_note(note_id)
            return
        folder = self._folders.get(target.folder_id)
        if folder is not None:
            self.sidebar.select_folder(folder.id)
            self._on_folder_selected(folder.id)
            self.chat.scroll_to_note(note_id)

    def _on_new_folder(self) -> None:
        dialog = FolderDialog(self)
        if dialog.exec():
            name, caption, color, icon = dialog.values()
            folder = vault.create_folder(name=name, caption=caption, color=color, icon=icon)
            self._reload_folders(select_id=folder.id)

    def _on_edit_folder(self, folder_id: str) -> None:
        folder = self._folders.get(folder_id)
        if folder is None:
            return
        dialog = FolderDialog(self, name=folder.name, caption=folder.caption,
                              color=folder.color, icon=folder.icon)
        if dialog.exec():
            folder.name, folder.caption, folder.color, folder.icon = dialog.values()
            vault.save_folder(folder)
            self._reload_folders(select_id=folder.id)

    def _on_delete_folder(self, folder_id: str) -> None:
        folder = self._folders.get(folder_id)
        if folder is None:
            return
        reply = QMessageBox.question(
            self, "Удалить папку",
            f"Удалить папку «{folder.name}» со всеми заметками?\nЭто действие необратимо.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            vault.delete_folder(folder_id)
            if self.chat._current_folder and self.chat._current_folder.id == folder_id:
                self.chat.show_folder(None)
            self._reload_folders()

    # --- экспорт / импорт ---

    def _on_export_folder(self, folder_id: str) -> None:
        folder = self._folders.get(folder_id)
        if folder is None:
            return
        suggested = f"{folder.name}.zip"
        path, _ = QFileDialog.getSaveFileName(self, "Экспорт папки", suggested, "Архив (*.zip)")
        if not path:
            return
        try:
            exporter.export_folder(folder_id, path)
            QMessageBox.information(self, "Экспорт", f"Папка «{folder.name}» сохранена.")
        except OSError as e:
            QMessageBox.warning(self, "Экспорт", f"Не удалось сохранить: {e}")

    def _on_export_all(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Экспорт всех заметок", "qtnotes_backup.zip", "Архив (*.zip)")
        if not path:
            return
        try:
            exporter.export_all(path)
            QMessageBox.information(self, "Экспорт", "Все заметки сохранены в архив.")
        except OSError as e:
            QMessageBox.warning(self, "Экспорт", f"Не удалось сохранить: {e}")

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Импорт архива", "", "Архив (*.zip)")
        if not path:
            return
        try:
            count = exporter.import_archive(path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Импорт", f"Не удалось импортировать: {e}")
            return
        self._reload_folders()
        self.calendar_page.reload()
        QMessageBox.information(self, "Импорт", f"Импортировано файлов: {count}.")

    def _on_settings(self) -> None:
        dialog = SettingsDialog(self, sync_manager=self.sync)
        if dialog.exec():
            family, size, vault_path = dialog.values()
            config.set_setting("font_family", family)
            config.set_setting("font_size", size)
            old_vault = str(config.vault_dir())
            vault_changed = bool(vault_path) and vault_path != old_vault
            if vault_changed:
                config.set_vault_path(vault_path)
            apply_font(QApplication.instance())
            self._reload_folders()
            self.chat.show_folder(None)
            # сменилось хранилище — перезапустить синк на новый vault
            if vault_changed and self.sync.is_running():
                self.sync.stop()
                self.sync.start()

    # --- синхронизация: обновление UI по удалённым изменениям ---

    def _on_remote_changed(self) -> None:
        self._sync_refresh.start()   # дебаунс пачки входящих изменений

    def _do_sync_refresh(self) -> None:
        # F1/F2 (раунд-3): обновить список папок diff-ом (без пересоздания и без повторного
        # show_folder, который дёргал бы discard-модалку), затем обновить ленту текущей
        # папки через refresh_folder (без гарда, ввод сохраняется).
        cur = self.chat._current_folder
        cur_id = cur.id if cur else None
        folders = vault.list_folders()
        self._folders = {f.id: f for f in folders}
        self.sidebar.set_folders(folders)  # F2: diff-update, выделение сохраняется
        if cur_id:
            self.sidebar.select_folder(cur_id)
        self.calendar_page.reload()
        if cur_id is None:
            return
        if cur_id in self._folders:
            self.chat.refresh_folder(self._folders[cur_id])  # F1: лента без discard-гарда
        else:
            self.chat.show_folder(None)  # папку удалили синком

    def closeEvent(self, e):  # noqa: N802
        try:
            self.sync.stop()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(e)


def apply_font(app) -> None:
    """Применить ОБЫЧНЫЙ текстовый шрифт.

    Emoji-шрифт НЕ добавляем в семейство (иначе Qt рисует им буквы/цифры/пробелы).
    Цветные эмодзи подхватываются автоматически через fontconfig — шрифт
    Noto Color Emoji установлен на уровень пользователя (см. ensure_emoji_font).
    """
    from PySide6.QtGui import QFontDatabase, QFontInfo

    configured = config.font_family()
    if configured:
        base = configured
    else:
        available = set(QFontDatabase.families())
        base = next((f for f in ("Noto Sans", "DejaVu Sans", "Liberation Sans",
                                 "Cantarell", "Ubuntu") if f in available), None)
        base = base or QFontInfo(QFont()).family()
    font = QFont(base)
    font.setPointSize(config.font_size())
    app.setFont(font)
