"""Обнаружение пиров в локальной сети по mDNS (zeroconf).

Каждое устройство анонсирует сервис `_qtnotes._tcp` с device_id и именем в TXT.
Браузер сообщает о появлении/исчезновении пиров — это и даёт «сами подключаются/
отключаются, когда в одной сети». Свой сервис из выдачи отфильтровывается.

Чистая логика разбора (`parse_service_info`) не зависит от сети и тестируется
детерминированно; живая регистрация/браузер требуют мультикаста.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

SERVICE_TYPE = "_qtnotes._tcp.local."


@dataclass
class FoundPeer:
    device_id: str
    name: str
    host: str    # IP-адрес (строкой)
    port: int


def _primary_ip() -> str:
    """Основной LAN-IP без отправки пакетов (UDP-connect задаёт маршрут)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _all_ips() -> list[str]:
    """I5: все пригодные IPv4-адреса хоста (multi-homed/VPN/контейнеры) — анонсируем все,
    чтобы пир на той же LAN нашёл достижимый, а не только угаданный «основной»."""
    ips: list[str] = []
    primary = _primary_ip()
    if primary and not primary.startswith("127."):
        ips.append(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and not ip.startswith("169.254.") \
                    and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


def _prop(props: dict, key: bytes) -> str:
    v = props.get(key) if props else None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v or ""


def parse_service_info(info, our_device_id: str) -> FoundPeer | None:
    """ServiceInfo → FoundPeer. None, если это мы сами или данных не хватает."""
    if info is None:
        return None
    did = _prop(info.properties, b"id")
    if not did or did == our_device_id:
        return None
    addrs = info.parsed_addresses() if hasattr(info, "parsed_addresses") else []
    # I5: предпочесть НЕ-loopback/НЕ-link-local адрес (на multi-homed хосте addrs[0]
    # мог быть недостижим из LAN); если таких нет — берём первый.
    host = ""
    for a in addrs:
        if not a.startswith("127.") and not a.startswith("169.254."):
            host = a
            break
    if not host and addrs:
        host = addrs[0]
    port = info.port or 0
    if not host or not port:
        return None
    return FoundPeer(device_id=did, name=_prop(info.properties, b"name"),
                     host=host, port=port)


def build_service_info(identity, port: int, ip: str | None = None) -> ServiceInfo:
    # I5: если IP не задан явно — анонсируем ВСЕ пригодные адреса (multi-homed/VPN).
    ips = [ip] if ip else _all_ips()
    name = f"{identity.device_id}.{SERVICE_TYPE}"
    server = f"qtnotes-{identity.device_id}.local."
    props = {b"id": identity.device_id.encode(), b"name": identity.name.encode()}
    return ServiceInfo(SERVICE_TYPE, name,
                       addresses=[socket.inet_aton(a) for a in ips],
                       port=port, properties=props, server=server)


def device_id_from_service_name(name: str) -> str:
    """Из имени сервиса `<device_id>._qtnotes._tcp.local.` вернуть device_id."""
    return name.split(".", 1)[0]


class _Listener(ServiceListener):
    def __init__(self, disc: "Discovery"):
        self._disc = disc

    def add_service(self, zc, type_, name):  # noqa: N802
        info = zc.get_service_info(type_, name)
        peer = parse_service_info(info, self._disc.our_id)
        if peer and self._disc.on_found:
            self._disc.on_found(peer)

    def update_service(self, zc, type_, name):  # noqa: N802
        self.add_service(zc, type_, name)

    def remove_service(self, zc, type_, name):  # noqa: N802
        did = device_id_from_service_name(name)
        if did != self._disc.our_id and self._disc.on_lost:
            self._disc.on_lost(did)


class Discovery:
    """Анонс своего сервиса + браузер пиров. Колбэки идут из потока zeroconf."""

    def __init__(self, identity, port: int, on_found=None, on_lost=None):
        self.identity = identity
        self.port = port
        self.our_id = identity.device_id
        self.on_found = on_found
        self.on_lost = on_lost
        self._zc: Zeroconf | None = None
        self._info: ServiceInfo | None = None
        self._browser: ServiceBrowser | None = None

    def start(self) -> None:
        self._zc = Zeroconf()
        self._info = build_service_info(self.identity, self.port)
        self._zc.register_service(self._info)
        self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, _Listener(self))

    def stop(self) -> None:
        zc, info = self._zc, self._info
        self._zc = self._info = self._browser = None
        if zc is None:
            return
        try:
            if info is not None:
                zc.unregister_service(info)
        finally:
            zc.close()
