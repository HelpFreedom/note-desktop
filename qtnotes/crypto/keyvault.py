"""Управление мастер-ключом (key vault): настройка ПИНа, разблокировка, различение
прямого/обратного (duress) ПИНа и нарастающая блокировка после неверных попыток.

Платформонезависимо и без I/O: состояние — сериализуемый KeyringState, аппаратная
часть инъектируется через HardwareKey. Это позволяет покрыть всю логику чистыми
unit-тестами без TPM/Keystore.

Модель ключей (см. docs/encryption-and-duress-plan.md):
- MK (мастер-ключ, 32 байта) шифрует весь контент vault. На диске не хранится.
- `wrapped_mk = seal(wrap_key, MK)`, где `wrap_key = HKDF(hw.mac(salt_wrap, normalPIN))`.
  Развернуть MK может только ПРЯМОЙ ПИН.
- `duress_tag = HKDF(hw.mac(salt_duress, reverse(normalPIN)))`. Обратный ПИН MK не
  разворачивает, а опознаётся сравнением тега → триггер стирания.
- Неверный ПИН не совпадает ни с одним → инкремент счётчика и блокировка.

ПИН в открытом виде не хранится нигде; при настройке он известен лишь временно.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum

from cryptography.exceptions import InvalidTag

from . import primitives as P
from .hwbackend import HardwareKey

PIN_LEN = 5
_INFO_WRAP = b"qtnotes/mk-wrap/v1"
_INFO_DURESS = b"qtnotes/duress-tag/v1"

# M1: медленный (memory-hard) KDF поверх аппаратного гейта. Защита-в-глубину: если гейт
# окажется быстрым/программным (TPM недоступен, software-backend), brute-force 5-значного
# ПИНа (~100k) упирается в scrypt (≈десятки мс/попытка), а не в одно HKDF. Параметры
# хранятся в keyring → можно поднять без потери совместимости. scrypt — stdlib (без новых
# зависимостей). N=16384,r=8,p=1 → ~16 MiB памяти, под дефолтным maxmem=32 MiB.
DEFAULT_KDF: dict = {"algo": "scrypt", "n": 16384, "r": 8, "p": 1}


def _stretch(material: bytes, salt: bytes, kdf: dict | None) -> bytes:
    """Медленное растяжение материала гейта (или no-op для legacy keyring без kdf)."""
    if not kdf:
        return material
    if kdf.get("algo") == "scrypt":
        return hashlib.scrypt(material, salt=salt, n=int(kdf["n"]), r=int(kdf["r"]),
                              p=int(kdf["p"]), dklen=P.KEY_LEN, maxmem=64 * 1024 * 1024)
    raise ValueError(f"неизвестный KDF keyring: {kdf}")

# Нарастающая блокировка: число неудач подряд -> секунды ожидания.
# Срабатывает после 2-й неверной попытки: 1м, 5м, 30м, 2ч, дальше сутки.
_LOCKOUT_SCHEDULE = {
    0: 0,
    1: 0,
    2: 60,
    3: 5 * 60,
    4: 30 * 60,
    5: 2 * 60 * 60,
}
_LOCKOUT_MAX = 24 * 60 * 60  # 6+ неудач -> сутки

# Самостирание: после БОЛЕЕ ЧЕМ стольких неверных ПИНов подряд все данные стираются
# безвозвратно (десктоп, на аппаратном NV-счётчике — нельзя обойти переводом часов).
# Так короткий ПИН защищён от перебора: вор успеет проверить лишь 6 вариантов из 100000,
# после чего данные самоуничтожаются. Счётчик сбрасывается при каждой УСПЕШНОЙ
# разблокировке, поэтому легитимный пользователь, помнящий ПИН, до стирания не доходит.
WIPE_AFTER_FAILS = 5


class PinError(ValueError):
    """ПИН не соответствует требованиям (длина/цифры/палиндром)."""


def validate_pin(pin: str) -> None:
    """Проверить требования к ПИНу. Бросает PinError при нарушении.

    Палиндром запрещён: иначе обратный ПИН совпал бы с прямым и duress был бы
    неотличим от обычной разблокировки.
    """
    if not isinstance(pin, str) or len(pin) != PIN_LEN:
        raise PinError(f"ПИН должен состоять из {PIN_LEN} цифр")
    if not pin.isdigit():
        raise PinError("ПИН должен содержать только цифры")
    if pin == pin[::-1]:
        raise PinError("ПИН-палиндром запрещён (обратный совпал бы с прямым)")


def lockout_seconds(fail_count: int) -> int:
    """Длительность блокировки для данного числа неудач подряд."""
    if fail_count >= 6:
        return _LOCKOUT_MAX
    return _LOCKOUT_SCHEDULE.get(fail_count, 0)


class UnlockStatus(Enum):
    OK = "ok"            # прямой ПИН — выдан MK
    DURESS = "duress"    # обратный ПИН — нужно стирание
    WRONG = "wrong"      # неверный ПИН — счётчик увеличен
    LOCKED = "locked"    # сейчас временная блокировка (по таймеру), попытка не принята
    WIPED = "wiped"      # превышен лимит неверных попыток — данные стёрты безвозвратно


@dataclass
class UnlockResult:
    status: UnlockStatus
    master_key: bytes | None = None   # только при OK
    retry_after: int = 0              # сек до конца блокировки (при LOCKED)
    fail_count: int = 0               # текущий счётчик неудач


@dataclass
class KeyringState:
    """Сериализуемое состояние ключа на диске (без MK и без ПИНа)."""
    version: int
    salt_wrap: bytes
    salt_duress: bytes
    wrapped_mk: bytes
    duress_tag: bytes | None   # None у подложки (второго уровня duress нет)
    fail_count: int = 0
    last_fail_ts: float = 0.0
    kdf: dict | None = None     # M1: параметры медленного KDF; None — legacy (без растяжения)

    # --- (де)сериализация: bytes -> base64 в JSON-совместимый dict ---

    def to_dict(self) -> dict:
        def b64(b: bytes | None):
            return base64.b64encode(b).decode("ascii") if b is not None else None
        return {
            "version": self.version,
            "salt_wrap": b64(self.salt_wrap),
            "salt_duress": b64(self.salt_duress),
            "wrapped_mk": b64(self.wrapped_mk),
            "duress_tag": b64(self.duress_tag),
            "fail_count": self.fail_count,
            "last_fail_ts": self.last_fail_ts,
            "kdf": self.kdf,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "KeyringState":
        def ub64(s):
            return base64.b64decode(s) if s is not None else None
        return cls(
            version=int(d["version"]),
            salt_wrap=ub64(d["salt_wrap"]),
            salt_duress=ub64(d["salt_duress"]),
            wrapped_mk=ub64(d["wrapped_mk"]),
            duress_tag=ub64(d["duress_tag"]),
            fail_count=int(d.get("fail_count", 0)),
            last_fail_ts=float(d.get("last_fail_ts", 0.0)),
            kdf=d.get("kdf"),
        )


def _wrap_key(hw: HardwareKey, salt: bytes, pin: str, kdf: dict | None) -> bytes:
    return P.hkdf(_stretch(hw.mac(salt, pin), salt, kdf), info=_INFO_WRAP)


def _duress_tag(hw: HardwareKey, salt: bytes, pin: str, kdf: dict | None) -> bytes:
    return P.hkdf(_stretch(hw.mac(salt, pin), salt, kdf), info=_INFO_DURESS)


def setup(pin: str, hw: HardwareKey, *, with_duress: bool = True,
          master_key: bytes | None = None) -> tuple[KeyringState, bytes]:
    """Создать новый key vault под заданный ПИН. Возвращает (состояние, MK).

    with_duress=False — для подложки после стирания: у неё нет обратного ПИНа, чтобы
    исходный (прямой) ПИН не имел никакого особого смысла.
    """
    validate_pin(pin)
    mk = master_key if master_key is not None else P.random_bytes(P.KEY_LEN)
    if len(mk) != P.KEY_LEN:
        raise ValueError("master_key должен быть 32 байта")
    salt_wrap = P.random_bytes(16)
    salt_duress = P.random_bytes(16)
    kdf = dict(DEFAULT_KDF)
    wrapped = P.seal(_wrap_key(hw, salt_wrap, pin, kdf), mk)
    tag = _duress_tag(hw, salt_duress, pin[::-1], kdf) if with_duress else None
    state = KeyringState(
        version=1,
        salt_wrap=salt_wrap,
        salt_duress=salt_duress,
        wrapped_mk=wrapped,
        duress_tag=tag,
        kdf=kdf,
    )
    return state, mk


def _upgrade_kdf(state: KeyringState, pin: str, hw: HardwareKey, mk: bytes) -> KeyringState:
    """Перевести legacy keyring на медленный KDF при успешном входе (ПИН известен).
    Новые соли + перезаворот MK + пересчёт duress-тега (если он был). Сбрасывает счётчик.

    I8 (раунд-3): апгрейд возможен ТОЛЬКО при входе — KDF растягивает ПИН, а ПИН доступен
    лишь в момент разблокировки (не при загрузке файла). Это самый ранний возможный момент.
    До первого входа legacy-keyring без scrypt защищён аппаратным гейтом (TPM/Keystore):
    офлайн-перебор ПИНа невозможен без железа, поэтому отсутствие KDF-растяжки до первого
    входа не открывает практической атаки."""
    kdf = dict(DEFAULT_KDF)
    salt_wrap = P.random_bytes(16)
    salt_duress = P.random_bytes(16)
    wrapped = P.seal(_wrap_key(hw, salt_wrap, pin, kdf), mk)
    tag = (_duress_tag(hw, salt_duress, pin[::-1], kdf)
           if state.duress_tag is not None else None)
    return KeyringState(
        version=state.version, salt_wrap=salt_wrap, salt_duress=salt_duress,
        wrapped_mk=wrapped, duress_tag=tag, kdf=kdf, fail_count=0, last_fail_ts=0.0)


def remaining_lockout(state: KeyringState, now: float | None = None) -> int:
    """Сколько секунд ещё длится блокировка (0 — не заблокировано)."""
    now = time.time() if now is None else now
    dur = lockout_seconds(state.fail_count)
    if dur <= 0:
        return 0
    left = int(state.last_fail_ts + dur - now)
    return left if left > 0 else 0


def unlock(state: KeyringState, pin: str, hw: HardwareKey,
           now: float | None = None) -> tuple[KeyringState, UnlockResult]:
    """Попытка разблокировки. Возвращает (новое_состояние, результат).

    Состояние возвращается обновлённым (счётчик неудач/время), его нужно сохранить.
    При DURESS вызывающий код обязан выполнить крипто-стирание (Ф6) — здесь только
    распознавание, без побочных эффектов.
    """
    now = time.time() if now is None else now

    left = remaining_lockout(state, now)
    if left > 0:
        return state, UnlockResult(UnlockStatus.LOCKED, retry_after=left,
                                   fail_count=state.fail_count)

    # 1) прямой ПИН?
    try:
        mk = P.open_sealed(_wrap_key(hw, state.salt_wrap, pin, state.kdf), state.wrapped_mk)
    except InvalidTag:
        mk = None
    if mk is not None:
        if not state.kdf:
            # M1: legacy keyring без медленного KDF → усиливаем при первом успешном входе
            # (перезаворачиваем MK и пересчитываем duress-тег под scrypt; ПИН известен).
            new = _upgrade_kdf(state, pin, hw, mk)
        else:
            new = KeyringState(**{**state.__dict__, "fail_count": 0, "last_fail_ts": 0.0})
        return new, UnlockResult(UnlockStatus.OK, master_key=mk, fail_count=0)

    # 2) обратный (duress) ПИН?
    if state.duress_tag is not None:
        cand = _duress_tag(hw, state.salt_duress, pin, state.kdf)
        if P.const_eq(cand, state.duress_tag):
            # счётчик не трогаем: это распознанный ПИН, а не промах
            return state, UnlockResult(UnlockStatus.DURESS, fail_count=state.fail_count)

    # 3) неверный ПИН → инкремент и (возможно) блокировка
    new = KeyringState(**{**state.__dict__,
                          "fail_count": state.fail_count + 1,
                          "last_fail_ts": now})
    res = UnlockResult(UnlockStatus.WRONG, fail_count=new.fail_count,
                       retry_after=remaining_lockout(new, now))
    return new, res
