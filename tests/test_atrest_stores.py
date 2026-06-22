"""Тесты шифрования at-rest для oplog и индекса (Ф2c).

- oplog: контент заметок (колонка payload) шифруется; метаданные остаются; round-trip;
  обратная совместимость с plaintext-payload; чужой ключ ломает расшифровку.
- индекс: при включённом шифровании уезжает в tmpfs (RAM), на диске vault его нет;
  поиск/folder_of работают; wipe_ephemeral удаляет кэш.

Изоляция обязательна (см. правило про реальный ~/Documents). Запуск:
    .venv/bin/python tests/test_atrest_stores.py
"""

import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_env():
    tmp = tempfile.mkdtemp(prefix="qtnotes-atrest-")
    os.environ["QTNOTES_VAULT"] = os.path.join(tmp, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(tmp, "data")
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(tmp, "run")
    os.makedirs(os.environ["XDG_RUNTIME_DIR"], exist_ok=True)
    return tmp


def _raw_payload(op_id: str) -> str:
    from qtnotes import config
    con = sqlite3.connect(str(config.sync_db_path()))
    try:
        row = con.execute("SELECT payload FROM ops WHERE op_id=?", (op_id,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def run_oplog_encrypted() -> None:
    """payload шифруется; метаданные на месте; round-trip; чужой ключ ломает."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.sync import oplog

    mk = P.random_bytes(32)
    config.set_encryption_enabled(True)
    session.set_master_key(mk)
    oplog.reset_for_tests()

    op_id = oplog.append_local(
        "note.put", "note-1", {"id": "note-1", "plaintext": "секрет про пароль 4242"})

    # на диске payload зашифрован, текста не видно; метаданные (kind/entity_id) есть
    raw = _raw_payload(op_id)
    assert raw.startswith("ENC1:"), raw[:20]
    assert "пароль" not in raw and "4242" not in raw
    con = sqlite3.connect(str(config.sync_db_path()))
    meta = con.execute("SELECT kind, entity_id FROM ops WHERE op_id=?", (op_id,)).fetchone()
    con.close()
    # P13: метаданные (kind/entity_id) тоже зашифрованы at-rest — образ sync.sqlite не
    # выдаёт тип операции и какую сущность тронули.
    assert meta[0].startswith("ENC1:") and "note.put" not in meta[0], meta[0][:20]
    assert meta[1].startswith("ENC1:") and "note-1" not in meta[1], meta[1][:20]

    # read-back расшифровывает корректно (и payload, и метаданные)
    ops = oplog.all_ops()
    assert ops[0]["kind"] == "note.put" and ops[0]["entity_id"] == "note-1"
    assert ops[0]["payload"]["plaintext"] == "секрет про пароль 4242"
    assert oplog.ops_since({})[0]["payload"]["id"] == "note-1"

    # чужой ключ → расшифровка падает
    from cryptography.exceptions import InvalidTag
    session.set_master_key(P.random_bytes(32))
    oplog.reset_for_tests()
    raised = False
    try:
        oplog.all_ops()
    except InvalidTag:
        raised = True
    assert raised, "чужой ключ должен ломать расшифровку payload"
    print("OK oplog: payload зашифрован, метаданные доступны, round-trip")


def run_oplog_backward_compat() -> None:
    """plaintext-payload (шифрование выкл) читается и при включённом шифровании."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.sync import oplog

    # OFF → payload в открытом виде
    config.set_encryption_enabled(False)
    session.lock()
    oplog.reset_for_tests()
    op_plain = oplog.append_local("folder.put", "f1", {"name": "Открытая папка"})
    raw = _raw_payload(op_plain)
    assert not raw.startswith("ENC1:")
    assert "Открытая папка" in raw  # plaintext JSON

    # теперь ON + ключ: дописываем зашифрованную op; читаются ОБЕ
    mk = P.random_bytes(32)
    config.set_encryption_enabled(True)
    session.set_master_key(mk)
    oplog.reset_for_tests()
    oplog.append_local("note.put", "n2", {"plaintext": "новый зашифрованный"})

    by_id = {o["entity_id"]: o for o in oplog.all_ops()}
    assert by_id["f1"]["payload"]["name"] == "Открытая папка"      # legacy plaintext
    assert by_id["n2"]["payload"]["plaintext"] == "новый зашифрованный"  # encrypted
    print("OK oplog: обратная совместимость (plaintext + encrypted вместе)")


def run_index_tmpfs() -> None:
    """При шифровании индекс в tmpfs; на диске vault его нет; поиск работает."""
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note

    config.set_encryption_enabled(True)
    session.set_master_key(P.random_bytes(32))
    index.reset_for_tests()

    ip = config.index_path()
    assert "qtnotes-index-" in str(ip), ip
    assert config.vault_dir().resolve() not in ip.resolve().parents

    f = vault.create_folder("Поиск", icon="letter")
    n = Note.create_text(f.id, "<p>квантовая криптография</p>", "квантовая криптография")
    vault.save_note(n)

    # на диске vault НЕТ plaintext-индекса
    assert not (config.vault_dir() / "index.sqlite").exists(), \
        "при шифровании index.sqlite не должен лежать в vault"

    # поиск и folder_of работают через tmpfs-индекс
    assert index.folder_of(n.id) == f.id
    rows = index.candidate_rows("квант", f.id)
    assert any("квантовая" in r["plaintext"] for r in rows)

    # wipe удаляет эфемерный кэш
    index.wipe_ephemeral()
    assert not ip.exists(), "wipe_ephemeral должен удалить tmpfs-индекс"
    print("OK index: tmpfs при шифровании, на диске vault чисто, поиск работает")


def run_index_plain_default() -> None:
    """Без шифрования индекс по-прежнему в vault/index.sqlite (поведение прежнее)."""
    from qtnotes import config
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note

    config.set_encryption_enabled(False)
    index.reset_for_tests()
    ip = config.index_path()
    assert ip == config.vault_dir() / "index.sqlite"

    f = vault.create_folder("Обычная", icon="letter")
    vault.save_note(Note.create_text(f.id, "<p>текст</p>", "текст"))
    assert ip.exists()  # индекс на диске vault, как раньше
    print("OK index: без шифрования — в vault (поведение прежнее)")


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
        session.lock()
        oplog.reset_for_tests()
        index.reset_for_tests()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    for test in (run_oplog_encrypted, run_oplog_backward_compat,
                 run_index_tmpfs, run_index_plain_default):
        _isolated(test)
    print("\nВСЕ ТЕСТЫ AT-REST ХРАНИЛИЩ (oplog+индекс) ПРОЙДЕНЫ")


if __name__ == "__main__":
    main()
