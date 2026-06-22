"""Сопряжение устройств по QR (одноразовое установление доверия).

Инициирует ТЕЛЕФОН: десктоп показывает QR со своими данными и слушает; телефон
сканирует, подключается, сверяет fingerprint сертификата (TOFU, привязка через QR),
обменивается device_id/cert. После этого оба заносят друг друга в trust-store и
дальнейший синк идёт по обычному взаимному TLS с pinning (A5).

Здесь реализованы ОБЕ стороны на Python — для тестов и для десктопа (сторона
«показал QR + слушаю»). Телефон позже повторит клиентскую сторону на Dart по этому
же формату.

Поток до доверия не может пинить cert заранее, поэтому:
  * сервер (десктоп) принимает TLS без клиентского cert (CERT_NONE), но требует в
    первом сообщении одноразовый token из QR;
  * клиент (телефон) не валидирует цепочку, но СВЕРЯЕТ fingerprint cert сервера с
    тем, что в QR (это и привязывает к нужному устройству);
  * cert телефона передаётся явно в сообщении pair_hello (сервер CERT_NONE его не
    запрашивает на уровне TLS).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import ssl
from datetime import datetime, timezone

import segno

from . import identity as identity_mod
from . import peers, transport, wire

PROTO = 1

# Окно сопряжения: после истечения слушатель не принимает новые пары. Закрывает
# сценарий «утёкший/подсмотренный QR используется позже». Одноразовость (consumed)
# дополнительно гарантирует, что после успешной пары токен больше не работает.
DEFAULT_PAIRING_TTL = 180  # секунд


class PairingError(Exception):
    pass


def generate_token() -> str:
    return secrets.token_urlsafe(12)


def make_pairing_payload(identity, host: str, port: int, token: str,
                         sync_port: int = 0) -> str:
    """Строка для QR: данные, по которым телефон находит и доверяет десктопу.

    sync_port — порт движка синхронизации (для прямого подключения телефона в обход
    mDNS, который часто фильтруется на роутерах)."""
    return json.dumps({
        "v": PROTO,
        "did": identity.device_id,
        "fp": identity.fingerprint,
        "name": identity.name,
        "host": host,
        "port": port,
        "sync_port": sync_port,
        "token": token,
    }, ensure_ascii=False, separators=(",", ":"))


def parse_pairing_payload(text: str) -> dict:
    d = json.loads(text)
    for key in ("did", "fp", "host", "port", "token"):
        if key not in d:
            raise PairingError(f"в QR нет поля {key}")
    return d


def qr_matrix(text: str) -> list[list[bool]]:
    """Матрица модулей QR (True=чёрный) — для отрисовки в Qt (Фаза B)."""
    q = segno.make(text, error="m")
    return [[bool(c) for c in row] for row in q.matrix]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- сторона десктопа: слушаем сопряжение ---

async def serve_pairing(identity, token: str, on_paired=None,
                        host: str = "0.0.0.0", port: int = 0,
                        ttl: float = DEFAULT_PAIRING_TTL):
    """Запустить ОДНОРАЗОВЫЙ слушатель сопряжения с TTL. on_paired(peer) при успехе.

    Токен действует один раз (после успешной пары — `consumed`, слушатель закрывается)
    и не дольше `ttl` секунд. Это закрывает повторное/отложенное использование
    подсмотренного QR и параллельное сопряжение нескольких устройств одним токеном."""
    loop = asyncio.get_running_loop()
    state = {"consumed": False, "deadline": loop.time() + ttl}
    holder: dict = {}

    async def handler(reader, writer):
        try:
            # окно закрыто (использован или истёк) → не принимаем
            if state["consumed"] or loop.time() > state["deadline"]:
                await wire.write_message(writer, {"type": "pair_err", "reason": "expired"})
                return
            kind, msg = await wire.read_frame(reader)
            if kind != "control" or msg.get("type") != "pair_hello":
                return
            if msg.get("token") != token:
                await wire.write_message(writer, {"type": "pair_err", "reason": "token"})
                return
            cert_pem = msg.get("cert_pem", "")
            claimed = msg.get("device_id", "")
            real = identity_mod.device_id_from_cert_pem(cert_pem.encode())
            if not cert_pem or real != claimed:
                await wire.write_message(writer, {"type": "pair_err", "reason": "cert"})
                return
            # успех: одноразово — гасим токен ДО on_paired, чтобы гонка двух подключений
            # не дала второй паре пройти
            state["consumed"] = True
            peer = peers.Peer(device_id=real, name=msg.get("name", ""),
                              cert_pem=cert_pem, paired_at=_now())
            if on_paired:
                on_paired(peer)
            await wire.write_message(writer, {
                "type": "pair_ok", "device_id": identity.device_id,
                "name": identity.name})
        except (wire.ProtocolError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            writer.close()
            # после успешной пары — закрыть слушатель (одноразовость на уровне сокета)
            if state["consumed"]:
                srv = holder.get("server")
                if srv is not None:
                    srv.close()
                # I7: отменить отложенный авто-закрыватель — он уже не нужен (не держим
                # висящий таймер на цикле после раннего успеха)
                h = holder.get("timer")
                if h is not None:
                    h.cancel()

    ctx = transport.make_server_context(identity.cert_path, identity.key_path, None)
    server = await asyncio.start_server(handler, host, port, ssl=ctx)
    holder["server"] = server
    holder["timer"] = loop.call_later(ttl, server.close)  # авто-закрытие, если никто не спарился
    return server


# --- сторона телефона (для тестов; на устройстве — Dart) ---

async def pair_with(payload: dict, identity, on_paired=None) -> peers.Peer:
    """Подключиться к десктопу из QR, сверить fingerprint, обменяться доверием."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE   # цепочку не валидируем — сверим fingerprint сами
    ctx.load_cert_chain(str(identity.cert_path), str(identity.key_path))

    reader, writer = await asyncio.open_connection(payload["host"], payload["port"], ssl=ctx)
    try:
        ssl_obj = writer.get_extra_info("ssl_object")
        der = ssl_obj.getpeercert(binary_form=True)
        if not der:
            raise PairingError("сервер не предъявил сертификат")
        actual_fp = hashlib.sha256(der).hexdigest()
        if actual_fp != payload["fp"]:
            raise PairingError("fingerprint сервера не совпал с QR — возможна подмена")
        server_pem = ssl.DER_cert_to_PEM_cert(der)

        await wire.write_message(writer, {
            "type": "pair_hello", "token": payload["token"],
            "device_id": identity.device_id, "name": identity.name,
            "cert_pem": identity.cert_pem.decode()})
        kind, resp = await wire.read_frame(reader)
        if kind != "control" or resp.get("type") != "pair_ok":
            raise PairingError(f"сопряжение отклонено: {resp!r}")

        peer = peers.Peer(device_id=payload["did"], name=resp.get("name", ""),
                          cert_pem=server_pem, paired_at=_now())
        if on_paired:
            on_paired(peer)
        return peer
    finally:
        writer.close()
