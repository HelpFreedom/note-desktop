"""Шифрование отдельных строковых значений (а не целых файлов).

Нужно там, где зашифровать надо КОНКРЕТНОЕ поле, а не файл целиком — например,
колонку `payload` в SQLite-журнале (oplog), где остальные колонки (id, часы, тип)
должны оставаться доступными для SQL-запросов.

Формат: зашифрованное значение — строка `ENC1:` || base64(seal(subkey, plaintext)).
Значение без префикса считается обычным plaintext (обратная совместимость и
плавная миграция, как у файлового слоя crypto_fs).

Гейтинг тот же, что у crypto_fs: шифруем только если шифрование включено и
хранилище разблокировано; при включённом, но заблокированном — отказ.

I3 (раунд-3, граница использования GCM): ключ значения = HKDF(MK, info), где info —
КОНСТАНТА колонки, поэтому все строки одной колонки шифруются ОДНИМ ключом со
СЛУЧАЙНЫМ 96-битным nonce на каждый seal. Безопасная граница случайных nonce ~2^32
сообщений на ключ (день рождения); aad=op_id (уникален на строку) дополнительно
привязывает шифртекст к записи. Для oplog-журнала это с огромным запасом (десятки
тысяч ops ≪ 2^32). Если колонка когда-нибудь станет супер-высокочастотной — перейти
на детерминированный nonce-счётчик.
"""

from __future__ import annotations

import base64

from .. import config
from . import primitives as P
from . import session
from .errors import VaultLockedError

PREFIX = "ENC1:"


def _encrypting() -> bool:
    return config.encryption_enabled() and session.is_unlocked()


def seal_str(plaintext: str, *, info: bytes, aad: bytes = b"") -> str:
    """Зашифровать строковое значение для хранения. При выключенном шифровании
    возвращает plaintext как есть (прежнее поведение)."""
    if config.encryption_enabled() and not session.is_unlocked():
        raise VaultLockedError("запись значения при заблокированном хранилище")
    if _encrypting():
        key = P.hkdf(session.get_master_key(), info=info)
        ct = P.seal(key, plaintext.encode("utf-8"), aad=aad)
        return PREFIX + base64.b64encode(ct).decode("ascii")
    return plaintext


def open_str(stored: str, *, info: bytes, aad: bytes = b"") -> str:
    """Расшифровать значение, сохранённое seal_str. Plaintext (без префикса)
    возвращается как есть. Бросает VaultLockedError, если зашифровано, а ключа нет."""
    if stored.startswith(PREFIX):
        mk = session.get_master_key()
        if mk is None:
            raise VaultLockedError("зашифрованное значение без ключа")
        key = P.hkdf(mk, info=info)
        pt = P.open_sealed(key, base64.b64decode(stored[len(PREFIX):]), aad=aad)
        return pt.decode("utf-8")
    return stored


def is_encrypted_value(stored: str | None) -> bool:
    return isinstance(stored, str) and stored.startswith(PREFIX)
