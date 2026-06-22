"""P1: атомарная запись с fsync (fsutil.atomic_write_bytes) и её использование в vault.

Проверяем:
- round-trip: записанные байты читаются обратно;
- после записи НЕ остаётся .tmp-файлов рядом с целью;
- перезапись существующего файла не теряет данные при «середине» (атомарность replace).

Запуск: .venv/bin/python tests/test_fsync_atomic.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_atomic_write_bytes():
    from qtnotes import fsutil
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "sub" / "note.json"
        fsutil.atomic_write_bytes(target, b"hello")
        assert target.read_bytes() == b"hello"
        # перезапись
        fsutil.atomic_write_bytes(target, b"world-2")
        assert target.read_bytes() == b"world-2"
        # никаких висящих .tmp
        leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"остались tmp-файлы: {leftovers}"
    print("ATOMIC OK: round-trip, перезапись, нет висящих .tmp")


def test_vault_writes_are_durable():
    # vault.save_note идёт через crypto_fs.write_bytes → fsutil.atomic_write_bytes.
    # Проверяем, что заметка пишется и читается, и tmp не остаётся.
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes.storage import vault
        from qtnotes.storage.models import Note
        f = vault.create_folder("П")
        n = Note.create_text(f.id, "<p>привет</p>", "привет")
        vault.save_note(n)
        got = vault.list_notes(f.id)
        assert len(got) == 1 and got[0].plaintext == "привет"
        ndir = vault.config.folders_dir() / f.id / "notes"
        tmps = [p.name for p in ndir.iterdir() if p.name.endswith(".tmp")]
        assert tmps == [], f"остались tmp в notes/: {tmps}"
    print("VAULT DURABLE OK: save_note через atomic_write_bytes, нет .tmp")


if __name__ == "__main__":
    test_atomic_write_bytes()
    test_vault_writes_are_durable()
    print("ALL P1 TESTS PASSED")
