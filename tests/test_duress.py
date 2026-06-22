"""Тест duress-стирания (Ф6).

Проверяем:
- обратный ПИН → реальные данные необратимо стёрты (заметки/папки/blobs/keyring/TPM-ключ);
- ЛИЧНЫЕ файлы в корне vault (имитация приватного файла) НЕ тронуты — главный инвариант;
- создана подложка: папка «123» + 3 заметки в нужном порядке;
- открывается как обычная разблокировка (OK, MK подложки в сессии);
- подложку открывает ТОЛЬКО обратный ПИН; исходный (прямой) теперь — просто неверный;
- синхронизация отключена, шифрование остаётся включённым.

Изоляция обязательна. Запуск:
    .venv/bin/python tests/test_duress.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="qtnotes-duress-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    return tmp


def run_software() -> None:
    from qtnotes import config
    from qtnotes.crypto import session, unlock
    from qtnotes.crypto.hwbackend import SoftwareHardwareKey
    from qtnotes.crypto.keyvault import UnlockStatus
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import oplog

    tmp = _fresh_env()
    session.lock()
    oplog.reset_for_tests()
    index.reset_for_tests()
    try:
        sw = SoftwareHardwareKey.generate()
        config.set_sync_enabled(True)  # включим — проверим, что duress выключит

        # ЛИЧНЫЙ файл в корне vault (имитация приватного файла) — должен уцелеть
        personal = config.vault_dir() / "BAZA_personal.kdbx"
        config.vault_dir().mkdir(parents=True, exist_ok=True)
        personal.write_bytes(b"MY SECRET KEEPASS DB \x00\x01\x02")

        # шифрование + реальные данные
        real_mk = unlock.setup_pin("13579", sw)   # обратный = 97531
        f = vault.create_folder("Реальная папка", icon="letter")
        n = Note.create_text(f.id, "<p>реальный секрет 4242</p>", "реальный секрет 4242")
        vault.save_note(n)
        note_path = config.folders_dir() / f.id / "notes" / f"{n.id}.json"
        assert note_path.exists()

        # имитация TPM-ключа в keyring — должен быть стёрт (крипто-стирание)
        tpmdir = config.keyring_dir() / "tpm"
        tpmdir.mkdir(parents=True, exist_ok=True)
        (tpmdir / "hmac.priv").write_bytes(b"fake-tpm-priv")
        assert unlock.keyring_path().exists()

        # --- DURESS: вводим ПИН задом наперёд ---
        session.lock()
        res = unlock.try_unlock("97531", sw)
        assert res.status is UnlockStatus.OK, "duress должен открываться как обычная разблокировка"
        decoy_mk = res.master_key
        assert session.is_unlocked() and session.get_master_key() == decoy_mk
        assert decoy_mk != real_mk, "ключ подложки должен отличаться от реального"

        # --- стирание реальных данных ---
        assert not note_path.exists(), "реальная заметка должна быть стёрта"
        assert not (config.folders_dir() / f.id).exists(), "реальная папка должна быть стёрта"
        assert not (tpmdir / "hmac.priv").exists(), "TPM-ключ должен быть стёрт (крипто-стирание)"

        # --- ГЛАВНОЕ: личный файл в корне vault уцелел ---
        assert personal.exists(), "ЛИЧНЫЙ файл не должен быть удалён!"
        assert personal.read_bytes() == b"MY SECRET KEEPASS DB \x00\x01\x02"

        # --- подложка (I1: случайная из пулов) ---
        from qtnotes.crypto import duress as _d
        folders = vault.list_folders()
        assert len(folders) == 1 and folders[0].name in _d.DECOY_FOLDERS, [x.name for x in folders]
        notes = vault.list_notes(folders[0].id)
        texts = [x.plaintext for x in notes]
        assert 2 <= len(texts) <= 4, texts
        assert all(t in _d.DECOY_POOL for t in texts), texts
        assert len(set(texts)) == len(texts), "заметки декоя без повторов"

        # --- подложку открывает ТОЛЬКО обратный ПИН; исходный — теперь неверный ---
        session.lock()
        assert unlock.try_unlock("97531", sw).status is UnlockStatus.OK
        session.lock()
        assert unlock.try_unlock("13579", sw).status is UnlockStatus.WRONG, \
            "исходный прямой ПИН не должен оставлять следа (просто неверный)"

        # --- синк выключен, шифрование включено ---
        assert config.sync_enabled() is False
        assert config.encryption_enabled() is True

        print("OK duress (software): реальное стёрто, личный файл цел, подложка создана, "
              "только обратный ПИН открывает, синк off")
    finally:
        session.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def run_tpm_restart() -> None:
    """На реальном TPM: после duress «перезапуск» (сброс кэша) — подложка открывается
    новым TPM-ключом (проверка фикса инвалидации кэша ключа)."""
    from qtnotes import config
    from qtnotes.crypto import session, tpm, unlock
    from qtnotes.crypto.keyvault import UnlockStatus
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import oplog

    test_handle = "0x01800012"
    orig = unlock.NV_HANDLE
    unlock.NV_HANDLE = test_handle
    tpm.counter_undefine(test_handle)
    tmp = _fresh_env()
    session.lock()
    oplog.reset_for_tests()
    index.reset_for_tests()
    tpm.reset_cache()
    try:
        backend = unlock.default_backend()
        unlock.setup_pin("13579", backend)        # обратный = 97531
        f = vault.create_folder("Реальная", icon="letter")
        vault.save_note(Note.create_text(f.id, "<p>секрет</p>", "секрет"))

        session.lock()
        res = unlock.try_unlock("97531", backend)  # duress
        assert res.status is UnlockStatus.OK

        # имитация ПЕРЕЗАПУСКА: сброс кэша TPM + забыть MK, затем открыть подложку
        tpm.reset_cache()
        session.lock()
        again = unlock.try_unlock("97531", backend)
        assert again.status is UnlockStatus.OK, "подложка должна открываться после перезапуска"
        assert session.is_unlocked()
        from qtnotes.crypto import duress as _d
        folders = vault.list_folders()
        assert len(folders) == 1 and folders[0].name in _d.DECOY_FOLDERS
        print("OK duress (TPM): подложка открывается новым ключом после перезапуска")
    finally:
        unlock.NV_HANDLE = orig
        tpm.counter_undefine(test_handle)
        session.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    run_software()
    from qtnotes.crypto import tpm
    if tpm.available():
        run_tpm_restart()
    else:
        print("ПРОПУЩЕНО (TPM): перезапуск подложки")
    print("\nТЕСТ DURESS-СТИРАНИЯ ПРОЙДЕН")


if __name__ == "__main__":
    main()
