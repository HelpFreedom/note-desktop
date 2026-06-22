"""P4: при ошибке расшифровки blob НЕ отдаём шифртекст в UI.

attachment_access_path:
- валидный зашифрованный blob → путь к расшифрованному кэш-файлу с верным плейнтекстом;
- повреждённый blob (GCM не сходится) → None (а НЕ путь к шифртексту).

Запуск: .venv/bin/python tests/test_decrypt_failure.py
"""

import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env():
    tmp = tempfile.mkdtemp(prefix="qtnotes-p4-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)


def main():
    _env()
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import vault
    from qtnotes.storage.models import Attachment, Note

    config.set_encryption_enabled(True)
    session.set_master_key(P.random_bytes(32))

    data = b"secret-image-bytes" * 50
    sha = hashlib.sha256(data).hexdigest()
    vault.write_blob(sha, data)  # пишется зашифрованным

    note = Note.create_text("f1", "<p>x</p>", "x")
    att = Attachment(file="a.bin", name="a.bin", sha256=sha)

    # валидный blob → расшифрованный путь, плейнтекст совпадает
    path = vault.attachment_access_path(note, att)
    assert path is not None, "валидный blob дал None"
    assert path.read_bytes() == data, "расшифровка вернула не те байты"
    blob_file = vault.blob_path(sha)
    assert path != blob_file, "вернули путь к самому blob (возможно шифртекст)!"
    print("OK: валидный зашифрованный blob → расшифрованный кэш с верным плейнтекстом")

    # повреждаем blob (флипаем байт в шифртексте) → расшифровка обязана дать None
    raw = bytearray(blob_file.read_bytes())
    raw[-1] ^= 0xFF  # портим тег GCM
    blob_file.write_bytes(bytes(raw))
    # очистить возможный кэш расшифровки
    vault.wipe_blob_cache()
    path2 = vault.attachment_access_path(note, att)
    assert path2 is None, f"повреждённый blob НЕ дал None, вернул {path2}"
    print("OK: повреждённый blob → None (шифртекст в UI не уходит)")

    config.set_encryption_enabled(False)
    print("ALL P4 TESTS PASSED")


if __name__ == "__main__":
    main()
