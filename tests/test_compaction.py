"""B1 (раунд-3): компакция журнала. По сущности остаётся op-победитель (LWW/tombstone),
доминируемые удаляются; vv (clock) и tombstones сохранны → сходимость не ломается.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_compaction.py
"""

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _put(eid, modified, dev, lam):
    return {"op_id": f"{dev}:{lam}", "device_id": dev, "lamport": lam,
            "wall": modified, "kind": "note.put", "entity_id": eid,
            "payload": {"id": eid, "modified": modified, "plaintext": modified}}


def _del(eid, wall, dev, lam):
    return {"op_id": f"{dev}:{lam}", "device_id": dev, "lamport": lam, "wall": wall,
            "kind": "note.del", "entity_id": eid, "payload": None}


def main():
    from qtnotes import config
    from qtnotes.storage import index
    from qtnotes.sync import oplog

    base = tempfile.mkdtemp(prefix="qtnotes-compact-")
    cfg, vlt = os.path.join(base, "cfg"), os.path.join(base, "vault")
    os.makedirs(cfg); os.makedirs(vlt)
    os.environ["XDG_CONFIG_HOME"] = cfg
    os.environ["QTNOTES_VAULT"] = vlt
    oplog.reset_for_tests(); index.reset_for_tests()

    try:
        config.set_sync_enabled(True)

        # --- 1) повторные правки одной заметки → остаётся последняя ---
        T = ["2026-01-01T00:00:0%d.000000+00:00" % i for i in range(1, 6)]
        for i, t in enumerate(T):
            assert oplog.record_remote(_put("X", t, "devA", 100 + i))
        # вторая сущность — одна правка (не трогается)
        assert oplog.record_remote(_put("Y", T[0], "devB", 50))

        assert len(oplog.all_ops()) == 6
        vv_before = oplog.version_vector()

        removed = oplog.compact()
        assert removed == 4, f"ожидали удалить 4 устаревших put X, удалили {removed}"
        ops = oplog.all_ops()
        assert len(ops) == 2, f"должно остаться по победителю на X и Y: {ops}"
        winners = {o["entity_id"]: o for o in ops}
        assert winners["X"]["payload"]["modified"] == T[-1], "победитель X — самая свежая правка"
        assert winners["Y"]["payload"]["modified"] == T[0]
        # vv НЕ изменился — сходимость сохранна
        assert oplog.version_vector() == vv_before, "compact не должен менять version vector"
        # свежий пир (пустой vv) получает ровно победителей
        fresh = oplog.ops_since({})
        assert {o["entity_id"] for o in fresh} == {"X", "Y"}
        assert len(fresh) == 2
        print("OK: повторные put схлопнуты до последнего; vv сохранён; свежий пир видит победителей")

        # идемпотентность
        assert oplog.compact() == 0, "повторная компакция — no-op"
        print("OK: компакция идемпотентна")

        # --- 2) удаление побеждает старые put → остаётся только del ---
        for i in range(3):
            assert oplog.record_remote(_put("Z", T[i], "devA", 200 + i))
        assert oplog.record_remote(_del("Z", T[4], "devA", 210))  # удаление новее всех put
        assert oplog.compact() == 3, "три устаревших put Z должны схлопнуться"
        zops = [o for o in oplog.all_ops() if o["entity_id"] == "Z"]
        assert len(zops) == 1 and zops[0]["kind"] == "note.del", "остаётся только удаление Z"
        print("OK: удаление поглощает старые put (остаётся del)")

        # --- 3) воскрешение (put новее удаления) → НЕ компактим (консервативно) ---
        assert oplog.record_remote(_del("W", T[1], "devA", 300))
        assert oplog.record_remote(_put("W", T[3], "devB", 305))  # put новее удаления
        before = len([o for o in oplog.all_ops() if o["entity_id"] == "W"])
        oplog.compact()
        after = len([o for o in oplog.all_ops() if o["entity_id"] == "W"])
        assert before == after == 2, "воскрешающую смесь не компактим (оба op на месте)"
        print("OK: воскрешение (put новее del) не компактится — консервативно")

        print("ALL COMPACTION (B1) TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
