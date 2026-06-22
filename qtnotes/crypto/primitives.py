"""Низкоуровневые криптопримитивы поверх `cryptography`.

Только проверенные конструкции: AES-256-GCM (AEAD), HKDF-SHA256 (вывод субключей),
HMAC-SHA256 (аппаратный гейт ПИНа), сравнение в постоянное время. Никакой
самодеятельности — все алгоритмы из stdlib `cryptography`.
"""

from __future__ import annotations

import hmac as _hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEY_LEN = 32  # 256 бит
NONCE_LEN = 12  # стандартный nonce для GCM


def random_bytes(n: int) -> bytes:
    """Криптослучайные байты (os.urandom — CSPRNG ОС)."""
    return os.urandom(n)


def hkdf(key_material: bytes, info: bytes, length: int = KEY_LEN,
         salt: bytes | None = None) -> bytes:
    """Вывести субключ из ключевого материала по HKDF-SHA256."""
    return HKDF(
        algorithm=hashes.SHA256(), length=length, salt=salt, info=info
    ).derive(key_material)


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    """HMAC-SHA256. Используется как «аппаратный гейт» в программном бэкенде."""
    return _hmac.new(key, message, "sha256").digest()


def seal(key: bytes, plaintext: bytes, aad: bytes = b"") -> bytes:
    """Зашифровать AES-256-GCM. Результат: nonce(12) || ciphertext+tag.

    Случайный nonce на каждый вызов; для нашего объёма данных вероятность
    коллизии пренебрежимо мала. aad (associated data) аутентифицируется, но не
    шифруется — сюда кладём контекст (например, путь файла) для защиты от подмены.
    """
    if len(key) != KEY_LEN:
        raise ValueError("ключ должен быть 32 байта")
    nonce = random_bytes(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def open_sealed(key: bytes, blob: bytes, aad: bytes = b"") -> bytes:
    """Расшифровать то, что произвёл seal(). Бросает InvalidTag при неверном
    ключе/подмене (cryptography.exceptions.InvalidTag)."""
    if len(key) != KEY_LEN:
        raise ValueError("ключ должен быть 32 байта")
    if len(blob) < NONCE_LEN:
        raise ValueError("слишком короткий blob")
    nonce, ct = blob[:NONCE_LEN], blob[NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad)


def const_eq(a: bytes, b: bytes) -> bool:
    """Сравнение в постоянное время (защита от тайминг-атак)."""
    return _hmac.compare_digest(a, b)
