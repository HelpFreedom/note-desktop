"""Runtime-состояние разблокировки: мастер-ключ в памяти процесса.

Пока приложение разблокировано (введён верный ПИН), MK лежит здесь и используется
крипто-слоем хранилища для прозрачного шифрования/расшифровки. При блокировке/выходе
ключ забывается. На диск отсюда ничего не пишется.

Один процесс — один MK; модульное состояние достаточно (десктоп — однопроцессное GUI).
"""

from __future__ import annotations

_master_key: bytes | None = None


def set_master_key(mk: bytes | None) -> None:
    global _master_key
    _master_key = mk


def get_master_key() -> bytes | None:
    return _master_key


def is_unlocked() -> bool:
    return _master_key is not None


def lock() -> None:
    """Забыть мастер-ключ (блокировка/выход)."""
    global _master_key
    _master_key = None
