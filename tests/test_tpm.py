"""Тесты TPM-кастодии (Ф3) на РЕАЛЬНОМ TPM.

Если TPM недоступен — тест помечает себя пропущенным (не падает). Проверяем:
- TpmHardwareKey.mac: детерминизм, зависимость от ПИНа/соли, длина 32, стабильность
  после сброса кэша (имитация нового процесса, перезагрузка ключа из файлов);
- keyvault поверх TPM-бэкенда работает так же: setup/unlock прямой→OK (MK шифрует),
  обратный→DURESS, неверный→WRONG, нарастающая блокировка;
- NV-счётчик: создание/чтение/инкремент монотонны (с очисткой).

Изоляция: ключ TPM кладётся во временный keyring; NV-индекс тестовый и удаляется.
Запуск:
    .venv/bin/python tests/test_tpm.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_NV_HANDLE = "0x01800010"  # тестовый индекс, удаляем в конце


def run_mac() -> None:
    from qtnotes.crypto import tpm
    from qtnotes.crypto.hwbackend import TpmHardwareKey

    kd = tempfile.mkdtemp(prefix="qtnotes-tpmkey-")
    try:
        hw = TpmHardwareKey(kd)
        a = hw.mac(b"salt-aaaa", "13579")
        assert len(a) == 32
        assert a == hw.mac(b"salt-aaaa", "13579")      # детерминизм
        assert a != hw.mac(b"salt-aaaa", "97531")      # другой ПИН
        assert a != hw.mac(b"salt-bbbb", "13579")      # другая соль

        # стабильность после сброса кэша процесса (перезагрузка ключа из файлов)
        tpm.reset_cache()
        assert a == hw.mac(b"salt-aaaa", "13579"), "mac должен пережить перезагрузку ключа"
    finally:
        tpm.reset_cache()
        shutil.rmtree(kd, ignore_errors=True)
    print("OK TPM mac: детерминизм, зависимость, стабильность после reset")


def run_keyvault_over_tpm() -> None:
    """Вся логика keyvault работает поверх реального TPM-бэкенда без изменений."""
    from qtnotes.crypto import keyvault as KV
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import tpm
    from qtnotes.crypto.hwbackend import TpmHardwareKey
    from qtnotes.crypto.keyvault import KeyringState, UnlockStatus

    kd = tempfile.mkdtemp(prefix="qtnotes-tpmkey-")
    try:
        hw = TpmHardwareKey(kd)
        state, mk = KV.setup("13579", hw)   # обратный = 97531

        # MK реально шифрует и расшифровывает
        blob = P.seal(mk, "секрет".encode("utf-8"))
        state2, res = KV.unlock(state, "13579", hw)
        assert res.status is UnlockStatus.OK and res.master_key == mk
        assert P.open_sealed(res.master_key, blob).decode("utf-8") == "секрет"

        # состояние переживает сериализацию + сброс кэша TPM (как новый процесс)
        tpm.reset_cache()
        restored = KeyringState.from_dict(state.to_dict())
        _, r2 = KV.unlock(restored, "13579", hw)
        assert r2.status is UnlockStatus.OK and r2.master_key == mk

        # duress / wrong
        _, dres = KV.unlock(state, "97531", hw)
        assert dres.status is UnlockStatus.DURESS and dres.master_key is None
        s, w1 = KV.unlock(state, "00000", hw)
        assert w1.status is UnlockStatus.WRONG and s.fail_count == 1
        s, w2 = KV.unlock(s, "00000", hw)
        assert w2.status is UnlockStatus.WRONG and s.fail_count == 2
        assert KV.remaining_lockout(s, s.last_fail_ts) == 60  # 2 неудачи → 1м
    finally:
        tpm.reset_cache()
        shutil.rmtree(kd, ignore_errors=True)
    print("OK keyvault поверх TPM: OK/DURESS/WRONG/lockout как с программным ключом")


def run_nv_counter() -> None:
    from qtnotes.crypto import tpm

    tpm.counter_undefine(_TEST_NV_HANDLE)  # на случай мусора с прошлого запуска
    try:
        tpm.ensure_counter(_TEST_NV_HANDLE)
        v0 = tpm.counter_read(_TEST_NV_HANDLE)
        v1 = tpm.counter_increment(_TEST_NV_HANDLE)
        v2 = tpm.counter_increment(_TEST_NV_HANDLE)
        assert v1 == v0 + 1, (v0, v1)
        assert v2 == v1 + 1, (v1, v2)
        # ensure повторно — идемпотентно, значение не сбрасывается
        tpm.ensure_counter(_TEST_NV_HANDLE)
        assert tpm.counter_read(_TEST_NV_HANDLE) == v2
    finally:
        tpm.counter_undefine(_TEST_NV_HANDLE)
    print("OK TPM NV-счётчик: монотонный инкремент, идемпотентный ensure, очистка")


def main() -> None:
    from qtnotes.crypto import tpm
    if not tpm.available():
        print("ПРОПУЩЕНО: TPM недоступен (нет устройства/прав tss)")
        return
    run_mac()
    run_keyvault_over_tpm()
    run_nv_counter()
    print("\nВСЕ ТЕСТЫ TPM-КАСТОДИИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
