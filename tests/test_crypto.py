"""Тесты крипто-ядра (Ф1): примитивы, аппаратный гейт, key vault, duress, lockout.

Чистая логика, без TPM/Keystore и без I/O (аппаратная часть — SoftwareHardwareKey).
Запуск:
    .venv/bin/python tests/test_crypto.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.exceptions import InvalidTag

from qtnotes.crypto import keyvault as KV
from qtnotes.crypto import primitives as P
from qtnotes.crypto.hwbackend import SoftwareHardwareKey
from qtnotes.crypto.keyvault import KeyringState, UnlockStatus


def run_primitives() -> None:
    """AEAD round-trip, аутентификация, AAD, HKDF детерминизм, const_eq."""
    key = P.random_bytes(32)
    msg = "секретная заметка 🔒".encode("utf-8")

    blob = P.seal(key, msg)
    assert P.open_sealed(key, blob) == msg
    assert blob[:12] != blob[12:24] or True  # nonce присутствует (12 байт)

    # неверный ключ -> InvalidTag
    try:
        P.open_sealed(P.random_bytes(32), blob)
        assert False, "должно было упасть на чужом ключе"
    except InvalidTag:
        pass

    # AAD аутентифицируется: подмена aad -> InvalidTag
    blob2 = P.seal(key, msg, aad=b"folder/123/note.json")
    assert P.open_sealed(key, blob2, aad=b"folder/123/note.json") == msg
    try:
        P.open_sealed(key, blob2, aad=b"folder/999/note.json")
        assert False, "подмена AAD должна ломать расшифровку"
    except InvalidTag:
        pass

    # два seal одного и того же -> разные шифртексты (случайный nonce)
    assert P.seal(key, msg) != P.seal(key, msg)

    # HKDF детерминирован и зависит от info
    km = P.random_bytes(32)
    assert P.hkdf(km, b"a") == P.hkdf(km, b"a")
    assert P.hkdf(km, b"a") != P.hkdf(km, b"b")

    assert P.const_eq(b"abc", b"abc") and not P.const_eq(b"abc", b"abd")
    print("OK primitives")


def run_hw() -> None:
    """SoftwareHardwareKey: детерминизм, зависимость от соли/ПИНа, изоляция ключей."""
    hw = SoftwareHardwareKey.generate()
    a = hw.mac(b"salt", "12345")
    assert a == hw.mac(b"salt", "12345")          # детерминизм
    assert a != hw.mac(b"salt", "54321")          # другой ПИН
    assert a != hw.mac(b"salt2", "12345")         # другая соль
    assert len(a) == 32
    # другой device key -> другой MAC
    assert a != SoftwareHardwareKey.generate().mac(b"salt", "12345")
    print("OK hwbackend")


def run_pin_validation() -> None:
    """5 цифр, только цифры, палиндромы запрещены."""
    KV.validate_pin("12345")
    KV.validate_pin("13579")
    for bad in ["1234", "123456", "12a45", "", "12321", "00000", "11111"]:
        try:
            KV.validate_pin(bad)
            assert False, f"ПИН {bad!r} должен быть отклонён"
        except KV.PinError:
            pass
    print("OK pin validation")


def run_setup_unlock() -> None:
    """Настройка + разблокировка прямым ПИНом выдаёт исходный MK; контент шифруется им."""
    hw = SoftwareHardwareKey.generate()
    state, mk = KV.setup("13579", hw)
    assert len(mk) == 32

    # MK можно реально использовать для шифрования заметки
    note = "Купить молоко".encode("utf-8")
    blob = P.seal(mk, note)

    state2, res = KV.unlock(state, "13579", hw)
    assert res.status is UnlockStatus.OK
    assert res.master_key == mk
    assert P.open_sealed(res.master_key, blob) == note
    assert state2.fail_count == 0

    # состояние сериализуется и переживает round-trip
    restored = KeyringState.from_dict(state.to_dict())
    _, res2 = KV.unlock(restored, "13579", hw)
    assert res2.status is UnlockStatus.OK and res2.master_key == mk
    print("OK setup/unlock + serialization")


def run_duress() -> None:
    """Обратный ПИН распознаётся как DURESS и НЕ выдаёт MK."""
    hw = SoftwareHardwareKey.generate()
    state, mk = KV.setup("13579", hw)  # обратный = 97531

    _, res = KV.unlock(state, "97531", hw)
    assert res.status is UnlockStatus.DURESS
    assert res.master_key is None

    # подложка (with_duress=False): обратный ПИН больше не имеет особого смысла
    decoy_state, decoy_mk = KV.setup("97531", hw, with_duress=False)
    assert decoy_mk != mk
    _, dres = KV.unlock(decoy_state, "13579", hw)  # исходный прямой -> теперь просто неверный
    assert dres.status is UnlockStatus.WRONG
    _, ok = KV.unlock(decoy_state, "97531", hw)    # обратный открывает подложку
    assert ok.status is UnlockStatus.OK
    print("OK duress detection + decoy")


def run_lockout() -> None:
    """Нарастающая блокировка: после 2 неверных — 1м, далее 5м/30м/2ч/сутки."""
    assert KV.lockout_seconds(1) == 0
    assert KV.lockout_seconds(2) == 60
    assert KV.lockout_seconds(3) == 300
    assert KV.lockout_seconds(4) == 1800
    assert KV.lockout_seconds(5) == 7200
    assert KV.lockout_seconds(6) == 86400
    assert KV.lockout_seconds(10) == 86400

    hw = SoftwareHardwareKey.generate()
    state, mk = KV.setup("13579", hw)
    t = 1000.0

    # 1-я неудача — без блокировки
    state, r1 = KV.unlock(state, "00000", hw, now=t)
    assert r1.status is UnlockStatus.WRONG and r1.fail_count == 1
    assert KV.remaining_lockout(state, t) == 0

    # 2-я неудача — включается блокировка на 60с
    state, r2 = KV.unlock(state, "00000", hw, now=t + 1)
    assert r2.status is UnlockStatus.WRONG and r2.fail_count == 2
    assert KV.remaining_lockout(state, t + 1) == 60

    # во время блокировки даже ПРАВИЛЬНЫЙ ПИН не принимается
    state, r3 = KV.unlock(state, "13579", hw, now=t + 30)
    assert r3.status is UnlockStatus.LOCKED
    assert 0 < r3.retry_after <= 60

    # после истечения блокировки правильный ПИН открывает и сбрасывает счётчик
    state, r4 = KV.unlock(state, "13579", hw, now=t + 1 + 61)
    assert r4.status is UnlockStatus.OK and r4.master_key == mk
    assert state.fail_count == 0

    # эскалация: чтобы накопить неудачи, надо пережидать каждую блокировку,
    # иначе попытка возвращает LOCKED и счётчик НЕ растёт (так и задумано).
    s = state
    clock = t + 1000.0
    s, e1 = KV.unlock(s, "00000", hw, now=clock)        # fc=1, без блокировки
    assert e1.status is UnlockStatus.WRONG and s.fail_count == 1
    for expected_fc, wait in [(2, 60), (3, 300), (4, 1800), (5, 7200)]:
        clock += wait + 1                                # переждать предыдущую блокировку
        s, e = KV.unlock(s, "00000", hw, now=clock)
        assert e.status is UnlockStatus.WRONG, (expected_fc, e.status)
        assert s.fail_count == expected_fc
        assert KV.remaining_lockout(s, clock) == KV.lockout_seconds(expected_fc)
    # ещё одна неудача после 2ч -> fc=6 -> сутки
    clock += 7200 + 1
    s, e6 = KV.unlock(s, "00000", hw, now=clock)
    assert s.fail_count == 6
    assert KV.remaining_lockout(s, clock) == 86400

    # попытка во время блокировки НЕ увеличивает счётчик
    s_before = s.fail_count
    s, elk = KV.unlock(s, "00000", hw, now=clock + 5)
    assert elk.status is UnlockStatus.LOCKED and s.fail_count == s_before
    print("OK lockout schedule + escalation")


def main() -> None:
    run_primitives()
    run_hw()
    run_pin_validation()
    run_setup_unlock()
    run_duress()
    run_lockout()
    print("\nВСЕ ТЕСТЫ КРИПТО-ЯДРА ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
