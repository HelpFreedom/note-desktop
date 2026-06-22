"""Конформанс СХОДИМОСТИ (keystone сетки). Проверяет ДВЕ вещи, которые не покрывал
прежний golden (он был только про крипто-формат и сериализацию одного объекта):

  1) НЕЗАВИСИМОСТЬ ОТ ПОРЯДКА: набор конкурентных операций, доставленный в разном
     порядке (модель двух устройств), обязан сходиться к ОДНОМУ финальному состоянию.
     Реальный синк доставляет op'ы пачками во времени — op с меньшим lamport может
     прийти ПОСЛЕ уже применённого op с большим lamport (разные устройства). Поэтому
     порядок применения по факту произвольный, и сходимость обязана это переживать.

  2) Python == Dart: обе реализации на одних и тех же векторах дают одно состояние.

Вектора пишутся в tests/golden/convergence_vectors.json и читаются Dart-драйвером
(mobile/test/convergence_conformance_test.dart), как в test_golden_vectors.py.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_convergence_conformance.py
"""

import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dart-сторона опциональна (запускается, если в PATH есть `dart` и рядом лежит проект
# qtnotes-mobile). Иначе проверяется только Python-сторона сходимости.
_DART = shutil.which("dart")

# Метки времени (микросекундный ISO8601 в UTC, как now_iso). Лексикограф. порядок = хронолог.
T1 = "2026-01-01T00:00:01.000000+00:00"
T2 = "2026-01-01T00:00:02.000000+00:00"
T3 = "2026-01-01T00:00:03.000000+00:00"
T4 = "2026-01-01T00:00:04.000000+00:00"
TE = "2026-06-01T00:00:00.000000+00:00"   # равный wall — для тай-брейка
DA = "a" * 16
DB = "b" * 16
DZ = "z" * 16
CREATED = "2026-01-01T00:00:00.000000+00:00"


def _note_payload(eid, plaintext, modified):
    return {"id": eid, "folder_id": "F", "kind": "text",
            "html": f"<p>{plaintext}</p>", "plaintext": plaintext, "caption_html": "",
            "attachments": [], "date_tag": None, "created": CREATED, "modified": modified}


def _put(eid, modified, dev, lam, plaintext):
    return {"op_id": f"{dev}:{lam}", "device_id": dev, "lamport": lam, "wall": modified,
            "kind": "note.put", "entity_id": eid, "payload": _note_payload(eid, plaintext, modified)}


def _del(eid, wall, dev, lam):
    return {"op_id": f"{dev}:{lam}", "device_id": dev, "lamport": lam, "wall": wall,
            "kind": "note.del", "entity_id": eid, "payload": None}


# Каждый сценарий: набор КОНКУРЕНТНЫХ операций. Финал не должен зависеть от порядка их
# доставки. `expected` — намеренная семантика (что ДОЛЖНО получиться по правилам проекта).
SCENARIOS = [
    {"name": "lww_concurrent_put",
     "ops": [_put("X", T2, DA, 1, "A"), _put("X", T4, DB, 1, "B")],
     "expected": {"X": {"present": True, "plaintext": "B"}}},   # новее по wall

    {"name": "del_beats_older_put",
     "ops": [_put("X", T2, DA, 1, "A"), _del("X", T4, DB, 2)],
     "expected": {"X": {"present": False}}},

    {"name": "put_after_del_stays_deleted",
     "ops": [_del("X", T2, DA, 1), _put("X", T4, DB, 2, "R")],
     "expected": {"X": {"present": False}}},   # Вариант A: удаление побеждает навсегда,
     # даже более новый put (по wall) НЕ воскрешает — независимо от порядка доставки

    {"name": "stale_put_suppressed_by_del",
     "ops": [_del("X", T4, DA, 2), _put("X", T2, DB, 1, "stale")],
     "expected": {"X": {"present": False}}},

    {"name": "tie_lamport_put_still_suppressed",
     "ops": [_del("X", TE, DA, 10), _put("X", TE, DB, 20, "hi")],
     "expected": {"X": {"present": False}}},   # Вариант A: даже больший lamport не воскрешает

    {"name": "tie_lamport_del_wins",
     "ops": [_del("X", TE, DA, 20), _put("X", TE, DB, 10, "x")],
     "expected": {"X": {"present": False}}},

    {"name": "tie_device_del_wins",
     "ops": [_del("X", TE, DZ, 10), _put("X", TE, DA, 10, "x")],
     "expected": {"X": {"present": False}}},   # равны wall+lamport → del по device (>=)

    {"name": "put_del_newerput_stays_deleted",
     "ops": [_put("X", T1, DA, 1, "v1"), _del("X", T2, DA, 2), _put("X", T3, DB, 3, "v3")],
     "expected": {"X": {"present": False}}},   # Вариант A: после удаления никакой put не воскрешает
]


def _read_state(vault, ids):
    out = {}
    for eid in ids:
        n = vault.find_note(eid)
        out[eid] = {"present": False} if n is None else {"present": True, "plaintext": n.plaintext}
    return out


def _apply_sequence(ops):
    """Применить op'ы к свежему хранилищу в данном порядке, вернуть финальное состояние."""
    from qtnotes import config
    from qtnotes.storage import index, vault
    from qtnotes.sync import apply, oplog

    base = tempfile.mkdtemp(prefix="qtnotes-conv-")
    cfg, vlt = os.path.join(base, "cfg"), os.path.join(base, "vault")
    os.makedirs(cfg); os.makedirs(vlt)
    os.environ["XDG_CONFIG_HOME"] = cfg
    os.environ["QTNOTES_VAULT"] = vlt
    oplog.reset_for_tests(); index.reset_for_tests()
    try:
        config.set_sync_enabled(True)
        # папка F должна существовать (заметки ссылаются на folder_id="F")
        from qtnotes.storage.models import Folder
        vault.save_folder(Folder(id="F", name="F"))
        for op in ops:
            if oplog.record_remote(op):
                apply.apply_op(op)
        ids = {op["entity_id"] for op in ops}
        return _read_state(vault, ids)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def run_python():
    """Вернуть (failures, results) где results[name][perm_str] = финальное состояние."""
    failures = []
    results = {}
    for sc in SCENARIOS:
        ops = sc["ops"]
        # все перестановки порядка доставки (op'ов мало — полный перебор)
        per = {}
        for perm in itertools.permutations(range(len(ops))):
            seq = [ops[i] for i in perm]
            per[",".join(map(str, perm))] = _apply_sequence(seq)
        results[sc["name"]] = per
        # 1) независимость от порядка: все перестановки дают одно состояние
        distinct = {json.dumps(s, sort_keys=True) for s in per.values()}
        if len(distinct) != 1:
            failures.append(
                f"[{sc['name']}] РАСХОЖДЕНИЕ ПО ПОРЯДКУ (py): {len(distinct)} разных финалов:\n"
                + "\n".join(f"    [{p}] → {json.dumps(per[p], sort_keys=True)}" for p in per))
            continue
        # 2) совпадение с намеренной семантикой
        got = next(iter(per.values()))
        if got != sc["expected"]:
            failures.append(f"[{sc['name']}] ожидали {sc['expected']}, получили {got}")
        else:
            print(f"OK (py): {sc['name']}")
    return failures, results


def write_vectors():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gdir = os.path.join(repo, "tests", "golden")
    os.makedirs(gdir, exist_ok=True)
    with open(os.path.join(gdir, "convergence_vectors.json"), "w", encoding="utf-8") as f:
        json.dump({"scenarios": SCENARIOS}, f, ensure_ascii=False, indent=1)
    return repo, gdir


def run_dart(repo, gdir, py_results):
    """Прогнать Dart-драйвер и сверить Python==Dart по каждому порядку доставки."""
    out_path = os.path.join(gdir, "conv_out.json")
    if os.path.exists(out_path):
        os.remove(out_path)
    # мобильный проект: подкаталог mobile/ (моно-лейаут) или соседний ../qtnotes-mobile
    mobile_dir = next(
        (d for d in (os.path.join(repo, "mobile"),
                     os.path.join(os.path.dirname(repo), "qtnotes-mobile"))
         if os.path.exists(os.path.join(d, "test", "convergence_conformance_test.dart"))),
        None)
    if _DART is None or mobile_dir is None:
        print("⚠ dart или проект qtnotes-mobile не найдены — пропускаю Dart-сторону "
              "(Python-сторона сходимости проверена)")
        return []
    env = dict(os.environ, CONV_DIR=gdir)
    r = subprocess.run([_DART, "test", "test/convergence_conformance_test.dart"],
                       cwd=mobile_dir, env=env,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
        return ["Dart-драйвер упал (см. вывод выше)"]
    if not os.path.exists(out_path):
        return ["Dart не записал conv_out.json"]
    with open(out_path, encoding="utf-8") as f:
        dart = json.load(f)

    failures = []
    for name, py_per in py_results.items():
        dart_per = dart.get(name)
        if dart_per is None:
            failures.append(f"[{name}] Dart не вернул результатов")
            continue
        # независимость от порядка ВНУТРИ Dart
        distinct = {json.dumps(s, sort_keys=True) for s in dart_per.values()}
        if len(distinct) != 1:
            failures.append(
                f"[{name}] РАСХОЖДЕНИЕ ПО ПОРЯДКУ (dart): {len(distinct)} разных финалов")
        # кросс-реализационное совпадение Python↔Dart по каждому порядку
        for perm, py_state in py_per.items():
            d_state = dart_per.get(perm)
            if json.dumps(py_state, sort_keys=True) != json.dumps(d_state, sort_keys=True):
                failures.append(
                    f"[{name}] Python≠Dart на порядке [{perm}]:\n"
                    f"    py   = {json.dumps(py_state, sort_keys=True)}\n"
                    f"    dart = {json.dumps(d_state, sort_keys=True)}")
    if not failures:
        print("OK: Python == Dart по всем сценариям и порядкам (кросс-конформанс)")
    return failures


def main():
    repo, gdir = write_vectors()
    failures, py_results = run_python()
    failures += run_dart(repo, gdir, py_results)
    print()
    if failures:
        print("❌ КОНФОРМАНС СХОДИМОСТИ НАРУШЕН:")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    print("ALL CONVERGENCE-CONFORMANCE TESTS PASSED")


if __name__ == "__main__":
    main()
