"""Воспроизведение жалобы: десктоп (шифрование ВКЛ) ↔ телефон (шифрование ВЫКЛ).

Симулируем обмен op'ами (то, что отдаёт ops_since — plaintext-словарь идёт по проводу).
Проверяем обе стороны и обе операции (put и del), особенно note.del десктоп→телефон.

Запуск: .venv/bin/python tests/test_sync_enc_propagation.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _become(vault_dir, cfg_dir, *, encrypted, mk=None):
    """Переключить процесс на «устройство»: env, config, session, кэши oplog/index."""
    from qtnotes import config
    from qtnotes.crypto import session
    from qtnotes.sync import oplog
    from qtnotes.storage import index
    os.environ["QTNOTES_VAULT"] = vault_dir
    os.environ["XDG_CONFIG_HOME"] = cfg_dir
    oplog.reset_for_tests()
    index.reset_for_tests()
    config.set_encryption_enabled(encrypted)
    config.set_sync_enabled(True)
    if encrypted:
        session.set_master_key(mk)
    else:
        session.lock()


def main():
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.sync import oplog, store as store_mod
    from qtnotes.storage import vault
    from qtnotes.storage.models import Note

    base = tempfile.mkdtemp(prefix="qtnotes-prop-")
    vaultA = os.path.join(base, "vA"); cfgA = os.path.join(base, "cA")
    vaultB = os.path.join(base, "vB"); cfgB = os.path.join(base, "cB")
    for d in (vaultA, cfgA, vaultB, cfgB):
        os.makedirs(d)
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(base, "run"); os.makedirs(os.environ["XDG_RUNTIME_DIR"])
    mkA = P.random_bytes(32)
    store = store_mod.GlobalStore()

    try:
        # === Устройство B (телефон, без шифрования): создаёт заметку ===
        _become(vaultB, cfgB, encrypted=False)
        fB = vault.create_folder("Папка")
        nB = Note.create_text(fB.id, "<p>с телефона</p>", "с телефона")
        vault.save_note(nB)
        b_ops = oplog.ops_since({})  # то, что B отправит по проводу
        assert any(o["kind"] == "note.put" and o["entity_id"] == nB.id for o in b_ops), \
            "B не породил note.put"
        print(f"B создал заметку, ops к отправке: {[o['kind'] for o in b_ops]}")

        # === Устройство A (десктоп, шифрование ВКЛ): принимает ops B ===
        _become(vaultA, cfgA, encrypted=True, mk=mkA)
        for op in b_ops:
            store.record_and_apply(op)   # как движок: record_remote(шифрует мету)+apply
        a_notes = vault.list_notes(fB.id)
        assert len(a_notes) == 1 and a_notes[0].id == nB.id, \
            f"A НЕ применил заметку телефона: {a_notes}"
        # и A читает свой oplog обратно (метаданные зашифрованы на диске, расшифровка в API)
        a_all = oplog.all_ops()
        assert any(o["kind"] == "note.put" and o["entity_id"] == nB.id for o in a_all), \
            "A не отдаёт принятый op обратно (расшифровка меты?)"
        print("OK: заметка телефона доехала до десктопа и применилась")

        # === A удаляет заметку → порождает note.del ===
        a_vv_seen = oplog.version_vector()  # vv A после приёма
        vault.delete_note(a_notes[0])
        del_ops = [o for o in oplog.ops_since(a_vv_seen) if o["kind"] == "note.del"]
        assert del_ops and del_ops[0]["entity_id"] == nB.id, \
            f"A не породил корректный note.del: {oplog.ops_since(a_vv_seen)}"
        # op, уходящий по проводу, должен быть ПЛЕЙНТЕКСТ (мета расшифрована)
        d = del_ops[0]
        assert d["kind"] == "note.del" and not d["entity_id"].startswith("ENC1:"), d
        a_to_send = oplog.ops_since({})  # всё, чем A поделится (полный набор)
        print(f"OK: A породил note.del (plaintext по проводу): entity={d['entity_id']}")

        # === B принимает удаление ===
        _become(vaultB, cfgB, encrypted=False)
        for op in a_to_send:
            store.record_and_apply(op)
        b_notes_after = vault.list_notes(fB.id)
        assert b_notes_after == [], f"❌ ВОСПРОИЗВЕДЕНО: B НЕ удалил заметку! осталось: {b_notes_after}"
        print("OK: удаление с десктопа применилось на телефоне")
        print("ALL PROPAGATION TESTS PASSED — баг НЕ воспроизводится на уровне op-логики")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
