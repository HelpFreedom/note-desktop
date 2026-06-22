"""Тесты шифрования вложений at-rest (Ф2b).

- write_blob/read_blob_bytes: blob на диске зашифрован, читается как плейнтекст;
- store.read_blob отдаёт пиру плейнтекст;
- ensure_blobs мигрирует legacy-вложение в зашифрованный blob (при шифровании);
- attachment_access_path расшифровывает в tmpfs-файл для UI; контент совпадает;
- без шифрования — поведение прежнее (blob plaintext, путь без копирования);
- wipe_blob_cache чистит кэш.

Изоляция обязательна. Запуск:
    .venv/bin/python tests/test_blob_crypto.py
"""

import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="qtnotes-blob-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    return tmp


PLAINTEXT = "двоичные данные с секретным словом ПАРОЛЬ".encode("utf-8") + bytes(range(256))


def run_blob_encrypted_lowlevel() -> None:
    """write_blob шифрует на диске; read_blob_bytes/store.read_blob дают плейнтекст."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import crypto_fs, vault
    from qtnotes.sync.store import GlobalStore

    config.set_encryption_enabled(True)
    session.set_master_key(P.random_bytes(32))

    sha = hashlib.sha256(PLAINTEXT).hexdigest()
    vault.write_blob(sha, PLAINTEXT)

    bp = vault.blob_path(sha)
    assert bp.exists()
    assert crypto_fs.is_encrypted_file(bp), "blob на диске должен быть зашифрован"
    raw = bp.read_bytes()
    assert b"\xd0\x9f\xd0\x90\xd0\xa0\xd0\x9e\xd0\x9b\xd0\xac" not in raw  # слово ПАРОЛЬ
    assert raw != PLAINTEXT

    # читается обратно как плейнтекст
    assert vault.read_blob_bytes(sha) == PLAINTEXT
    # пиру по синку отдаём плейнтекст
    assert GlobalStore().read_blob(sha) == PLAINTEXT
    # дедуп: повторная запись не ломает
    vault.write_blob(sha, PLAINTEXT)
    assert vault.read_blob_bytes(sha) == PLAINTEXT
    print("OK blob low-level: шифр на диске, плейнтекст на чтении и в синке")


def run_ensure_blobs_and_access() -> None:
    """ensure_blobs мигрирует вложение в зашифрованный blob; access_path расшифровывает."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import crypto_fs, vault
    from qtnotes.storage.models import Attachment, Note

    config.set_encryption_enabled(True)
    session.set_master_key(P.random_bytes(32))

    f = vault.create_folder("Картинки", icon="letter")
    note = Note.create_text(f.id, "<p>фото</p>", "фото")
    # кладём legacy-вложение (как делает UI до миграции)
    adir = vault.attachments_dir(f.id, note.id)
    (adir / "secret.png").write_bytes(PLAINTEXT)
    note.attachments.append(
        Attachment(file="secret.png", mime="image/png", name="secret.png",
                   size=len(PLAINTEXT)))

    vault.save_note(note)  # при шифровании → ensure_blobs мигрирует в зашифр. blob

    att = note.attachments[0]
    sha = hashlib.sha256(PLAINTEXT).hexdigest()
    assert att.sha256 == sha, "ensure_blobs должен проставить sha256"
    assert not (adir / "secret.png").exists(), "legacy-файл должен быть удалён"
    bp = vault.blob_path(sha)
    assert crypto_fs.is_encrypted_file(bp), "blob должен быть зашифрован"

    # access_path: расшифрованный tmpfs-файл, читается напрямую, контент совпадает
    ap = vault.attachment_access_path(note, att)
    assert "qtnotes-blobs-" in str(ap), ap
    assert ap.resolve() != bp.resolve()
    assert ap.read_bytes() == PLAINTEXT
    assert ap.suffix == ".png"  # расширение сохранено (для определения формата/внешнего открытия)

    # повторный доступ берёт из кэша (тот же путь)
    assert vault.attachment_access_path(note, att) == ap

    # wipe чистит кэш
    vault.wipe_blob_cache()
    assert not ap.exists()
    print("OK ensure_blobs + access_path: миграция в шифр. blob, расшифровка для UI")


def run_plaintext_default() -> None:
    """Без шифрования: blob — обычный файл, access_path = сам blob (без копий)."""
    from qtnotes import config
    from qtnotes.storage import crypto_fs, vault
    from qtnotes.storage.models import Attachment, Note

    config.set_encryption_enabled(False)
    sha = hashlib.sha256(PLAINTEXT).hexdigest()
    vault.write_blob(sha, PLAINTEXT)
    bp = vault.blob_path(sha)
    assert not crypto_fs.is_encrypted_file(bp)
    assert bp.read_bytes() == PLAINTEXT  # на диске плейнтекст (как раньше)
    assert vault.read_blob_bytes(sha) == PLAINTEXT

    f = vault.create_folder("Обычные", icon="letter")
    note = Note.create_text(f.id, "<p>x</p>", "x")
    att = Attachment(file="x.bin", mime="application/octet-stream", name="x.bin",
                     size=len(PLAINTEXT), sha256=sha)
    note.attachments.append(att)
    ap = vault.attachment_access_path(note, att)
    assert ap == bp, "без шифрования access_path должен указывать на сам blob (без копий)"
    print("OK без шифрования: blob plaintext, access_path без копирования")


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
        from qtnotes.storage import vault
        vault.wipe_blob_cache()
        session.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    for test in (run_blob_encrypted_lowlevel, run_ensure_blobs_and_access,
                 run_plaintext_default):
        _isolated(test)
    print("\nВСЕ ТЕСТЫ ШИФРОВАНИЯ ВЛОЖЕНИЙ ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
