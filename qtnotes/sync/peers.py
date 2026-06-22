"""Доверенные (сопряжённые) устройства — trust-store в peers.json.

Каждый пир: device_id, человекочитаемое имя, его cert (PEM) для TLS-pinning и
время сопряжения. Файл лежит рядом с настройками (config.peers_path()), НЕ в vault.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .. import config


@dataclass
class Peer:
    device_id: str
    name: str
    cert_pem: str        # PEM-строка сертификата пира (для сверки при TLS)
    paired_at: str

    def as_dict(self) -> dict:
        return {"device_id": self.device_id, "name": self.name,
                "cert_pem": self.cert_pem, "paired_at": self.paired_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Peer":
        return cls(device_id=d["device_id"], name=d.get("name", ""),
                   cert_pem=d.get("cert_pem", ""), paired_at=d.get("paired_at", ""))


def _load_raw() -> list[dict]:
    p = config.peers_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_raw(items: list[dict]) -> None:
    p = config.peers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def list_peers() -> list[Peer]:
    return [Peer.from_dict(d) for d in _load_raw()]


def get_peer(device_id: str) -> Peer | None:
    for p in list_peers():
        if p.device_id == device_id:
            return p
    return None


def add_peer(device_id: str, name: str, cert_pem: str) -> Peer:
    """Добавить/обновить доверенное устройство (по device_id)."""
    peers = [p for p in list_peers() if p.device_id != device_id]
    peer = Peer(device_id=device_id, name=name, cert_pem=cert_pem,
                paired_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    peers.append(peer)
    _save_raw([p.as_dict() for p in peers])
    return peer


def remove_peer(device_id: str) -> None:
    peers = [p for p in list_peers() if p.device_id != device_id]
    _save_raw([p.as_dict() for p in peers])


def is_trusted(device_id: str) -> bool:
    return get_peer(device_id) is not None
