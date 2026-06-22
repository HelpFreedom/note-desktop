"""Жизненный цикл движка синхронизации со стороны UI.

Держит SyncEngine (он крутится в своём потоке), маршалит уведомления об удалённых
изменениях в поток UI через Qt-сигналы и обслуживает сопряжение по QR. Локальные
изменения авто-пушатся через хук oplog.set_change_listener (развязка с vault).
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, Signal

from .. import config
from ..sync import identity as identity_mod
from ..sync import peers as peers_mod


class SyncManager(QObject):
    remoteChanged = Signal()      # применены удалённые изменения (доставка в UI-поток)
    devicePaired = Signal(str)    # имя/ид сопряжённого устройства
    statusChanged = Signal()      # включение/выключение/состояние сессий

    def __init__(self, parent=None):
        super().__init__(parent)
        self._engine = None
        self._identity = None
        self._pairing_server = None

    def identity(self):
        if self._identity is None:
            self._identity = identity_mod.ensure_identity()
        return self._identity

    def is_running(self) -> bool:
        return self._engine is not None

    # --- старт/стоп ---

    def start(self) -> None:
        if self._engine is not None:
            return
        from ..sync import engine as engine_mod
        from ..sync import oplog
        from ..sync import seed
        from ..sync import store as store_mod

        from ..sync import theme_publish

        ident = self.identity()

        def _on_started():
            seed.ensure_seeded()          # миграция вложений в blobs
            theme_publish.publish_theme()  # опубликовать тему+обои для телефона
            oplog.compact()               # B1: подрезать историю до старта синка с пирами

        # тяжёлый старт отдаём движку — он выполнит его в своём потоке после старта
        # сервера, чтобы не морозить UI. Порт — сохранённый (стабильный между
        # перезапусками), чтобы адрес в QR не устаревал.
        from .. import config
        self._engine = engine_mod.SyncEngine(
            ident, store_mod.GlobalStore(),
            on_changed=self._on_remote_changed, on_started=_on_started,
            serve_port=config.sync_port())
        oplog.set_change_listener(self._on_local_change)
        self._engine.start()
        # запомнить фактический порт (может отличаться, если сохранённый был занят)
        if self._engine._port:
            config.set_sync_port(self._engine._port)
        self.statusChanged.emit()

    def stop(self) -> None:
        from ..sync import oplog
        oplog.set_change_listener(None)
        if self._engine is not None:
            self._engine.stop()
            self._engine = None
        self.statusChanged.emit()

    def restart(self) -> None:
        """Перезапустить движок (например, после сопряжения — чтобы сервер начал
        доверять новому устройству; trusted-cert фиксируется при bind)."""
        if self._engine is not None:
            self.stop()
            self.start()

    def set_enabled(self, on: bool) -> None:
        config.set_sync_enabled(on)
        if on:
            self.start()
        else:
            self.stop()

    # --- колбэки движка (приходят из его потока) ---

    def _on_remote_changed(self) -> None:
        self.remoteChanged.emit()   # очередь в UI-поток (обновить ленту/календарь)
        self.statusChanged.emit()   # и статус «онлайн устройств»

    def _on_local_change(self) -> None:
        if self._engine is not None:
            self._engine.notify_change()

    def online_peers(self) -> list[str]:
        if self._engine is None:
            return []
        return list(self._engine.sessions.keys())

    # --- сопряжение по QR (десктоп показывает, телефон сканирует) ---

    def start_pairing(self) -> str | None:
        """Поднять ОДНОРАЗОВЫЙ слушатель сопряжения, вернуть payload для QR. Нужен
        запущенный движок. Предыдущий слушатель (если был) закрывается."""
        if self._engine is None or self._engine._loop is None:
            return None
        from ..sync import discovery, pairing

        self.stop_pairing()  # не плодить параллельные слушатели/токены
        ident = self.identity()
        token = pairing.generate_token()

        def on_paired(peer):
            peers_mod.add_peer(peer.device_id, peer.name, peer.cert_pem)
            # показываем имя + device_id (= префикс отпечатка cert) для сверки
            label = f"{peer.name} · {peer.device_id}" if peer.name else peer.device_id
            self.devicePaired.emit(label)

        loop = self._engine._loop
        fut = asyncio.run_coroutine_threadsafe(
            pairing.serve_pairing(ident, token, on_paired=on_paired,
                                  host="0.0.0.0", port=0), loop)
        server = fut.result(timeout=5)
        self._pairing_server = server
        port = server.sockets[0].getsockname()[1]
        sync_port = self._engine._port or 0
        return pairing.make_pairing_payload(
            ident, discovery._primary_ip(), port, token, sync_port=sync_port)

    def stop_pairing(self) -> None:
        server = self._pairing_server
        self._pairing_server = None
        if server is not None and self._engine is not None and self._engine._loop is not None:
            self._engine._loop.call_soon_threadsafe(server.close)
