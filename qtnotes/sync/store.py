"""Доступ движка синхронизации к хранилищу за тонким интерфейсом.

Движок (engine.py) работает не с глобальными модулями напрямую, а через объект
Store. В продакшене это GlobalStore (одно хранилище процесса). В тестах можно
подставить изолированные хранилища (для двух «устройств» в одном процессе).

Методы синхронные и не содержат await — это важно: движок вызывает их между
сетевыми awaits, и изоляция глобального состояния в тестах остаётся корректной.
"""

from __future__ import annotations

import hashlib

from ..storage import vault
from . import apply, oplog


def blob_hashes_of_op(op: dict) -> list[str]:
    """sha256 блобов, на которые ссылается op (вложения заметок и обои темы)."""
    kind = op.get("kind")
    payload = op.get("payload")
    if not payload:
        return []
    if kind == "note.put":
        return [a["sha256"] for a in payload.get("attachments", []) if a.get("sha256")]
    if kind == "setting.put" and isinstance(payload, dict):
        w = payload.get("wallpaper")
        return [w] if w else []
    return []


class GlobalStore:
    """Хранилище процесса (продакшен)."""

    def version_vector(self) -> dict:
        return oplog.version_vector()

    def ops_since(self, remote_vv: dict) -> list[dict]:
        return oplog.ops_since(remote_vv)

    def record_and_apply(self, op: dict) -> bool:
        """Применить чужую op и сохранить. True — если новая.

        H5: ПРИМЕНЯЕМ до записи. Если apply бросит — op НЕ записывается, version
        vector не двигается → op придёт снова при следующем синке (а не потеряется
        молча, помеченным «видели»). apply_* идемпотентны, повторное применение
        безопасно."""
        if oplog.has_op(op["op_id"]):
            return False
        apply.apply_op(op)        # бросок пробрасываем — НЕ записываем (ретрай позже)
        oplog.record_remote(op)   # запись + продвижение vv только после успеха
        return True

    def missing_blob_hashes(self, op: dict) -> list[str]:
        return [h for h in blob_hashes_of_op(op) if not vault.has_blob(h)]

    def read_blob(self, sha256: str) -> bytes | None:
        # пиру отдаём ПЛЕЙНТЕКСТ (он проверит sha и сохранит у себя); расшифровка
        # at-rest прозрачна.
        return vault.read_blob_bytes(sha256)

    def write_blob(self, sha256: str, data: bytes) -> bool:
        """Записать blob, проверив контрольную сумму. False — если sha не сошёлся."""
        if hashlib.sha256(data).hexdigest() != sha256:
            return False
        vault.write_blob(sha256, data)
        return True
