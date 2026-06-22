"""Прозрачный крипто-слой файлового хранилища.

Маршрутизирует чтение/запись файлов в зависимости от состояния:
- шифрование выключено (по умолчанию) → пишем/читаем как обычный plaintext
  (байт-в-байт прежнее поведение vault.py);
- шифрование включено и хранилище разблокировано → пишем зашифрованный файл с
  magic-заголовком; читаем с автоопределением формата.

Формат зашифрованного файла: MAGIC || seal(subkey, plaintext, aad=rel_path), где
subkey = HKDF(MK, "file/" || rel_path). Привязка к относительному пути (как info и
как aad) изолирует файлы друг от друга и ловит подмену/перемещение шифртекста.

Файл без MAGIC читается как plaintext — это обеспечивает обратную совместимость и
плавную миграцию: старые незашифрованные файлы продолжают читаться.

Блобы (вложения), oplog и индекс ЗДЕСЬ НЕ обрабатываются — у них отдельные шаги
(Ф2b/Ф2c), т.к. блобы читаются UI напрямую по пути.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import config
from .. import fsutil as _fsutil
from ..crypto import primitives as P
from ..crypto import session
from ..crypto.errors import VaultLockedError  # re-export для обратной совместимости

MAGIC = b"QTNC1\n"  # QtNotes Crypto, формат v1
_INFO_PREFIX = b"file/"

__all__ = ["VaultLockedError", "MAGIC", "write_bytes", "read_bytes",
           "write_json", "read_json", "is_encrypted_file"]


def _rel_info(path: Path) -> bytes:
    """Стабильный контекст файла = путь относительно vault (для info/aad).

    Если файл вне vault (не должно случаться для контента) — используем имя файла,
    чтобы не падать."""
    try:
        rel = Path(path).resolve().relative_to(config.vault_dir().resolve())
        return str(rel).encode("utf-8")
    except (ValueError, OSError):
        return Path(path).name.encode("utf-8")


def _subkey(mk: bytes, info: bytes) -> bytes:
    return P.hkdf(mk, info=_INFO_PREFIX + info)


def _encrypting() -> bool:
    return config.encryption_enabled() and session.is_unlocked()


def write_bytes(path: Path, data: bytes) -> None:
    """Атомарная запись (tmp + os.replace). Шифрует, если шифрование включено и
    разблокировано. Если включено, но заблокировано — отказ (чтобы случайно не
    записать plaintext при включённом шифровании)."""
    path = Path(path)
    if config.encryption_enabled() and not session.is_unlocked():
        raise VaultLockedError("запись при заблокированном хранилище")
    path.parent.mkdir(parents=True, exist_ok=True)
    if _encrypting():
        info = _rel_info(path)
        out = MAGIC + P.seal(_subkey(session.get_master_key(), info), data, aad=info)
    else:
        out = data
    _fsutil.atomic_write_bytes(path, out)  # tmp+fsync+replace+fsync(dir): durable


def read_bytes(path: Path) -> bytes | None:
    """Прочитать файл, расшифровав при необходимости. Возвращает None, если файла
    нет/не читается. Бросает VaultLockedError, если файл зашифрован, а ключа нет."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw[: len(MAGIC)] == MAGIC:
        mk = session.get_master_key()
        if mk is None:
            raise VaultLockedError(f"зашифрованный файл без ключа: {path.name}")
        info = _rel_info(path)
        return P.open_sealed(_subkey(mk, info), raw[len(MAGIC):], aad=info)
    return raw  # plaintext (шифрование выкл или legacy-файл)


def write_json(path: Path, obj) -> None:
    write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))


def read_json(path: Path):
    raw = read_bytes(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def is_encrypted_file(path: Path) -> bool:
    """True, если файл начинается с MAGIC (зашифрован нашим слоем)."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False
