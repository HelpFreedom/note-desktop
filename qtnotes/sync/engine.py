"""Оркестратор синхронизации: связывает обнаружение, транспорт, журнал и применение.

Сессия (по docs/sync-protocol.md): обе стороны шлют hello+have, отвечают на have
порцией ops, на ops — докачкой недостающих blobs. Соединение держится открытым для
push-on-change. На пир — ровно одно соединение (инициирует устройство с меньшим
device_id). Движок работает с хранилищем через Store (изоляция для тестов).

Запуск в проде — в отдельном потоке с собственным asyncio-циклом; UI взаимодействует
через потокобезопасные вызовы и колбэк on_changed.
"""

from __future__ import annotations

import asyncio
import sys
import threading

from . import discovery as discovery_mod
from . import peers as peers_mod
from . import transport, wire


def _warn(msg: str) -> None:
    print(f"[sync] {msg}", file=sys.stderr)


_MAX_WANT_BLOBS = 4096   # H6: потолок числа блобов на один запрос want_blobs
PROTO_VERSION = 1        # версия протокола синка (hello.proto)


class Session:
    """Одно TLS-соединение с пиром. Гоняет протокол до закрытия."""

    def __init__(self, engine: "SyncEngine", reader, writer, peer_id: str):
        self.engine = engine
        self.reader = reader
        self.writer = writer
        self.peer_id = peer_id
        self.store = engine.store
        self.peer_vv: dict = {}     # что, по нашим данным, уже есть у пира
        self._closed = False
        # сериализация записи: start() и push() могут писать в один сокет
        # одновременно — без лока кадры перемешаются и соединение порвётся
        self._write_lock = asyncio.Lock()

    async def _send(self, obj: dict) -> None:
        async with self._write_lock:
            await wire.write_message(self.writer, obj)

    async def start(self) -> None:
        await self._send({"type": "hello", "device_id": self.engine.identity.device_id,
                          "name": self.engine.identity.name, "proto": 1})
        await self._send({"type": "have", "vv": self.store.version_vector()})
        try:
            while not self._closed:
                kind, val = await wire.read_frame(self.reader)
                if kind == "blob":
                    sha, data = val
                    if self.store.write_blob(sha, data):
                        self.engine._changed()
                else:
                    await self._dispatch(val)
        except (wire.ProtocolError, asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            self.close()

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            if msg.get("device_id") and msg["device_id"] != self.peer_id:
                self.close()   # заявленный id не совпал с TLS-сертификатом
                return
            peer_proto = msg.get("proto")
            if peer_proto != PROTO_VERSION:
                # A2: версии протокола различаются. НЕ закрываем — raise на неизвестный
                # kind (apply.py) гарантирует, что непонятая op не теряется молча, а
                # переигрывается после апгрейда. Знакомые ops синкаются как обычно.
                _warn(f"пир {self.peer_id} на proto={peer_proto!r}, у нас {PROTO_VERSION}"
                      f" — частичная совместимость (незнакомые ops отложатся до апгрейда)")
        elif t == "have":
            self.peer_vv = msg.get("vv", {})
            ops = self.store.ops_since(self.peer_vv)
            if ops:
                await self._send({"type": "ops", "ops": ops})
            self.peer_vv = self.store.version_vector()
        elif t == "ops":
            changed = False
            missing: list[str] = []
            for op in msg.get("ops", []):
                # H5: одна битая op не должна рвать всю сессию и блокировать остальные.
                # Ошибка → лог + пропуск; op не записана (vv не сдвинут) → повтор позже.
                try:
                    if self.store.record_and_apply(op):
                        changed = True
                    missing += self.store.missing_blob_hashes(op)
                except Exception as e:  # noqa: BLE001
                    _warn(f"пропуск битой op {op.get('op_id')!r}: {e}")
            if missing:
                await self._send({"type": "want_blobs",
                                  "hashes": list(dict.fromkeys(missing))})
            if changed:
                self.engine._changed()
                # A1: подтвердить отправителю реально применённое (наш свежий vv).
                # Если что-то было пропущено (apply бросил — пир залочился и т.п.), наш
                # vv этого не покрывает → отправитель пере-предложит. При changed=False
                # (всё пропущено/дубликаты/ядовито) have НЕ шлём → нет бесконечного цикла.
                await self._send({"type": "have", "vv": self.store.version_vector()})
        elif t == "want_blobs":
            # H6: ограничиваем число блобов на один запрос (защита от усилителя
            # «один кадр → много чтений с диска + отправок»). Лишнее пир до-запросит
            # в следующих циклах (недостающие блобы детектируются повторно).
            for h in list(msg.get("hashes", []))[:_MAX_WANT_BLOBS]:
                data = self.store.read_blob(h)
                if data is not None:
                    async with self._write_lock:
                        await wire.write_blob(self.writer, h, data)
        elif t == "bye":
            self.close()

    async def push(self) -> None:
        """Отправить пиру операции, появившиеся после последней отдачи."""
        ops = self.store.ops_since(self.peer_vv)
        if not ops:
            return
        try:
            await self._send({"type": "ops", "ops": ops})
            self.peer_vv = self.store.version_vector()
        except OSError:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
        except OSError:
            pass
        self.engine._remove_session(self)


class SyncEngine:
    """Связывает всё вместе. store — доступ к хранилищу; get_peers — доверенные."""

    def __init__(self, identity, store, get_peers=None, on_changed=None, on_started=None,
                 serve_port=0):
        self.identity = identity
        self.store = store
        self.get_peers = get_peers or peers_mod.list_peers
        self.on_changed = on_changed
        # on_started() выполняется в ПОТОКЕ движка после старта сервера — сюда уходит
        # тяжёлый сидинг (миграция вложений в blobs), чтобы не морозить UI
        self.on_started = on_started
        # стабильный порт между перезапусками (сохранённый); 0 — любой свободный
        self._serve_port = serve_port
        self.sessions: dict[str, Session] = {}
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._discovery = None
        self._port = 0
        # E2: слоты «сейчас дозваниваемся» (резервируются СИНХРОННо в _on_peer_found,
        # до планирования корутины) + время последней попытки на пир — иначе серия
        # mDNS-анонсов, пока connect ещё в полёте и сессии нет в self.sessions, плодит
        # параллельные дозвоны, рвущие друг друга (connect→teardown→reconnect шторм).
        self._connecting: set[str] = set()
        self._last_attempt: dict[str, float] = {}
        self._reconnect_cooldown = 3.0  # сек между попытками к одному пиру

    # --- доверие ---

    def _trusted_cadata(self) -> str | None:
        pems = [p.cert_pem for p in self.get_peers() if p.cert_pem]
        return "\n".join(pems) if pems else None

    def _peer_cert(self, peer_id: str) -> str | None:
        for p in self.get_peers():
            if p.device_id == peer_id:
                return p.cert_pem
        return None

    # --- колбэки/сессии ---

    def _changed(self) -> None:
        if self.on_changed:
            try:
                self.on_changed()
            except Exception:  # noqa: BLE001 — колбэк UI не должен ронять синк
                pass

    def _remove_session(self, sess: Session) -> None:
        if self.sessions.get(sess.peer_id) is sess:
            del self.sessions[sess.peer_id]
            self._changed()  # обновить статус «онлайн» в UI

    async def _run_session(self, reader, writer, peer_id: str) -> None:
        old = self.sessions.get(peer_id)
        if old is not None:
            old.close()
        sess = Session(self, reader, writer, peer_id)
        self.sessions[peer_id] = sess
        self._changed()  # обновить статус «онлайн» в UI
        await sess.start()

    # --- сервер/клиент ---

    async def serve(self, host: str = "0.0.0.0", port: int = 0):
        ctx = transport.make_server_context(self.identity.cert_path,
                                            self.identity.key_path, self._trusted_cadata())
        try:
            self._server = await asyncio.start_server(self._on_incoming, host, port, ssl=ctx)
        except OSError:
            # сохранённый порт занят — берём любой свободный
            self._server = await asyncio.start_server(self._on_incoming, host, 0, ssl=ctx)
        self._port = self._server.sockets[0].getsockname()[1]
        return self._server

    async def _on_incoming(self, reader, writer) -> None:
        ssl_obj = writer.get_extra_info("ssl_object")
        peer_id = transport.peer_device_id(ssl_obj)
        if not peer_id or self._peer_cert(peer_id) is None:
            writer.close()
            return
        await self._run_session(reader, writer, peer_id)

    async def connect(self, host: str, port: int, peer_id: str) -> None:
        cert = self._peer_cert(peer_id)
        if cert is None:
            raise ValueError("пир не в списке доверенных")
        reader, writer = await transport.open_connection(host, port, self.identity, cert)
        await self._run_session(reader, writer, peer_id)

    async def push_all(self) -> None:
        for sess in list(self.sessions.values()):
            await sess.push()

    # --- интеграция обнаружения (для прода) ---

    def _on_peer_found(self, found: "discovery_mod.FoundPeer") -> None:
        import time
        if self._peer_cert(found.device_id) is None:
            return  # не доверенный — игнор
        if found.device_id in self.sessions or found.device_id in self._connecting:
            return  # уже на связи или дозваниваемся — не плодим параллельные connect
        # одно соединение на пару: инициирует устройство с меньшим device_id
        if self.identity.device_id < found.device_id and self._loop is not None:
            now = time.monotonic()
            if now - self._last_attempt.get(found.device_id, 0.0) < self._reconnect_cooldown:
                return  # недавно уже пытались — кулдаун (защита от флаппинга)
            self._last_attempt[found.device_id] = now
            self._connecting.add(found.device_id)  # резерв слота СИНХРОННо
            asyncio.run_coroutine_threadsafe(
                self._safe_connect(found.host, found.port, found.device_id), self._loop)

    def _on_peer_lost(self, device_id: str) -> None:
        sess = self.sessions.get(device_id)
        if sess is not None:
            sess.close()

    async def _safe_connect(self, host, port, peer_id) -> None:
        try:
            await self.connect(host, port, peer_id)
        except (OSError, ValueError):
            pass
        finally:
            self._connecting.discard(peer_id)  # слот свободен (сессия завершилась/не открылась)

    # --- фоновый поток (прод) ---

    def start(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, args=(ready,), daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

    def _thread_main(self, ready: threading.Event) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # сначала только сервер — это быстро; UI разблокируется сразу. Порт —
        # сохранённый (стабильный между перезапусками), при занятости — любой.
        self._loop.run_until_complete(self.serve(port=self._serve_port))
        ready.set()
        # тяжёлый сидинг (миграция вложений в blobs) — в этом потоке, UI уже свободен
        if self.on_started is not None:
            try:
                self.on_started()
            except Exception:  # noqa: BLE001
                pass
        # mDNS-обнаружение запускаем уже ПОСЛЕ разблокировки UI (оно медленнее)
        try:
            self._discovery = discovery_mod.Discovery(
                self.identity, self._port,
                on_found=self._on_peer_found, on_lost=self._on_peer_lost)
            self._discovery.start()
        except Exception:  # noqa: BLE001 — без mDNS синк всё равно по прямому адресу
            self._discovery = None
        self._loop.run_forever()

    def notify_change(self) -> None:
        """Потокобезопасно: разослать новые ops всем активным сессиям (push-on-change)."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.push_all(), self._loop)

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return

        async def shutdown():
            for sess in list(self.sessions.values()):
                sess.close()
            if self._discovery is not None:
                self._discovery.stop()
            if self._server is not None:
                self._server.close()

        fut = asyncio.run_coroutine_threadsafe(shutdown(), loop)
        try:
            fut.result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._loop = None
