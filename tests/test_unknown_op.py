"""A2 (раунд-3): op неизвестного kind НЕ записывается (vv не двигается) → переиграется
после апгрейда схемы, а не теряется молча, помеченной «видели». Форвард-совместимость.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_unknown_op.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

T = "2026-01-01T00:00:00.000000+00:00"


def _op(kind, eid, payload, dev, lam):
    return {"op_id": f"{dev}:{lam}", "device_id": dev, "lamport": lam, "wall": T,
            "kind": kind, "entity_id": eid, "payload": payload}


def main():
    from qtnotes import config
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import apply, oplog, store as store_mod

    base = tempfile.mkdtemp(prefix="qtnotes-unknownop-")
    cfg, vlt = os.path.join(base, "cfg"), os.path.join(base, "vault")
    os.makedirs(cfg); os.makedirs(vlt)
    os.environ["XDG_CONFIG_HOME"] = cfg
    os.environ["QTNOTES_VAULT"] = vlt
    oplog.reset_for_tests(); index.reset_for_tests()

    try:
        config.set_sync_enabled(True)
        store = store_mod.GlobalStore()
        f = vault.create_folder("F")
        n1 = Note.create_text(f.id, "<p>a</p>", "a")

        good = _op("note.put", n1.id, n1.as_dict(), "devA", 10)
        # op новой версии: kind, которого этот клиент ещё не знает
        future = _op("note.archive", n1.id, {"archived": True}, "devB", 11)

        assert store.record_and_apply(good) is True

        raised = False
        try:
            store.record_and_apply(future)
        except Exception:  # noqa: BLE001
            raised = True
        assert raised, "неизвестный kind должен бросать (а не молча игнорироваться)"

        # КЛЮЧЕВОЕ: op неизвестного kind НЕ записана → vv не двигается → переиграется
        assert not oplog.has_op("devB:11"), "op неизвестного kind не должна записываться"
        vv = oplog.version_vector()
        assert "devB" not in vv, f"vv не должен покрывать устройство непонятой op: {vv}"
        print("OK: op неизвестного kind бросает и не записывается (vv не сдвинут)")

        # put без payload — тоже неполная → бросок, не запись
        bad = _op("note.put", "x", None, "devC", 12)
        try:
            apply.apply_op(bad); assert False, "put без payload должен бросать"
        except ValueError:
            pass
        print("OK: put без payload бросает ValueError")

        print("ALL UNKNOWN-OP (A2) TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
