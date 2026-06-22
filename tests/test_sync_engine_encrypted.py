"""Живой движок (реальный TLS) с АСИММЕТРИЕЙ шифрования: A (десктоп) зашифрован,
B (телефон) — нет. Воспроизводим: put A→B, затем DEL A→B (провалившийся случай).

Ловим в т.ч. «проглоченные» asyncio-исключения на потоке движка (push_all/ops_since).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_sync_engine_encrypted.py
"""

import asyncio
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ERRORS = []


def main():
    from qtnotes import config
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto import session
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note, new_id
    from qtnotes.sync import engine as engine_mod
    from qtnotes.sync import identity, oplog, peers, store

    base = tempfile.mkdtemp(prefix="qtnotes-engenc-")
    A = (os.path.join(base, "cA"), os.path.join(base, "vA"))
    B = (os.path.join(base, "cB"), os.path.join(base, "vB"))
    for d in (*A, *B, os.path.join(base, "run")):
        os.makedirs(d)
    os.environ["XDG_RUNTIME_DIR"] = os.path.join(base, "run")
    mkA = P.random_bytes(32)

    def enter(ctx):
        os.environ["XDG_CONFIG_HOME"], os.environ["QTNOTES_VAULT"] = ctx
        oplog.reset_for_tests()
        index.reset_for_tests()
        if ctx is A:
            config.set_encryption_enabled(True)
            session.set_master_key(mkA)
        else:
            config.set_encryption_enabled(False)
            session.lock()

    class CtxStore:
        def __init__(self, ctx):
            self.ctx = ctx
            self._g = store.GlobalStore()

        def _e(self):
            enter(self.ctx)

        def version_vector(self):
            self._e(); return self._g.version_vector()

        def ops_since(self, vv):
            self._e(); return self._g.ops_since(vv)

        def record_and_apply(self, op):
            self._e(); return self._g.record_and_apply(op)

        def missing_blob_hashes(self, op):
            self._e(); return self._g.missing_blob_hashes(op)

        def read_blob(self, h):
            self._e(); return self._g.read_blob(h)

        def write_blob(self, h, d):
            self._e(); return self._g.write_blob(h, d)

    # перехватываем «проглоченные» исключения задач asyncio
    def _trap(loop, ctx):
        _ERRORS.append(ctx.get("exception") or ctx.get("message"))

    try:
        enter(A)
        config.set_sync_enabled(True)
        idA = identity.ensure_identity()
        f = vault.create_folder("Синк")
        n1 = Note.create_text(f.id, "<p>секрет</p>", "секрет-заметка")
        vault.save_note(n1)
        n1_id = n1.id

        enter(B)
        config.set_sync_enabled(True)
        idB = identity.ensure_identity()

        peerA = peers.Peer(idA.device_id, "A", idA.cert_pem.decode(), "now")
        peerB = peers.Peer(idB.device_id, "B", idB.cert_pem.decode(), "now")
        engA = engine_mod.SyncEngine(idA, CtxStore(A), get_peers=lambda: [peerB])
        engB = engine_mod.SyncEngine(idB, CtxStore(B), get_peers=lambda: [peerA])

        def b_has(nid):
            enter(B); return vault.find_note(nid) is not None

        async def wait_until(pred, timeout):
            loop = asyncio.get_running_loop()
            end = loop.time() + timeout
            while loop.time() < end:
                if pred():
                    return True
                await asyncio.sleep(0.05)
            return pred()

        async def scenario():
            asyncio.get_running_loop().set_exception_handler(_trap)
            server = await engB.serve("127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            task = asyncio.create_task(engA.connect("127.0.0.1", port, idB.device_id))

            ok = await wait_until(lambda: b_has(n1_id), 8.0)
            assert ok, "PUT A→B (зашифр.→незашифр.) не доехал"
            print("OK: создание на A (зашифр.) применилось на B")

            # === удаление на A → должно удалиться на B (провалившийся случай) ===
            enter(A)
            note = vault.find_note(n1_id)
            vault.delete_note(note)
            await engA.push_all()
            ok2 = await wait_until(lambda: not b_has(n1_id), 8.0)
            assert ok2, "❌ ВОСПРОИЗВЕДЕНО: DEL A→B не доехал (удаление не применилось на B)"
            print("OK: удаление на A (зашифр.) применилось на B")

            for s in list(engA.sessions.values()):
                s.close()
            server.close()
            await server.wait_closed()
            task.cancel()

        asyncio.run(scenario())
        if _ERRORS:
            print(f"⚠ asyncio проглотил исключения движка: {_ERRORS}")
        assert not _ERRORS, f"исключения на потоке движка: {_ERRORS}"
        print("ALL LIVE-ENGINE ENCRYPTED TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
