"""Регрессия: legacy keyring (без поля kdf, MAC старого формата) НЕ должен давать
ложную блокировку после добавления M1 (scrypt). Воспроизводит баг «Слишком много
повреждений» до ввода ПИНа.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_keyring_kdf_compat.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    base = tempfile.mkdtemp(prefix="qtnotes-kdfcompat-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "cfg")
    os.environ["QTNOTES_VAULT"] = os.path.join(base, "vault")
    os.makedirs(os.environ["XDG_CONFIG_HOME"]); os.makedirs(os.environ["QTNOTES_VAULT"])

    from qtnotes import config
    from qtnotes.crypto import keyvault as KV
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import unlock
    from qtnotes.crypto.hwbackend import SoftwareHardwareKey

    try:
        backend = SoftwareHardwareKey.generate()

        # --- собрать НАСТОЯЩИЙ legacy keyring (как до M1): MK обёрнут БЕЗ scrypt, kdf=None ---
        salt_wrap, salt_duress = P.random_bytes(16), P.random_bytes(16)
        mk = P.random_bytes(32)
        wrapped = P.seal(KV._wrap_key(backend, salt_wrap, "13579", None), mk)
        tag = KV._duress_tag(backend, salt_duress, "97531", None)
        state = KV.KeyringState(version=1, salt_wrap=salt_wrap, salt_duress=salt_duress,
                                wrapped_mk=wrapped, duress_tag=tag, kdf=None)

        # MAC старого формата: каноника БЕЗ ключа kdf (старый to_dict его не имел)
        kr = state.to_dict(); kr.pop("kdf", None)
        canonical = json.dumps({"keyring": kr, "nv_baseline": 0, "backend": "software"},
                               sort_keys=True, separators=(",", ":"))
        legacy_mac = backend.mac(unlock._MAC_SALT, canonical).hex()
        payload = {"keyring": kr, "nv_baseline": 0, "backend": "software", "mac": legacy_mac}
        kp = unlock.keyring_path()
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))

        # --- НОВЫЙ код читает legacy keyring: НЕ должно быть ложной блокировки ---
        rem = unlock.remaining_lockout(backend)
        assert rem == 0, f"❌ РЕГРЕССИЯ: ложная блокировка {rem}с (24ч ≈ {rem//60}мин)"
        stored = unlock._read()
        assert unlock._verify(stored, backend) is True, "legacy MAC ложно не сошёлся"
        print("OK: legacy keyring читается без ложной блокировки (баг устранён)")

        # --- вход проходит и апгрейдит keyring на scrypt, MAC пересчитан и валиден ---
        res = unlock.try_unlock("13579", backend)
        assert res.status == KV.UnlockStatus.OK, f"вход не прошёл: {res.status}"
        assert res.master_key == mk, "MK не совпал"
        stored2 = unlock._read()
        assert stored2.state.kdf is not None, "keyring должен апгрейдиться на scrypt"
        assert unlock._verify(stored2, backend) is True, "после апгрейда MAC должен сходиться"
        assert unlock.remaining_lockout(backend) == 0
        print("OK: вход успешен, keyring апгрейдился на scrypt, MAC валиден")

        # --- свежий keyring (scrypt с самого начала) — MAC тоже валиден ---
        shutil.rmtree(os.environ["XDG_CONFIG_HOME"]); os.makedirs(os.environ["XDG_CONFIG_HOME"])
        config.set_encryption_enabled(False)
        unlock.setup_pin("24680", backend)
        s3 = unlock._read()
        assert s3.state.kdf is not None and unlock._verify(s3, backend) is True
        assert unlock.remaining_lockout(backend) == 0
        print("OK: свежий scrypt-keyring — MAC валиден, без блокировки")

        print("ALL KEYRING KDF-COMPAT TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
