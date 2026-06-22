"""Тест миграции хранилища в зашифрованный формат (включение шифрования на данных).

Создаём plaintext-данные (папка, заметка с legacy-вложением, событие, общая настройка,
oplog), включаем шифрование, мигрируем — и проверяем, что ВСЁ на диске зашифровано,
но читается; устаревший plaintext-индекс удалён; бэкап создан.

Изоляция обязательна. Запуск:
    .venv/bin/python tests/test_migrate.py
"""

import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ATT = "вложение с секретом ПАРОЛЬ".encode("utf-8") + bytes(range(64))


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="qtnotes-migrate-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    return tmp


def main() -> None:
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import crypto_fs, index, migrate, vault
    from qtnotes.storage.models import Attachment, Note
    from qtnotes.sync import oplog

    tmp = _fresh_env()
    try:
        # --- 1) данные в PLAINTEXT (шифрование выкл) ---
        config.set_encryption_enabled(False)
        session.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()

        f = vault.create_folder("Личное", icon="letter")
        note = Note.create_text(f.id, "<p>тайна 4242</p>", "тайна 4242")
        adir = vault.attachments_dir(f.id, note.id)
        (adir / "doc.bin").write_bytes(ATT)
        note.attachments.append(Attachment(file="doc.bin", mime="application/octet-stream",
                                           name="doc.bin", size=len(ATT)))
        vault.save_note(note)  # enc/sync выкл → вложение остаётся legacy-файлом
        vault.add_event("2026-06-21", "Событие X", "#ff0000")
        vault.set_shared("theme", {"palette": {"accent": "#abcdef"}})
        oplog.append_local("note.put", note.id, note.as_dict())  # plaintext payload в oplog

        # индекс построен в plaintext в самом vault
        assert (config.vault_dir() / "index.sqlite").exists()
        assert not crypto_fs.is_encrypted_file(config.folders_dir() / f.id / "folder.json")

        # --- 2) включаем шифрование + бэкап + миграция ---
        mk = P.random_bytes(32)
        config.set_encryption_enabled(True)
        session.set_master_key(mk)
        oplog.reset_for_tests()
        index.reset_for_tests()

        backup = migrate.backup_zip()
        assert backup.exists() and backup.stat().st_size > 0, "бэкап должен быть создан"
        # S3: бэкап лежит в owned-каталоге (duress сотрёт его), а НЕ рядом с vault
        from qtnotes.storage import owned_paths
        assert owned_paths.is_owned(backup), "бэкап миграции должен быть в owned-зоне"
        assert backup.parent != config.vault_dir().parent, "не рядом с vault"
        stats = migrate.migrate_encrypt()
        assert stats["folders"] >= 1 and stats["notes"] >= 1
        # S3: после успешной миграции плейнтекст-бэкап удаляется
        migrate.cleanup_backup(backup)
        assert not backup.exists(), "плейнтекст-бэкап должен быть удалён после миграции"

        # --- 3) проверки: всё на диске зашифровано ---
        fp = config.folders_dir() / f.id / "folder.json"
        np = config.folders_dir() / f.id / "notes" / f"{note.id}.json"
        ep = config.calendar_dir() / "events.json"
        spath = config.vault_dir() / "shared.json"
        for p in (fp, np, ep, spath):
            assert crypto_fs.is_encrypted_file(p), f"{p.name} должен быть зашифрован"
        assert b"4242" not in np.read_bytes()

        # вложение мигрировано в зашифрованный blob, legacy удалён
        n2 = vault.find_note(note.id)
        att = n2.attachments[0]
        assert att.sha256, "ensure_blobs должен проставить sha256 при миграции"
        assert not (adir / "doc.bin").exists()
        bp = vault.blob_path(att.sha256)
        assert crypto_fs.is_encrypted_file(bp)
        assert vault.read_blob_bytes(att.sha256) == ATT  # читается обратно

        # oplog payload зашифрован
        con = sqlite3.connect(str(config.sync_db_path()))
        raw = con.execute("SELECT payload FROM ops LIMIT 1").fetchone()[0]
        con.close()
        assert raw.startswith("ENC1:") and "4242" not in raw

        # устаревший plaintext-индекс в vault удалён; индекс теперь в tmpfs
        assert not (config.vault_dir() / "index.sqlite").exists()
        assert "qtnotes-index-" in str(config.index_path())

        # --- 4) данные по-прежнему читаются и ищутся ---
        assert n2.plaintext == "тайна 4242"
        assert vault.list_events()[0].name == "Событие X"
        assert vault.get_shared("theme")["palette"]["accent"] == "#abcdef"
        assert index.folder_of(note.id) == f.id
        rows = index.candidate_rows("тайна", f.id)
        assert any("тайна" in r["plaintext"] for r in rows)

        # --- 5) идемпотентность: повторная миграция не ломает ---
        stats2 = migrate.migrate_encrypt()
        assert vault.find_note(note.id).plaintext == "тайна 4242"
        assert stats2["folders"] >= 1

        print("OK миграция: всё зашифровано, читается, индекс перестроен, бэкап создан, идемпотентно")
        print("\nТЕСТ МИГРАЦИИ ПРОЙДЕН")
    finally:
        from qtnotes.crypto import session as s
        s.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
