"""Аппаратный гейт ПИНа: интерфейс HardwareKey + программный бэкенд.

Идея: ПИН никогда не проверяется «в открытую» и не превращается в ключ напрямую.
Вместо этого аппаратный неизвлекаемый ключ считает `mac(salt, pin)`. Без доступа к
железу этот MAC не вычислить, поэтому офлайн-перебор файлов невозможен, даже если
злоумышленник скопировал весь vault и метаданные ключей.

- HardwareKey — абстрактный интерфейс (одна операция: mac).
- SoftwareHardwareKey — программная имитация для тестов и для работы ДО подключения
  TPM/Keystore. Держит 32-байтный «device key»; в проде его заменят TPM (десктоп) и
  Android Keystore (телефон), где ключ физически неизвлекаем.

ВАЖНО: SoftwareHardwareKey НЕ даёт настоящей аппаратной защиты от перебора — его
device key лежит в файле. Это слой для разработки/тестов и кросс-платформенной логики;
боевая стойкость появляется в Ф3 (TPM) и Ф5 (Keystore).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from . import primitives as P


class HardwareKey(ABC):
    """Интерфейс аппаратного гейта. Реализации: Software (тут), TPM (Ф3), Keystore (Ф5)."""

    @abstractmethod
    def mac(self, salt: bytes, pin: str) -> bytes:
        """Вернуть детерминированный MAC от (salt, pin) на неизвлекаемом ключе.
        Одинаковые (salt, pin) → одинаковый результат; ключ из железа не выходит."""
        raise NotImplementedError


class SoftwareHardwareKey(HardwareKey):
    """Программная имитация: HMAC-SHA256(device_key, salt || pin). Для тестов и
    переходного периода до TPM/Keystore."""

    def __init__(self, device_key: bytes):
        if len(device_key) != P.KEY_LEN:
            raise ValueError("device_key должен быть 32 байта")
        self._device_key = device_key

    @classmethod
    def generate(cls) -> "SoftwareHardwareKey":
        return cls(P.random_bytes(P.KEY_LEN))

    def mac(self, salt: bytes, pin: str) -> bytes:
        return P.hmac_sha256(self._device_key, salt + pin.encode("utf-8"))


class TpmHardwareKey(HardwareKey):
    """Боевой бэкенд (десктоп): HMAC на неизвлекаемом TPM-ключе. ПИН — вход HMAC, не
    auth ключа, поэтому TPM считает mac для любого ПИНа (различение прямой/обратный/
    неверный — на уровне приложения). Без TPM значение не вычислить → офлайн-перебор
    украденных файлов невозможен."""

    def __init__(self, keyring_dir):
        self._keyring_dir = keyring_dir

    @staticmethod
    def available() -> bool:
        from . import tpm
        return tpm.available()

    def mac(self, salt: bytes, pin: str) -> bytes:
        from . import tpm
        return tpm.hmac(self._keyring_dir, salt + pin.encode("utf-8"))
