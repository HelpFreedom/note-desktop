"""A1 (раунд-3): после ops-батча получатель шлёт свежий `have` ТОЛЬКО если что-то
реально применил (changed). Это даёт отправителю актуальный vv и заставляет
пере-предложить пропущенное (пир залочился и т.п.). При полностью пропущенном батче
have НЕ шлётся → нет бесконечного цикла на «ядовитой»/неприменимой op.

Драйвим Session._dispatch со стаб-стором и перехватом _send (без сети).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_rehave.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class StubStore:
    def __init__(self):
        self.vv = {}
        self.applied = []

    def version_vector(self):
        return dict(self.vv)

    def record_and_apply(self, op):
        if op.get("fail"):
            raise ValueError("apply boom (пир залочился)")
        self.applied.append(op["op_id"])
        self.vv[op["device_id"]] = op["lamport"]
        return True

    def missing_blob_hashes(self, op):
        return []


class FakeEngine:
    def __init__(self, store):
        self.store = store
        self.identity = None
        self.changed_calls = 0

    def _changed(self):
        self.changed_calls += 1


async def main():
    from qtnotes.sync import engine as E

    store = StubStore()
    sess = E.Session(FakeEngine(store), None, None, "peer")
    sent = []

    async def cap(obj):
        sent.append(obj)
    sess._send = cap

    # 1) батч, где op не применилась (apply бросил) → changed=False → have НЕ шлём
    await sess._dispatch({"type": "ops", "ops": [
        {"op_id": "d:1", "device_id": "d", "lamport": 1, "fail": True}]})
    assert not any(m["type"] == "have" for m in sent), \
        "при полностью пропущенном батче have слать нельзя (риск зацикливания)"
    assert store.applied == [], "пропущенная op не должна примениться"
    print("OK: полностью пропущенный батч → have не отправлен (нет цикла)")

    # 2) батч с применимой op → changed=True → have со свежим vv
    sent.clear()
    await sess._dispatch({"type": "ops", "ops": [
        {"op_id": "d:2", "device_id": "d", "lamport": 2}]})
    haves = [m for m in sent if m["type"] == "have"]
    assert len(haves) == 1, f"ожидался ровно один have после изменившего батча: {sent}"
    assert haves[0]["vv"] == {"d": 2}, f"have должен нести актуальный vv: {haves[0]}"
    print("OK: изменивший батч → ровно один have с актуальным vv")

    # 3) смешанный батч (одна ок, одна падает) → changed=True → have с vv только по ок
    sent.clear()
    await sess._dispatch({"type": "ops", "ops": [
        {"op_id": "e:5", "device_id": "e", "lamport": 5},
        {"op_id": "f:9", "device_id": "f", "lamport": 9, "fail": True}]})
    haves = [m for m in sent if m["type"] == "have"]
    assert len(haves) == 1 and haves[0]["vv"].get("e") == 5 and "f" not in haves[0]["vv"], \
        f"have должен покрывать применённое и НЕ покрывать пропущенное: {haves}"
    print("OK: смешанный батч → have покрывает только применённое (пропущенное переиграется)")

    print("ALL RE-HAVE (A1) TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
