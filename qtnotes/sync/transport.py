"""TLS-транспорт со взаимной аутентификацией по pinned-сертификатам.

Доверие не через CA, а через закрепление (pinning): сторона загружает cert(ы)
доверенных пиров как «CA», поэтому валидируется ровно их самоподписанный cert.
device_id пира выводится из его cert после рукопожатия и сверяется с ожидаемым.
См. docs/sync-protocol.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl

from . import peers


def make_server_context(certfile, keyfile, trusted_cadata: str | None) -> ssl.SSLContext:
    """TLS-контекст сервера. trusted_cadata — PEM доверенных cert (как CA).

    Если None — принимаем без проверки клиентского cert (режим сопряжения, A7).
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(certfile), str(keyfile))
    ctx.check_hostname = False
    if trusted_cadata:
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(cadata=trusted_cadata)
    else:
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def make_client_context(certfile, keyfile, peer_cadata: str) -> ssl.SSLContext:
    """TLS-контекст клиента, валидирующий ровно cert пира (pinning)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_cert_chain(str(certfile), str(keyfile))
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=peer_cadata)
    return ctx


def peer_device_id(ssl_object) -> str | None:
    """device_id пира из его cert (после рукопожатия). None — если cert нет."""
    if ssl_object is None:
        return None
    der = ssl_object.getpeercert(binary_form=True)
    if not der:
        return None
    return hashlib.sha256(der).hexdigest()[:16]


def trusted_cadata() -> str | None:
    """PEM всех доверенных устройств (для серверного контекста)."""
    pems = [p.cert_pem for p in peers.list_peers() if p.cert_pem]
    return "\n".join(pems) if pems else None


async def start_server(identity, trusted_cadata_str, handler,
                       host: str = "0.0.0.0", port: int = 0):
    ctx = make_server_context(identity.cert_path, identity.key_path, trusted_cadata_str)
    return await asyncio.start_server(handler, host, port, ssl=ctx)


async def open_connection(host: str, port: int, identity, peer_cert_pem: str):
    ctx = make_client_context(identity.cert_path, identity.key_path, peer_cert_pem)
    return await asyncio.open_connection(host, port, ssl=ctx)
