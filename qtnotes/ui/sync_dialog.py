"""Диалог синхронизации: вкл/выкл, сопряжение по QR, список устройств, статус."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .. import config
from .qr_widget import QrView


class SyncDialog(QDialog):
    """UI поверх SyncManager. Движок живёт в MainWindow, диалог им управляет."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self._mgr = manager
        self.setWindowTitle("Синхронизация")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)

        self._enable_btn = QPushButton()
        self._enable_btn.setCursor(Qt.PointingHandCursor)
        self._enable_btn.setMinimumHeight(42)
        self._enable_btn.clicked.connect(self._toggle_sync)
        root.addWidget(self._enable_btn)
        self._update_toggle_btn()

        ident = manager.identity()
        info = QLabel(f"Это устройство: <b>{ident.name}</b><br>ID: {ident.device_id}")
        info.setTextFormat(Qt.RichText)
        root.addWidget(info)

        self._pair_btn = QPushButton("Сопрячь устройство (показать QR)")
        self._pair_btn.clicked.connect(self._on_pair)
        root.addWidget(self._pair_btn)

        self._qr = QrView()
        self._qr.hide()
        root.addWidget(self._qr, alignment=Qt.AlignCenter)
        self._qr_hint = QLabel()
        self._qr_hint.setWordWrap(True)
        self._qr_hint.setObjectName("EmptyState")
        self._qr_hint.hide()
        root.addWidget(self._qr_hint)

        root.addWidget(QLabel("Сопряжённые устройства:"))
        self._list = QListWidget()
        root.addWidget(self._list, 1)
        rm = QPushButton("Удалить выбранное")
        rm.setObjectName("Ghost")
        rm.clicked.connect(self._remove_selected)
        root.addWidget(rm)

        self._status = QLabel()
        self._status.setObjectName("EmptyState")
        root.addWidget(self._status)

        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        root.addWidget(close)

        manager.devicePaired.connect(self._on_paired)
        manager.statusChanged.connect(self._refresh_status)

        self._refresh_devices()
        self._refresh_status()
        self._update_controls()

    # --- состояние ---

    def _update_controls(self) -> None:
        self._pair_btn.setEnabled(config.sync_enabled())

    def _update_toggle_btn(self) -> None:
        on = config.sync_enabled()
        if on:
            self._enable_btn.setText("✓  Синхронизация включена")
            self._enable_btn.setToolTip("Нажмите, чтобы выключить")
            self._enable_btn.setStyleSheet(
                "QPushButton{background:#3a7d44;color:white;border-radius:8px;"
                "padding:10px;font-weight:bold;}"
                "QPushButton:hover{background:#46924f;}")
        else:
            self._enable_btn.setText("Включить синхронизацию")
            self._enable_btn.setToolTip("")
            self._enable_btn.setStyleSheet(
                "QPushButton{background:#5288c1;color:white;border-radius:8px;"
                "padding:10px;font-weight:bold;}"
                "QPushButton:hover{background:#5e96d2;}")

    def _toggle_sync(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        new = not config.sync_enabled()
        if new:
            # запуск (seed + сервер + mDNS) занимает секунду — показываем занятость
            self._enable_btn.setEnabled(False)
            self._enable_btn.setText("Запускаю синхронизацию…")
            QApplication.setOverrideCursor(Qt.WaitCursor)
            QApplication.processEvents()
            try:
                self._mgr.set_enabled(True)
            finally:
                QApplication.restoreOverrideCursor()
                self._enable_btn.setEnabled(True)
        else:
            self._mgr.set_enabled(False)
            self._hide_qr()
        self._update_toggle_btn()
        self._update_controls()
        self._refresh_status()

    def _refresh_status(self) -> None:
        if config.sync_enabled():
            n = len(self._mgr.online_peers())
            self._status.setText(f"Синхронизация включена. Онлайн устройств: {n}.")
        else:
            self._status.setText("Синхронизация выключена.")

    # --- сопряжение ---

    def _on_pair(self) -> None:
        payload = self._mgr.start_pairing()
        if not payload:
            QMessageBox.information(self, "Сопряжение",
                                    "Сначала включите синхронизацию.")
            return
        from ..sync import pairing
        self._qr.set_matrix(pairing.qr_matrix(payload))
        self._qr.show()
        self._qr_hint.setText(
            "Отсканируйте QR камерой телефона в приложении QtNotes. "
            "Держите это окно открытым до завершения сопряжения.")
        self._qr_hint.show()

    def _hide_qr(self) -> None:
        self._mgr.stop_pairing()
        self._qr.hide()
        self._qr_hint.hide()

    def _on_paired(self, name: str) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication
        self._hide_qr()
        # перезапуск движка: сервер должен начать доверять новому устройству
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._mgr.restart()
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_devices()
        self._refresh_status()
        QMessageBox.information(self, "Сопряжение", f"Устройство «{name}» сопряжено.")

    # --- список устройств ---

    def _refresh_devices(self) -> None:
        from ..sync import peers
        self._list.clear()
        for p in peers.list_peers():
            item = QListWidgetItem(f"{p.name or '—'}   ·   {p.device_id}")
            item.setData(Qt.UserRole, p.device_id)
            self._list.addItem(item)

    def _remove_selected(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        from ..sync import peers
        peers.remove_peer(item.data(Qt.UserRole))
        self._refresh_devices()

    def closeEvent(self, e):  # noqa: N802
        self._hide_qr()
        super().closeEvent(e)
