"""P12: фоновая перешифровка (QThread) не блокирует UI и корректно шифрует данные.

Проверяем именно потоковую обвязку + результат через сигнал (software-бэкенд, без TPM/NV,
чтобы НЕ трогать реальный аппаратный счётчик).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_migration_worker.py
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
base = tempfile.mkdtemp(prefix="qtnotes-p12-")
os.environ["QTNOTES_VAULT"] = os.path.join(base, "vault")
os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(base, "data")
os.environ["XDG_RUNTIME_DIR"] = os.path.join(base, "run")
os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from PySide6.QtCore import QEventLoop
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from qtnotes import config
    from qtnotes.crypto import hwbackend
    from qtnotes.storage import crypto_fs, vault
    from qtnotes.storage.models import Note
    from qtnotes.ui.settings_dialog import _EncryptMigrationWorker

    # подготовить plaintext-данные
    f = vault.create_folder("П")
    n = Note.create_text(f.id, "<p>секрет</p>", "секрет")
    vault.save_note(n)
    note_path = config.folders_dir() / f.id / "notes" / f"{n.id}.json"
    assert not crypto_fs.is_encrypted_file(note_path), "до миграции должен быть plaintext"

    backend = hwbackend.SoftwareHardwareKey.generate()
    worker = _EncryptMigrationWorker("12345", backend)
    out = {}
    worker.done.connect(lambda stats, backup: out.update(stats=stats, backup=backup))
    worker.failed.connect(lambda msg: out.update(error=msg))
    loop = QEventLoop()
    worker.finished.connect(loop.quit)
    worker.start()
    loop.exec()
    worker.wait()

    assert "error" not in out, f"воркер упал: {out.get('error')}"
    assert "stats" in out, "сигнал done не пришёл"
    assert crypto_fs.is_encrypted_file(note_path), "после миграции заметка НЕ зашифрована"
    # читается обратно через крипто-слой
    notes = vault.list_notes(f.id)
    assert len(notes) == 1 and notes[0].plaintext == "секрет", "round-trip после шифрования"
    print(f"OK: фоновая миграция через QThread зашифровала данные (stats={out['stats']})")
    print("ALL P12 TESTS PASSED")


if __name__ == "__main__":
    main()
