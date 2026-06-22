"""Тесты крипто-слоя хранилища (Ф2): прозрачное шифрование JSON-контента за флагом.

Проверяем:
- шифрование ВЫКЛ (по умолчанию) → файлы остаются обычным plaintext (поведение прежнее);
- шифрование ВКЛ + разблокировано → на диске зашифрованный файл с MAGIC, но vault
  читает данные корректно (round-trip);
- обратная совместимость: старый plaintext-файл читается и при включённом шифровании;
- отказ при заблокированном хранилище (нет ключа).

Изоляция обязательна: тест направлен в временный vault (см. 
~/Documents). Запуск:
    .venv/bin/python tests/test_storage_crypto.py
"""

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_env():
    """Изолированное окружение: свой vault + config. Возвращает (tmpdir, cleanup)."""
    tmp = tempfile.mkdtemp(prefix="qtnotes-crypto-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    return tmp


def run_plaintext_default() -> None:
    """Шифрование выключено → folder.json остаётся читаемым plaintext-JSON."""
    from qtnotes import config
    from qtnotes.storage import vault
    from qtnotes.storage.models import Note

    assert config.encryption_enabled() is False
    f = vault.create_folder("Личное", icon="letter")
    n = Note.create_text(f.id, "<p>привет</p>", "привет")
    vault.save_note(n)

    # файл на диске — валидный JSON в открытом виде
    from qtnotes.storage import crypto_fs
    fp = config.folders_dir() / f.id / "folder.json"
    assert not crypto_fs.is_encrypted_file(fp), "при выключенном шифровании файл должен быть plaintext"
    raw = fp.read_bytes()
    assert json.loads(raw.decode("utf-8"))["name"] == "Личное"

    # читается обычным путём
    assert any(x.id == f.id for x in vault.list_folders())
    assert vault.find_note(n.id).plaintext == "привет"
    print("OK plaintext по умолчанию (поведение прежнее)")


def run_encrypted_roundtrip() -> None:
    """Шифрование включено + разблокировано → файл зашифрован, но данные читаются."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import crypto_fs, vault
    from qtnotes.storage.models import Note

    config.set_encryption_enabled(True)
    session.set_master_key(P.random_bytes(32))

    f = vault.create_folder("Секреты", icon="letter")
    n = Note.create_text(f.id, "<p>пароль 42</p>", "пароль 42")
    vault.save_note(n)
    vault.add_event("2026-06-20", "День X", "#ff0000")
    vault.set_shared("theme", {"palette": {"accent": "#123456"}})

    # на диске — зашифровано, plaintext не виден
    fp = config.folders_dir() / f.id / "folder.json"
    np = config.folders_dir() / f.id / "notes" / f"{n.id}.json"
    ep = config.calendar_dir() / "events.json"
    sp = config.vault_dir() / "shared.json"
    for p in (fp, np, ep, sp):
        assert crypto_fs.is_encrypted_file(p), f"{p.name} должен быть зашифрован"
    assert b"\xd0" not in np.read_bytes()[: len(crypto_fs.MAGIC)]  # не сырой UTF-8
    assert b"\xd0\xbf\xd0\xb0\xd1\x80\xd0\xbe\xd0\xbb\xd1\x8c" not in np.read_bytes(), \
        "слово 'пароль' не должно лежать в открытом виде"

    # vault читает корректно
    assert vault.find_note(n.id).plaintext == "пароль 42"
    assert any(x.id == f.id for x in vault.list_folders())
    assert vault.list_events()[0].name == "День X"
    assert vault.get_shared("theme")["palette"]["accent"] == "#123456"

    # неверный ключ → данные не читаются (InvalidTag перехватывается как «нет данных»)
    session.set_master_key(P.random_bytes(32))
    from cryptography.exceptions import InvalidTag
    try:
        vault.list_folders()  # _read_json вернёт None при InvalidTag? нет — пробрасывается
        raised = False
    except InvalidTag:
        raised = True
    assert raised, "чужой ключ должен ломать расшифровку"

    config.set_encryption_enabled(False)
    session.lock()
    print("OK зашифрованный round-trip + on-disk проверка")


def run_backward_compat() -> None:
    """Старый plaintext-файл читается и когда шифрование включено."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import vault

    # создаём заметку в plaintext (шифрование выкл)
    config.set_encryption_enabled(False)
    f = vault.create_folder("Старое", icon="letter")
    from qtnotes.storage.models import Note
    n = Note.create_text(f.id, "<p>legacy</p>", "legacy")
    vault.save_note(n)

    # теперь включаем шифрование и разблокируем — старый файл всё ещё читается
    config.set_encryption_enabled(True)
    session.set_master_key(P.random_bytes(32))
    assert vault.find_note(n.id).plaintext == "legacy"

    config.set_encryption_enabled(False)
    session.lock()
    print("OK обратная совместимость (plaintext читается при вкл. шифровании)")


def run_locked_write_refused() -> None:
    """При включённом шифровании и заблокированном хранилище запись запрещена."""
    from qtnotes import config
    from qtnotes.crypto import session
    from qtnotes.storage import crypto_fs, vault
    from qtnotes.storage.models import Folder

    config.set_encryption_enabled(True)
    session.lock()  # ключа нет

    raised = False
    try:
        vault.save_folder(Folder.create(name="x", icon="letter", order=0))
    except crypto_fs.VaultLockedError:
        raised = True
    assert raised, "запись при заблокированном шифровании должна быть запрещена"

    config.set_encryption_enabled(False)
    print("OK отказ записи при заблокированном хранилище")


def _isolated(test) -> None:
    """Прогнать тест в собственном свежем vault (изоляция между тестами)."""
    from qtnotes.crypto import session
    tmp = _fresh_env()
    session.lock()  # сбросить ключ из предыдущего теста
    try:
        test()
    finally:
        session.lock()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    for test in (run_plaintext_default, run_encrypted_roundtrip,
                 run_backward_compat, run_locked_write_refused):
        _isolated(test)
    print("\nВСЕ ТЕСТЫ КРИПТО-СЛОЯ ХРАНИЛИЩА ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
