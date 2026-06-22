"""P10: приватный ключ устройства шифруется at-rest под TPM, расшифровка — в tmpfs.

- свежая личность: на диске device_key.pem ЗАШИФРОВАН (магия), key_path указывает в
  tmpfs на расшифрованный PEM; device_id стабилен при перезагрузке;
- legacy-plaintext ключ мигрирует в зашифрованный (с сохранением device_id);
- без TPM поведение прежнее (plaintext) — здесь не проверяем (TPM есть).

Запуск: .venv/bin/python tests/test_identity_encryption.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env():
    base = tempfile.mkdtemp(prefix="qtnotes-p10-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "config")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(base, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    return base


def main():
    from pathlib import Path
    _env()
    from qtnotes.sync import identity

    if not identity._tpm_available():
        print("SKIP: TPM недоступен — P10 шифрование пропущено")
        return

    base = Path(tempfile.mkdtemp(prefix="qtnotes-dev-"))

    # 1) свежая личность под TPM
    d1 = base / "dev1"
    idn = identity.load_or_create(d1, "DevA")
    disk_key = (d1 / "device_key.pem").read_bytes()
    assert disk_key.startswith(identity._KEY_MAGIC), "ключ на диске НЕ зашифрован"
    assert idn.key_path != (d1 / "device_key.pem"), "key_path должен указывать в tmpfs"
    assert idn.key_path.exists(), "tmpfs-ключ не материализован"
    assert idn.key_pem.startswith(b"-----BEGIN"), "key_pem должен быть расшифрованным PEM"
    assert idn.key_path.read_bytes().startswith(b"-----BEGIN"), "tmpfs хранит не plaintext"
    print("OK: свежий ключ зашифрован на диске, расшифрован в tmpfs")

    # 2) стабильность device_id при перезагрузке
    idn2 = identity.load_or_create(d1, "DevA")
    assert idn2.device_id == idn.device_id, "device_id изменился при перезагрузке"
    assert (d1 / "device_key.pem").read_bytes().startswith(identity._KEY_MAGIC)
    print("OK: device_id стабилен, ключ остаётся зашифрованным")

    # 3) миграция legacy-plaintext → зашифрованный
    d2 = base / "dev2"
    d2.mkdir(parents=True)
    key_pem, cert_pem = identity._generate("DevB")
    (d2 / "device_key.pem").write_bytes(key_pem)       # legacy plaintext
    (d2 / "device_cert.pem").write_bytes(cert_pem)
    before_id = identity.device_id_from_cert_pem(cert_pem)
    migrated = identity.load_or_create(d2, "DevB")
    assert migrated.device_id == before_id, "device_id изменился при миграции (cert тот же!)"
    assert (d2 / "device_key.pem").read_bytes().startswith(identity._KEY_MAGIC), \
        "legacy-ключ не зашифрован после миграции"
    assert migrated.key_path.exists() and migrated.key_path.read_bytes().startswith(b"-----BEGIN")
    print("OK: legacy-plaintext мигрирован в зашифрованный, device_id сохранён")
    print("ALL P10 TESTS PASSED")


if __name__ == "__main__":
    main()
