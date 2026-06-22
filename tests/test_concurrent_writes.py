"""P2: гонки записи — уникальные tmp-имена + сериализация агрегатов.

Проверяем:
- 20 потоков пишут ОДИН файл через atomic_write_bytes → без исключений, итог валиден
  (один из записанных вариантов), нет висящих .tmp;
- N потоков добавляют события через vault.add_event → ни одно не потеряно (замок
  сериализует read-modify-write events.json).

Запуск: .venv/bin/python tests/test_concurrent_writes.py
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_same_file_concurrent():
    from pathlib import Path
    from qtnotes import fsutil
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "race.bin"
        errors = []
        contents = [f"variant-{i}".encode() for i in range(20)]

        def worker(data):
            try:
                for _ in range(25):
                    fsutil.atomic_write_bytes(target, data)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(c,)) for c in contents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"исключения при гонке: {errors[:3]}"
        assert target.read_bytes() in contents, "итоговый файл повреждён"
        leftovers = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"остались tmp: {leftovers}"
    print("RACE FILE OK: 20 потоков, один файл — без порчи и без висящих .tmp")


def test_events_no_lost_update():
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes.storage import vault
        n = 40
        errors = []

        def add(i):
            try:
                vault.add_event("2026-06-22", f"e{i}", "#ffffff")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"исключения: {errors[:3]}"
        got = len(vault.list_events())
        assert got == n, f"потеря обновлений: ожидали {n}, получили {got}"
    print(f"EVENTS OK: {n} конкурентных add_event — ни одно не потеряно")


if __name__ == "__main__":
    test_same_file_concurrent()
    test_events_no_lost_update()
    print("ALL P2 TESTS PASSED")
