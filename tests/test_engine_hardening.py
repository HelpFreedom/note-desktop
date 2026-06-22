"""E1+E2 (раунд-3, десктоп):
 E1 — сбой TPM при записи keyring НЕ срезает mac молча: _write ретраит и при стойком
      сбое пробрасывает (иначе keyring записался бы без mac, и _verify ему доверял бы вечно).
 E2 — серия mDNS-колбэков, пока connect ещё в полёте, планирует РОВНО один дозвон
      (резерв слота _connecting + кулдаун) — без реконнект-шторма.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_engine_hardening.py
"""

import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_e1_mac_write_fails_loud():
    from qtnotes import config  # noqa: F401
    from qtnotes.crypto import keyvault as KV
    from qtnotes.crypto import unlock
    from qtnotes.crypto.hwbackend import SoftwareHardwareKey

    base = tempfile.mkdtemp(prefix="qtnotes-e1-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "cfg")
    os.environ["QTNOTES_VAULT"] = os.path.join(base, "vault")
    os.makedirs(os.environ["XDG_CONFIG_HOME"]); os.makedirs(os.environ["QTNOTES_VAULT"])
    try:
        good = SoftwareHardwareKey.generate()
        state, _ = KV.setup("13579", good)
        stored = unlock._Stored(state=state, nv_baseline=0, backend="software")

        # рабочий backend → mac записывается, файл валиден
        unlock._write(stored, good)
        assert unlock._read().mac is not None, "mac должен записаться рабочим backend"

        # «фланки» backend, который всегда падает на mac → _write должен БРОСИТЬ,
        # а не записать keyring без mac
        class FlakyBackend:
            def mac(self, salt, data):
                raise RuntimeError("tpm busy (RC_RETRY)")
        raised = False
        try:
            unlock._write(stored, FlakyBackend())
        except RuntimeError:
            raised = True
        assert raised, "стойкий сбой mac должен пробрасываться, а не срезать защиту молча"
        # на диске остался ПРЕДЫДУЩИЙ валидный файл (с mac) — не перезаписан без mac
        assert unlock._read().mac is not None, "битая запись не должна оставить файл без mac"
        print("OK E1: сбой TPM при записи keyring пробрасывается, mac не срезается")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_e2_reconnect_guard():
    from qtnotes.sync import engine as engine_mod
    from qtnotes.sync import peers as peers_mod

    me = types.SimpleNamespace(device_id="aaaa1111", name="me")
    peer = peers_mod.Peer("ffff9999", "B", "CERTPEM", "now")
    eng = engine_mod.SyncEngine(me, None, get_peers=lambda: [peer])
    eng._loop = object()  # не None → путь планирования активен

    scheduled = []
    orig = engine_mod.asyncio.run_coroutine_threadsafe

    def fake_schedule(coro, loop):
        coro.close()  # не оставляем «never awaited»
        scheduled.append(1)
        return None
    engine_mod.asyncio.run_coroutine_threadsafe = fake_schedule
    try:
        found = types.SimpleNamespace(device_id="ffff9999", host="127.0.0.1", port=1234)
        for _ in range(6):  # серия mDNS-анонсов, пока сессии ещё нет
            eng._on_peer_found(found)
        assert len(scheduled) == 1, f"ожидался ровно один дозвон, было {len(scheduled)}"
        assert "ffff9999" in eng._connecting, "слот должен быть зарезервирован"
        print("OK E2: серия mDNS-колбэков → один дозвон (нет реконнект-шторма)")

        # после завершения попытки слот освобождается, кулдаун ещё держит
        eng._connecting.discard("ffff9999")
        eng._on_peer_found(found)
        assert len(scheduled) == 1, "кулдаун должен подавить немедленный повтор"
        print("OK E2: кулдаун подавляет немедленный повторный дозвон")
    finally:
        engine_mod.asyncio.run_coroutine_threadsafe = orig


def main():
    test_e1_mac_write_fails_loud()
    test_e2_reconnect_guard()
    print("ALL ENGINE-HARDENING (E1+E2) TESTS PASSED")


if __name__ == "__main__":
    main()
