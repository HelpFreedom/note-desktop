"""P3: сборка мусора блобов (mark-and-sweep) с возрастным барьером и защитой.

Проверяем:
- осиротевший blob (заметка удалена) собирается; blob, на который ссылается другая
  заметка, остаётся;
- возрастной барьер: свежий blob не удаляется, пока младше min_age;
- защита: при включённом шифровании без разблокировки GC ничего не удаляет.

Запуск: .venv/bin/python tests/test_gc_blobs.py
"""

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _mk_note_with_blob(vault, folder_id, data: bytes):
    from qtnotes.storage.models import Attachment, Note
    sha = hashlib.sha256(data).hexdigest()
    vault.write_blob(sha, data)
    n = Note.create_text(folder_id, "<p>x</p>", "x")
    n.attachments.append(Attachment(file="a.bin", sha256=sha))
    vault.save_note(n)
    return n, sha


def test_gc_collects_orphans_keeps_referenced():
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes.storage import vault
        f = vault.create_folder("П")
        n1, sha1 = _mk_note_with_blob(vault, f.id, b"orphan-data")
        n2, sha2 = _mk_note_with_blob(vault, f.id, b"kept-data")
        assert vault.has_blob(sha1) and vault.has_blob(sha2)

        # удаляем n1 → blob sha1 осиротел; min_age=0, чтобы не ждать
        vault.delete_note(n1)
        removed = vault.gc_blobs(min_age_seconds=0)
        assert removed == 1, f"ожидали удалить 1 блоб, удалили {removed}"
        assert not vault.has_blob(sha1), "осиротевший blob не удалён"
        assert vault.has_blob(sha2), "blob, на который ссылается n2, удалён по ошибке!"
    print("GC OK: сирота собрана, ссылочный blob сохранён")


def test_age_barrier():
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes.storage import vault
        vault.create_folder("П")  # есть папки, но блоб ничей
        vault.write_blob(hashlib.sha256(b"fresh").hexdigest(), b"fresh")
        # большой барьер → свежий blob не трогаем
        assert vault.gc_blobs(min_age_seconds=3600) == 0, "свежий blob удалён вопреки барьеру"
    print("GC AGE OK: свежий blob защищён возрастным барьером")


def test_locked_encryption_no_delete():
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes import config
        from qtnotes.storage import vault
        # blob на диске, шифрование «включено», но не разблокировано
        sha = hashlib.sha256(b"secret").hexdigest()
        vault.write_blob(sha, b"secret")
        config.set_encryption_enabled(True)
        try:
            removed = vault.gc_blobs(min_age_seconds=0)
            assert removed == 0, "GC удалил при заблокированном шифровании!"
            assert vault.has_blob(sha)
        finally:
            config.set_encryption_enabled(False)
    print("GC LOCK OK: при заблокированном шифровании GC воздерживается")


if __name__ == "__main__":
    test_gc_collects_orphans_keeps_referenced()
    test_age_barrier()
    test_locked_encryption_no_delete()
    print("ALL P3 TESTS PASSED")
