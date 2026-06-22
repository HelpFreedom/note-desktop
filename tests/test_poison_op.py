"""H5: битая op изолируется — не рвёт сессию, не теряет соседние ops, не двигает vv.

Воспроизводим логику цикла обработки 'ops' в движке (try/except вокруг каждой op) и
проверяем порядок применить-потом-записать (store.record_and_apply).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_poison_op.py
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
    from qtnotes.sync import oplog, store as store_mod

    base = tempfile.mkdtemp(prefix="qtnotes-poison-")
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
        n2 = Note.create_text(f.id, "<p>b</p>", "b")

        good1 = _op("note.put", n1.id, n1.as_dict(), "devA", 100)
        poison = _op("note.put", "poison-id", {}, "devB", 101)  # from_dict({}) → KeyError
        good2 = _op("note.put", n2.id, n2.as_dict(), "devC", 102)

        # как в движке: try/except вокруг каждой op
        raised = []
        for o in [good1, poison, good2]:
            try:
                store.record_and_apply(o)
            except Exception as e:  # noqa: BLE001
                raised.append((o["op_id"], type(e).__name__))

        assert raised and raised[0][0] == "devB:101", f"ожидали бросок на битой op: {raised}"
        # соседние ops применились несмотря на битую посередине
        assert vault.find_note(n1.id) is not None, "good1 не применился"
        assert vault.find_note(n2.id) is not None, "good2 не применился (битая op заблокировала?)"
        # битая op НЕ записана → vv не сдвинут → придёт снова при следующем синке
        assert not oplog.has_op("devB:101"), "битая op не должна записываться"
        assert oplog.has_op("devA:100") and oplog.has_op("devC:102")
        vv = oplog.version_vector()
        assert "devB" not in vv, f"vv не должен содержать устройство битой op: {vv}"
        assert vv.get("devA") == 100 and vv.get("devC") == 102, vv
        print("OK: битая op изолирована, соседние применились, vv не сдвинут для битой")

        # идемпотентность: повторное применение good1 — no-op (уже записан)
        assert store.record_and_apply(good1) is False
        print("OK: повторная запись уже виденной op — no-op")
        print("ALL POISON-OP TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
