"""Тесты контроллера разблокировки (Ф4a).

- программный бэкенд: setup → unlock(OK выдаёт MK + сессия), wrong→lockout, duress→
  не разблокирует, lock() забывает ключ;
- TPM-бэкенд: NV-«пол» держит блокировку, даже если файл keyring подделать (сбросить
  счётчик). Гейтится наличием TPM; использует тестовый NV-индекс и удаляет его.

Изоляция обязательна. Запуск:
    .venv/bin/python tests/test_unlock.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="qtnotes-unlock-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    return tmp


def run_controller_software() -> None:
    from qtnotes.crypto import session, unlock
    from qtnotes.crypto.hwbackend import SoftwareHardwareKey
    from qtnotes.crypto.keyvault import UnlockStatus

    sw = SoftwareHardwareKey.generate()

    assert not unlock.is_configured()
    mk = unlock.setup_pin("13579", sw)            # обратный = 97531
    assert session.is_unlocked() and session.get_master_key() == mk
    assert unlock.is_configured()

    # имитируем перезапуск: забыли ключ, открываем заново
    session.lock()
    assert not session.is_unlocked()
    res = unlock.try_unlock("13579", sw)
    assert res.status is UnlockStatus.OK and res.master_key == mk
    assert session.is_unlocked()

    # неверный ПИН: 1-я — без блокировки, 2-я — 60с
    r1 = unlock.try_unlock("00000", sw, now=1000.0)
    assert r1.status is UnlockStatus.WRONG and r1.fail_count == 1
    r2 = unlock.try_unlock("00000", sw, now=1001.0)
    assert r2.status is UnlockStatus.WRONG and r2.fail_count == 2
    assert unlock.remaining_lockout(sw, now=1001.0) == 60
    # верный ПИН во время блокировки — отказ
    rl = unlock.try_unlock("13579", sw, now=1030.0)
    assert rl.status is UnlockStatus.LOCKED
    # после окончания — открывает и сбрасывает счётчик
    ro = unlock.try_unlock("13579", sw, now=1001.0 + 61)
    assert ro.status is UnlockStatus.OK
    assert unlock.remaining_lockout(sw, now=2000.0) == 0

    # lock() забывает ключ
    session.lock()
    unlock.try_unlock("13579", sw)
    assert session.is_unlocked()
    unlock.lock()
    assert not session.is_unlocked()

    # duress: обратный ПИН ВЫПОЛНЯЕТ стирание и открывает ПОДЛОЖКУ (возвращает OK);
    # реальные данные заменяются папкой «123». Полная проверка — в test_duress.
    session.lock()
    rd = unlock.try_unlock("97531", sw)
    assert rd.status is UnlockStatus.OK and rd.master_key is not None
    assert session.is_unlocked()
    from qtnotes.storage import vault
    from qtnotes.crypto import duress as _d
    fs = vault.list_folders()
    assert len(fs) == 1 and fs[0].name in _d.DECOY_FOLDERS
    print("OK контроллер (software): setup/unlock/lockout/lock/duress→подложка")


def run_controller_tpm_nvfloor() -> None:
    """NV-«пол» сохраняет блокировку даже при подделке файла keyring."""
    from qtnotes.crypto import session, tpm, unlock
    from qtnotes.crypto.keyvault import KeyringState, UnlockStatus

    test_handle = "0x01800011"
    orig_handle = unlock.NV_HANDLE
    unlock.NV_HANDLE = test_handle
    tpm.counter_undefine(test_handle)
    try:
        backend = unlock.default_backend()  # TpmHardwareKey(config.keyring_dir())
        unlock.setup_pin("13579", backend)
        session.lock()

        # две неверных при now=T → блокировка 60с; NV увеличен на 2
        T = 5000.0
        unlock.try_unlock("00000", backend, now=T)
        r2 = unlock.try_unlock("00000", backend, now=T + 1)
        assert r2.status is UnlockStatus.WRONG and r2.fail_count == 2
        assert unlock.remaining_lockout(backend, now=T + 1) == 60

        # ПОДДЕЛКА: сбрасываем счётчик в файле в 0 (last_fail_ts оставляем) —
        # как будто атакующий обнулил keyring.json, чтобы снять блокировку
        stored = unlock._read()
        tampered = KeyringState(**{**stored.state.__dict__, "fail_count": 0})
        unlock._write(unlock._Stored(state=tampered, nv_baseline=stored.nv_baseline,
                                     backend=stored.backend))
        assert unlock._read().state.fail_count == 0  # файл подделан

        # NV-«пол» всё равно держит блокировку
        eff = unlock._effective_state(unlock._read(), backend)
        assert eff.fail_count >= 2, eff.fail_count
        rl = unlock.try_unlock("13579", backend, now=T + 30)
        assert rl.status is UnlockStatus.LOCKED, "NV-пол должен сохранять блокировку"

        # после окончания окна верный ПИН открывает и сбрасывает базлайн
        ro = unlock.try_unlock("13579", backend, now=T + 1 + 61)
        assert ro.status is UnlockStatus.OK
        assert unlock.remaining_lockout(backend, now=T + 200) == 0
    finally:
        unlock.NV_HANDLE = orig_handle
        tpm.counter_undefine(test_handle)
        session.lock()
    print("OK контроллер (TPM): NV-пол держит блокировку при подделке файла")


def run_tpm_wipe() -> None:
    """После >5 неверных ПИНов — необратимое самостирание (только при NV-счётчике)."""
    from qtnotes import config
    from qtnotes.crypto import session, tpm, unlock
    from qtnotes.crypto.keyvault import UnlockStatus
    from qtnotes.storage import vault
    from qtnotes.storage.models import Note

    test_handle = "0x01800013"
    orig = unlock.NV_HANDLE
    unlock.NV_HANDLE = test_handle
    tpm.counter_undefine(test_handle)
    try:
        backend = unlock.default_backend()
        unlock.setup_pin("13579", backend)

        # реальные данные + личный файл в корне vault (должен уцелеть)
        f = vault.create_folder("Реальная", icon="letter")
        vault.save_note(Note.create_text(f.id, "<p>секрет</p>", "секрет"))
        personal = config.vault_dir() / "personal.kdbx"
        personal.write_bytes(b"PRIVATE")
        assert unlock.is_configured()

        session.lock()
        # 6 неверных, пережидая таймерные блокировки (1м,5м,30м,2ч). 6-й → стирание.
        nows = [1000.0, 1001.0, 1062.0, 1363.0, 3164.0, 10365.0]
        last = None
        for nw in nows:
            last = unlock.try_unlock("00000", backend, now=nw)
        assert last.status is UnlockStatus.WIPED, last.status

        # реальные данные стёрты; шифрование и синк выключены; ПИН не настроен
        assert not (config.folders_dir() / f.id).exists()
        assert config.encryption_enabled() is False
        assert config.sync_enabled() is False
        assert not unlock.is_configured()
        assert not session.is_unlocked()
        # личный файл в корне vault уцелел
        assert personal.exists() and personal.read_bytes() == b"PRIVATE"
    finally:
        unlock.NV_HANDLE = orig
        tpm.counter_undefine(test_handle)
        session.lock()
    print("OK контроллер (TPM): >5 неверных → самостирание, личный файл цел")


def _isolated(test) -> None:
    from qtnotes.crypto import session
    from qtnotes.storage import index
    from qtnotes.sync import oplog
    tmp = _fresh_env()
    session.lock()
    oplog.reset_for_tests()
    index.reset_for_tests()
    try:
        test()
    finally:
        session.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    _isolated(run_controller_software)
    from qtnotes.crypto import tpm
    if tpm.available():
        _isolated(run_controller_tpm_nvfloor)
        _isolated(run_tpm_wipe)
    else:
        print("ПРОПУЩЕНО (TPM): NV-пол, самостирание")
    print("\nВСЕ ТЕСТЫ КОНТРОЛЛЕРА РАЗБЛОКИРОВКИ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
