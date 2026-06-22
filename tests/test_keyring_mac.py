"""P11: integrity-MAC keyring против подделки лимита перебора PIN.

- setup_pin пишет keyring с полем mac;
- верный ПИН при валидном MAC разблокирует;
- подделка (занижение fail_count / подмена nv_baseline без пересчёта mac) → LOCKED,
  и НИКОГДА не WIPED (неразрушающе);
- legacy-файл без mac по-прежнему разблокируется (обратная совместимость).

Запуск: .venv/bin/python tests/test_keyring_mac.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    os.environ["QTNOTES_VAULT"] = tempfile.mkdtemp(prefix="qtnotes_vault_")
    from qtnotes.crypto import hwbackend, unlock
    from qtnotes.crypto.keyvault import UnlockStatus

    backend = hwbackend.SoftwareHardwareKey.generate()
    pin = "12345"
    unlock.setup_pin(pin, backend)
    kp = unlock.keyring_path()

    d = json.loads(kp.read_text(encoding="utf-8"))
    assert d.get("mac"), "MAC не записан в keyring"
    print("OK: keyring записан с MAC")

    res = unlock.try_unlock(pin, backend)
    assert res.status is UnlockStatus.OK, f"валидный MAC: ожидали OK, {res.status}"
    print("OK: верный ПИН разблокирует при валидном MAC")

    # ПОДДЕЛКА: занизить счётчик и подменить nv_baseline, mac не трогаем
    d = json.loads(kp.read_text(encoding="utf-8"))
    d["keyring"]["fail_count"] = 0
    d["keyring"]["last_fail_ts"] = 0.0
    d["nv_baseline"] = 999999
    kp.write_text(json.dumps(d), encoding="utf-8")
    res = unlock.try_unlock(pin, backend)
    assert res.status is UnlockStatus.LOCKED, f"подделка: ожидали LOCKED, {res.status}"
    assert res.status is not UnlockStatus.WIPED, "подделка вызвала стирание — недопустимо!"
    print("OK: подделка MAC → LOCKED, без стирания")

    # LEGACY: убрать mac → снова разблокируется (обратная совместимость)
    d = json.loads(kp.read_text(encoding="utf-8"))
    d.pop("mac", None)
    d["keyring"]["fail_count"] = 0
    d["keyring"]["last_fail_ts"] = 0.0
    d["nv_baseline"] = 0
    kp.write_text(json.dumps(d), encoding="utf-8")
    res = unlock.try_unlock(pin, backend)
    assert res.status is UnlockStatus.OK, f"legacy без mac: ожидали OK, {res.status}"
    # после успешной разблокировки mac снова дописан
    assert json.loads(kp.read_text(encoding="utf-8")).get("mac"), "mac не восстановлен"
    print("OK: legacy без MAC разблокируется и MAC дописывается заново")

    _tpm_mac_roundtrip()
    print("ALL P11 TESTS PASSED")


def _tpm_mac_roundtrip():
    """Проверка integrity-MAC на РЕАЛЬНОМ TPM (тот путь, что у пользователя), но БЕЗ NV
    и БЕЗ setup_pin — только backend.mac на изолированном temp-ключе (боевые ключи/NV
    не трогаем)."""
    from qtnotes.crypto import tpm
    if not tpm.available():
        print("SKIP: TPM недоступен — пропускаем проверку на железе")
        return
    from qtnotes.crypto import hwbackend, unlock
    from qtnotes.crypto.keyvault import setup as kv_setup
    sw = hwbackend.SoftwareHardwareKey.generate()
    state, _ = kv_setup("13579", sw)  # объект состояния (без NV/записи)
    tpm_dir = tempfile.mkdtemp(prefix="qtnotes_tpm_iso_")
    try:
        hw = hwbackend.TpmHardwareKey(tpm_dir)  # создаст ОТДЕЛЬНЫЙ hmac-ключ в temp
        m1 = unlock._compute_mac(hw, state, 0, "tpm")
        m2 = unlock._compute_mac(hw, state, 0, "tpm")
        assert m1 == m2, "TPM MAC недетерминирован"
        m3 = unlock._compute_mac(hw, state, 999999, "tpm")  # иной nv_baseline
        assert m3 != m1, "TPM MAC не зависит от nv_baseline (подделка не ловится)"
    finally:
        tpm.reset_cache()  # не оставлять temp-ключ в кэше процесса
    print("OK (TPM): integrity-MAC детерминирован и ловит подмену nv_baseline на железе")


if __name__ == "__main__":
    main()
