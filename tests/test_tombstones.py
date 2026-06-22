"""H4: tombstones против воскрешения удалённых заметок.

Правило (выбор пользователя, Вариант A 2026-06-22): УДАЛЕНИЕ ПОБЕЖДАЕТ НАВСЕГДА.
Любой tombstone подавляет ВСЕ последующие put для этой сущности, независимо от
времени/lamport/device. Это независимо от порядка доставки (раньше «воскрешение»
более новым put давало расходимость — см. test_convergence_conformance). Для конкурентных
put БЕЗ удаления действует обычный LWW по `modified` (см. apply._apply_note_put).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_tombstones.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

T_OLD = "2020-01-01T00:00:00.000000+00:00"
T_MID = "2023-01-01T00:00:00.000000+00:00"
T_NEW = "2026-01-01T00:00:00.000000+00:00"


def _del_op(entity_id, wall, lamport, dev):
    return {"op_id": f"{dev}:{lamport}", "device_id": dev, "lamport": lamport,
            "wall": wall, "kind": "note.del", "entity_id": entity_id, "payload": None}


def _put_op(note_dict, wall, lamport, dev):
    return {"op_id": f"{dev}:{lamport}", "device_id": dev, "lamport": lamport,
            "wall": wall, "kind": "note.put", "entity_id": note_dict["id"],
            "payload": dict(note_dict)}


def main():
    from qtnotes import config
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import apply, oplog

    base = tempfile.mkdtemp(prefix="qtnotes-tomb-")
    cfg, vlt = os.path.join(base, "cfg"), os.path.join(base, "vault")
    os.makedirs(cfg); os.makedirs(vlt)
    os.environ["XDG_CONFIG_HOME"] = cfg
    os.environ["QTNOTES_VAULT"] = vlt
    oplog.reset_for_tests(); index.reset_for_tests()

    try:
        config.set_sync_enabled(True)
        f = vault.create_folder("F")
        n = Note.create_text(f.id, "<p>v1</p>", "v1")
        n.modified = T_MID
        vault.save_note(n)
        nd = n.as_dict()
        assert vault.find_note(n.id) is not None

        # --- удаление новее (wall=T_NEW): заметка удалена, tombstone записан ---
        d = _del_op(n.id, T_NEW, 100, "devX")
        assert oplog.record_remote(d)
        apply.apply_op(d)
        assert vault.find_note(n.id) is None, "удаление не применилось"
        print("OK: удаление применилось, tombstone записан")

        # --- СТАЛЫЙ put (wall=T_OLD < удаление): НЕ воскрешает ---
        stale = _put_op(nd, T_OLD, 50, "devY")
        assert oplog.record_remote(stale)
        apply.apply_op(stale)
        assert vault.find_note(n.id) is None, "❌ ВОСКРЕШЕНИЕ: устаревший put воскресил заметку"
        print("OK: устаревший put подавлен tombstone'ом (антивоскрешение)")

        # --- Вариант A (2026-06-22): удаление побеждает НАВСЕГДА — даже более новый
        #     по времени put НЕ воскрешает (раньше воскрешал → расходимость, см.
        #     test_convergence_conformance). Удаление — финальное действие для id. ---
        newer = _put_op(nd, "2027-01-01T00:00:00.000000+00:00", 200, "devZ")
        assert oplog.record_remote(newer)
        apply.apply_op(newer)
        assert vault.find_note(n.id) is None, "после удаления put не должен воскрешать (Вариант A)"
        print("OK: даже более новый put не воскрешает (удаление побеждает навсегда)")

        # === тай-брейк по lamport при равном wall ===
        n2 = Note.create_text(f.id, "<p>x</p>", "x"); n2.modified = T_MID
        vault.save_note(n2)
        nd2 = n2.as_dict()
        d2 = _del_op(n2.id, T_MID, 10, "aaa")  # удаление wall=T_MID, lamport=10
        assert oplog.record_remote(d2); apply.apply_op(d2)
        assert vault.find_note(n2.id) is None

        # put равный wall, МЕНЬШИЙ lamport (5<10) → удаление новее → подавляется
        p_lo = _put_op(nd2, T_MID, 5, "bbb")
        assert oplog.record_remote(p_lo); apply.apply_op(p_lo)
        assert vault.find_note(n2.id) is None, "при равном wall меньший lamport проигрывает удалению"
        print("OK: тай-брейк по lamport — меньший проигрывает удалению")

        # Вариант A: даже больший lamport не воскрешает — удаление окончательно
        p_hi = _put_op(nd2, T_MID, 20, "ccc")
        assert oplog.record_remote(p_hi); apply.apply_op(p_hi)
        assert vault.find_note(n2.id) is None, "после удаления put не воскрешает даже с большим lamport (Вариант A)"
        print("OK: больший lamport тоже не воскрешает (удаление побеждает навсегда)")

        # === зашифрованный vault: tombstone через keyed-hash ekey (ветка P13) ===
        from qtnotes.crypto import primitives as P, session
        cfg2, vlt2 = os.path.join(base, "cfg2"), os.path.join(base, "vault2")
        os.makedirs(cfg2); os.makedirs(vlt2)
        os.environ["XDG_CONFIG_HOME"] = cfg2
        os.environ["QTNOTES_VAULT"] = vlt2
        oplog.reset_for_tests(); index.reset_for_tests()
        config.set_sync_enabled(True)
        config.set_encryption_enabled(True)
        session.set_master_key(P.random_bytes(32))

        fe = vault.create_folder("E")
        ne = Note.create_text(fe.id, "<p>secret</p>", "secret"); ne.modified = T_MID
        vault.save_note(ne)
        nde = ne.as_dict()
        de = _del_op(ne.id, T_NEW, 100, "devX")
        assert oplog.record_remote(de); apply.apply_op(de)
        assert vault.find_note(ne.id) is None
        # ekey — keyed-hash (не равен entity_id), но детерминирован → put находит tombstone
        assert oplog._tomb_ekey(ne.id) != ne.id, "под шифрованием ekey должен быть keyed-hash"
        stale_e = _put_op(nde, T_OLD, 50, "devY")
        assert oplog.record_remote(stale_e); apply.apply_op(stale_e)
        assert vault.find_note(ne.id) is None, "❌ воскрешение в зашифрованном vault"
        print("OK: tombstone работает в зашифрованном vault (keyed-hash ekey)")

        print("ALL TOMBSTONE TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
