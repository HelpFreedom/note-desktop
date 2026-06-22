"""Тесты движка синхронизации (headless, без сети на ранних шагах).

Запуск:
    .venv/bin/python tests/test_sync.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run_identity() -> None:
    """A1: личность устройства стабильна, device_id выводится из cert."""
    from qtnotes.sync import identity

    id1 = identity.ensure_identity()
    assert len(id1.device_id) == 16, id1.device_id
    assert id1.fingerprint.startswith(id1.device_id)
    assert id1.cert_pem and id1.key_pem
    assert id1.cert_path.exists() and id1.key_path.exists()

    # повторный вызов — та же личность (ключ не перегенерён)
    id2 = identity.ensure_identity()
    assert id2.device_id == id1.device_id, (id1.device_id, id2.device_id)
    assert id2.cert_pem == id1.cert_pem

    # device_id пира выводится из его cert
    assert identity.device_id_from_cert_pem(id1.cert_pem) == id1.device_id
    print("SYNC A1 identity OK: стабильный device_id, cert/ключ на месте")


def run_peers() -> None:
    """A1: trust-store сопряжённых устройств."""
    from qtnotes.sync import peers

    assert peers.list_peers() == []
    peers.add_peer("abc123def456aaaa", "Телефон", "-----CERT-----")
    assert peers.is_trusted("abc123def456aaaa")
    p = peers.get_peer("abc123def456aaaa")
    assert p is not None and p.name == "Телефон" and p.paired_at
    # повторное добавление обновляет, не дублирует
    peers.add_peer("abc123def456aaaa", "Мой телефон", "-----CERT2-----")
    assert len(peers.list_peers()) == 1
    assert peers.get_peer("abc123def456aaaa").name == "Мой телефон"
    peers.remove_peer("abc123def456aaaa")
    assert not peers.is_trusted("abc123def456aaaa")
    print("SYNC A1 peers OK: trust-store add/get/update/remove")


def run_oplog() -> None:
    """A2: vault-операции пишут ops; version_vector/ops_since/record_remote."""
    from qtnotes import config
    from qtnotes.storage import vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import oplog

    config.set_sync_enabled(True)

    f = vault.create_folder("Журнал")
    n = Note.create_text(f.id, "<p>привет</p>", "привет")
    vault.save_note(n)

    kinds = [o["kind"] for o in oplog.all_ops()]
    assert "folder.put" in kinds and "note.put" in kinds, kinds

    vv = oplog.version_vector()
    did = oplog.local_device_id()
    assert did in vv and vv[did] >= 2, vv

    # ops_since(пустой вектор) = все; ops_since(текущий) = ничего
    assert len(oplog.ops_since({})) == len(oplog.all_ops())
    assert oplog.ops_since(vv) == []

    # удаление пишет tombstone-op
    vault.delete_note(n)
    assert any(o["kind"] == "note.del" and o["entity_id"] == n.id
               for o in oplog.all_ops())

    # приём удалённой операции: сохраняется, поднимает vv, идемпотентен
    remote = {"op_id": "ffffffffffffffff:1", "device_id": "ffffffffffffffff",
              "lamport": 1, "wall": "2026-01-01T00:00:00+00:00",
              "kind": "folder.put", "entity_id": "rem1",
              "payload": {"id": "rem1", "name": "Удалённая папка"}}
    assert oplog.record_remote(remote) is True
    assert oplog.record_remote(remote) is False   # повтор — не дублируется
    assert oplog.version_vector().get("ffffffffffffffff") == 1
    print("SYNC A2 oplog OK: ops, version vector, ops_since, record_remote")


def run_merge() -> None:
    """A3: два устройства сходятся к одному состоянию (перенос/правка/LWW/удаление)."""
    import shutil

    from qtnotes import config
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import apply, oplog

    A = (tempfile.mkdtemp(), tempfile.mkdtemp())   # (cfg, vault)
    B = (tempfile.mkdtemp(), tempfile.mkdtemp())

    def use(ctx):
        os.environ["XDG_CONFIG_HOME"], os.environ["QTNOTES_VAULT"] = ctx
        oplog.reset_for_tests()
        index.reset_for_tests()

    def sync(src, dst):
        """Отдать dst всё, чего у него нет от src, и применить."""
        use(dst)
        dst_vv = oplog.version_vector()
        use(src)
        ops = oplog.ops_since(dst_vv)
        use(dst)
        for op in ops:
            if oplog.record_remote(op):
                apply.apply_op(op)

    try:
        use(A)
        config.set_sync_enabled(True)
        fa = vault.create_folder("Общая")
        n = Note.create_text(fa.id, "<p>v1</p>", "v1")
        vault.save_note(n)

        use(B)
        config.set_sync_enabled(True)
        assert vault.find_note(n.id) is None

        # A → B: папка и заметка приезжают
        sync(A, B)
        use(B)
        assert vault.find_note(n.id) is not None, "заметка не пришла на B"
        assert any(f.id == fa.id for f in vault.list_folders()), "папка не пришла"
        assert vault.find_note(n.id).plaintext == "v1"

        # правка на B → A подтягивает
        use(B)
        nb = vault.find_note(n.id)
        nb.plaintext, nb.html = "v2-from-B", "<p>v2-from-B</p>"
        nb.touch()
        vault.save_note(nb)
        sync(B, A)
        use(A)
        assert vault.find_note(n.id).plaintext == "v2-from-B"

        # конфликт: правки на обоих, побеждает более свежая по modified (B)
        use(A)
        na = vault.find_note(n.id)
        na.plaintext = "A-edit"
        na.modified = "2029-01-01T00:00:00.000000+00:00"
        vault.save_note(na)
        use(B)
        nb = vault.find_note(n.id)
        nb.plaintext = "B-edit"
        nb.modified = "2030-01-01T00:00:00.000000+00:00"
        vault.save_note(nb)
        sync(A, B)
        sync(B, A)
        use(A)
        assert vault.find_note(n.id).plaintext == "B-edit", vault.find_note(n.id).plaintext
        use(B)
        assert vault.find_note(n.id).plaintext == "B-edit"

        # удаление на A распространяется на B
        use(A)
        vault.delete_note(vault.find_note(n.id))
        sync(A, B)
        use(B)
        assert vault.find_note(n.id) is None, "удаление не распространилось"

        print("SYNC A3 merge OK: перенос, правка, LWW-конфликт, удаление сходятся")
    finally:
        for cfg, vlt in (A, B):
            shutil.rmtree(cfg, ignore_errors=True)
            shutil.rmtree(vlt, ignore_errors=True)


def run_blobs() -> None:
    """A4: вложения переходят в content-addressed blobs, дедуп, экспорт/импорт."""
    import shutil

    from qtnotes import config
    from qtnotes.storage import exporter, index, vault
    from qtnotes.storage.models import Attachment, Note, new_id
    from qtnotes.sync import oplog, seed

    cfg = tempfile.mkdtemp()
    v1 = tempfile.mkdtemp()
    v2 = tempfile.mkdtemp()
    os.environ["XDG_CONFIG_HOME"] = cfg
    os.environ["QTNOTES_VAULT"] = v1
    oplog.reset_for_tests()
    index.reset_for_tests()

    try:
        # --- legacy-вложения (синк выключен) ---
        config.set_sync_enabled(False)
        f = vault.create_folder("Медиа")
        data = b"one-and-the-same-bytes" * 100

        def mk_note(fname: str) -> Note:
            n = Note(id=new_id(), folder_id=f.id, kind="file", plaintext=fname)
            adir = vault.attachments_dir(f.id, n.id)
            (adir / fname).write_bytes(data)
            n.attachments = [Attachment(file=fname, mime="application/octet-stream",
                                        name=fname, size=len(data))]
            vault.save_note(n)
            return n

        n1 = mk_note("a.bin")
        n2 = mk_note("b.bin")   # тот же контент, другое имя
        assert vault.attachment_abspath(n1, n1.attachments[0]).exists()
        assert n1.attachments[0].sha256 == ""   # пока legacy

        # --- включаем синк: пересохранение мигрирует в blobs ---
        config.set_sync_enabled(True)
        vault.save_note(n1)
        vault.save_note(n2)

        a1 = n1.attachments[0]
        assert a1.sha256, "sha256 не проставлен"
        assert vault.blob_path(a1.sha256).exists(), "blob не создан"
        assert not (vault.attachments_dir(f.id, n1.id) / "a.bin").exists(), "legacy не удалён"
        assert vault.attachment_abspath(n1, a1) == vault.blob_path(a1.sha256)

        # дедуп: одинаковый контент → один blob
        blobs = [p for p in config.blobs_dir().iterdir() if p.is_file()]
        assert len(blobs) == 1, [p.name for p in blobs]
        assert n2.attachments[0].sha256 == a1.sha256

        # op note.put несёт sha256
        put_ops = [o for o in oplog.all_ops()
                   if o["kind"] == "note.put" and o["entity_id"] == n1.id]
        assert put_ops and put_ops[-1]["payload"]["attachments"][0]["sha256"] == a1.sha256

        # seed идемпотентен (флаг)
        seed.ensure_seeded()
        before = len(oplog.all_ops())
        seed.ensure_seeded()
        assert len(oplog.all_ops()) == before, "повторный seed добавил ops"

        # --- экспорт/импорт с blob ---
        zp = os.path.join(tempfile.gettempdir(), "qtnotes_blob_export.zip")
        exporter.export_all(zp)

        os.environ["QTNOTES_VAULT"] = v2
        oplog.reset_for_tests()
        index.reset_for_tests()
        assert vault.list_folders() == []
        exporter.import_archive(zp)
        os.remove(zp)

        folders = vault.list_folders()
        assert len(folders) == 1
        notes = vault.list_notes(folders[0].id)
        assert len(notes) == 2
        att = notes[0].attachments[0]
        assert att.sha256 == a1.sha256
        assert vault.attachment_abspath(notes[0], att).exists(), "blob не восстановился"
        print("SYNC A4 blobs OK: миграция, дедуп, sha256 в op, экспорт/импорт blob")
    finally:
        for d in (cfg, v1, v2):
            shutil.rmtree(d, ignore_errors=True)


def run_transport() -> None:
    """A5: взаимный TLS (pinning), обмен CONTROL/BLOB, отказ недоверенному."""
    import asyncio
    import hashlib
    import shutil
    import ssl
    from pathlib import Path

    from qtnotes.sync import identity, transport, wire

    dirs = [tempfile.mkdtemp() for _ in range(3)]
    idA = identity.load_or_create(Path(dirs[0]), "DevA")
    idB = identity.load_or_create(Path(dirs[1]), "DevB")
    idC = identity.load_or_create(Path(dirs[2]), "DevC")
    blob_data = b"\x00\x01binary-blob-\xff payload" * 50
    blob_sha = hashlib.sha256(blob_data).hexdigest()

    async def positive():
        seen = {}

        async def handler(reader, writer):
            ssl_obj = writer.get_extra_info("ssl_object")
            seen["server_sees"] = transport.peer_device_id(ssl_obj)
            kind, msg = await wire.read_frame(reader)
            seen["got"] = (kind, msg)
            await wire.write_message(writer, {"type": "hello", "device_id": idB.device_id})
            await wire.write_blob(writer, blob_sha, blob_data)
            await writer.drain()

        # сервер B доверяет только A
        server = await transport.start_server(idB, idA.cert_pem.decode(), handler,
                                              host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await transport.open_connection("127.0.0.1", port, idA,
                                                         idB.cert_pem.decode())
        client_sees = transport.peer_device_id(writer.get_extra_info("ssl_object"))
        await wire.write_message(writer, {"type": "hello", "device_id": idA.device_id})
        k1, resp = await wire.read_frame(reader)
        k2, (sha, data) = await wire.read_frame(reader)
        writer.close()
        server.close()
        await server.wait_closed()
        return seen, client_sees, resp, (k2, sha, data)

    seen, client_sees, resp, blobres = asyncio.run(positive())
    assert seen["server_sees"] == idA.device_id, seen
    assert client_sees == idB.device_id
    assert seen["got"][1]["device_id"] == idA.device_id
    assert resp["device_id"] == idB.device_id
    assert blobres[0] == "blob" and blobres[1] == blob_sha and blobres[2] == blob_data

    async def negative():
        async def handler(reader, writer):
            try:
                await wire.read_frame(reader)
            except Exception:
                pass

        server = await transport.start_server(idB, idA.cert_pem.decode(), handler,
                                              host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        failed = False
        try:
            # C не в списке доверенных у B → рукопожатие/обмен обрываются
            reader, writer = await transport.open_connection("127.0.0.1", port, idC,
                                                             idB.cert_pem.decode())
            await wire.write_message(writer, {"type": "hello"})
            await wire.read_frame(reader)
        except (ssl.SSLError, OSError, asyncio.IncompleteReadError, ConnectionError):
            failed = True
        finally:
            server.close()
            await server.wait_closed()
        return failed

    assert asyncio.run(negative()) is True, "недоверенный клиент НЕ был отклонён"
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("SYNC A5 transport OK: mutual-TLS pinning, CONTROL/BLOB, отказ чужому")


def run_discovery() -> None:
    """A6: разбор mDNS-сервиса (детерминированно) + мягкая живая регистрация."""
    import shutil
    import socket
    import time
    from pathlib import Path

    from zeroconf import ServiceInfo

    from qtnotes.sync import discovery, identity

    # --- чистая логика (без сети) ---
    info = ServiceInfo(
        discovery.SERVICE_TYPE, "aabbccddeeff0011." + discovery.SERVICE_TYPE,
        addresses=[socket.inet_aton("192.168.1.50")], port=54321,
        properties={b"id": b"aabbccddeeff0011", b"name": "Телефон".encode()})
    peer = discovery.parse_service_info(info, our_device_id="0000000000000000")
    assert peer is not None
    assert peer.device_id == "aabbccddeeff0011" and peer.port == 54321
    assert peer.host == "192.168.1.50"
    # свой сервис отфильтровывается
    assert discovery.parse_service_info(info, our_device_id="aabbccddeeff0011") is None
    assert discovery.device_id_from_service_name(
        "aabbccddeeff0011." + discovery.SERVICE_TYPE) == "aabbccddeeff0011"
    print("SYNC A6 discovery OK: разбор сервиса и self-фильтр")

    # --- живая регистрация/браузер (мягко: зависит от мультикаста) ---
    dirs = [tempfile.mkdtemp(), tempfile.mkdtemp()]
    try:
        idA = identity.load_or_create(Path(dirs[0]), "DevA")
        idFake = identity.load_or_create(Path(dirs[1]), "DevFake")
        found: list = []
        d = discovery.Discovery(idA, port=45111, on_found=found.append)
        d.start()
        try:
            fake = discovery.build_service_info(idFake, port=45112)
            d._zc.register_service(fake)
            deadline = time.time() + 3.0
            while time.time() < deadline and not any(
                    p.device_id == idFake.device_id for p in found):
                time.sleep(0.1)
            d._zc.unregister_service(fake)
            if any(p.device_id == idFake.device_id for p in found):
                print("SYNC A6 live OK: пир обнаружен по mDNS")
            else:
                print("SYNC A6 live SKIP: мультикаст недоступен в этой среде (норм)")
        finally:
            d.stop()
    except Exception as e:  # noqa: BLE001 — живая часть не обязана работать в TTY
        print(f"SYNC A6 live SKIP: {type(e).__name__}: {e}")
    finally:
        for dd in dirs:
            shutil.rmtree(dd, ignore_errors=True)


def run_pairing() -> None:
    """A7: QR-сопряжение — обмен доверием, отказ при подмене/неверном токене."""
    import asyncio
    import shutil
    from pathlib import Path

    from qtnotes.sync import identity, pairing

    dirs = [tempfile.mkdtemp(), tempfile.mkdtemp()]
    idA = identity.load_or_create(Path(dirs[0]), "Десктоп")
    idP = identity.load_or_create(Path(dirs[1]), "Телефон")
    token = pairing.generate_token()

    # QR: payload собирается и разбирается
    payload_str = pairing.make_pairing_payload(idA, "127.0.0.1", 0, token)
    parsed = pairing.parse_pairing_payload(payload_str)
    assert parsed["did"] == idA.device_id and parsed["fp"] == idA.fingerprint
    matrix = pairing.qr_matrix(payload_str)
    assert matrix and len(matrix) == len(matrix[0]) > 0   # квадратная матрица QR

    async def scenario():
        desktop_got: list = []
        server = await pairing.serve_pairing(idA, token, on_paired=desktop_got.append,
                                             host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        good = pairing.parse_pairing_payload(
            pairing.make_pairing_payload(idA, "127.0.0.1", port, token))

        # ВАЖНО (S2: одноразовый токен): отказные сценарии — ДО успешной пары, т.к.
        # успех гасит токен (consumed) и закрывает слушатель. Отказы токен не тратят.

        # подмена fingerprint → отказ (клиентская сверка до отправки hello)
        bad_fp = dict(good); bad_fp["fp"] = "00" * 32
        fp_rejected = False
        try:
            await pairing.pair_with(bad_fp, idP)
        except pairing.PairingError:
            fp_rejected = True

        # неверный token → отказ (сервер отвечает pair_err, токен не потреблён)
        bad_tok = dict(good); bad_tok["token"] = "wrong-token"
        tok_rejected = False
        try:
            await pairing.pair_with(bad_tok, idP)
        except pairing.PairingError:
            tok_rejected = True

        # корректная пара → успех (потребляет токен, закрывает слушатель)
        desktop_peer = await pairing.pair_with(good, idP)

        # одноразовость: повторная пара тем же токеном уже невозможна (consumed/закрыт)
        await asyncio.sleep(0.1)
        reuse_rejected = False
        try:
            await pairing.pair_with(good, idP)
        except (pairing.PairingError, OSError):
            reuse_rejected = True

        try:
            server.close()
            await server.wait_closed()
        except Exception:
            pass
        return desktop_got, desktop_peer, fp_rejected, tok_rejected, reuse_rejected

    desktop_got, desktop_peer, fp_rejected, tok_rejected, reuse_rejected = \
        asyncio.run(scenario())

    # телефон занёс десктоп; десктоп занёс телефон — взаимно и верно
    assert desktop_peer.device_id == idA.device_id
    assert identity.fingerprint_from_cert_pem(desktop_peer.cert_pem.encode()) == idA.fingerprint
    assert len(desktop_got) == 1, len(desktop_got)
    assert desktop_got[0].device_id == idP.device_id
    assert identity.fingerprint_from_cert_pem(desktop_got[0].cert_pem.encode()) == idP.fingerprint
    assert fp_rejected and tok_rejected
    assert reuse_rejected, "повторная пара тем же токеном должна быть отклонена (S2)"
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)
    print("SYNC A7 pairing OK: QR-обмен доверием, отказ при подмене/токене/повторе")


def run_engine() -> None:
    """A8: два движка сходятся по реальному TLS+протоколу (+blob, +push-on-change)."""
    import asyncio
    import shutil
    from pathlib import Path

    from qtnotes import config
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Attachment, Note, new_id
    from qtnotes.sync import engine as engine_mod
    from qtnotes.sync import identity, oplog, peers, store

    A = (tempfile.mkdtemp(), tempfile.mkdtemp())   # (cfg, vault)
    B = (tempfile.mkdtemp(), tempfile.mkdtemp())

    def enter(ctx):
        os.environ["XDG_CONFIG_HOME"], os.environ["QTNOTES_VAULT"] = ctx
        oplog.reset_for_tests()
        index.reset_for_tests()

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

    try:
        # A: вложение-заметка (синк включён → blob)
        enter(A)
        config.set_sync_enabled(True)
        idA = identity.ensure_identity()
        f = vault.create_folder("Синк")
        data = b"engine-blob-bytes" * 300
        n1 = Note(id=new_id(), folder_id=f.id, kind="file", plaintext="file-note")
        (vault.attachments_dir(f.id, n1.id) / "x.bin").write_bytes(data)
        n1.attachments = [Attachment(file="x.bin", mime="application/octet-stream",
                                     name="x.bin", size=len(data))]
        vault.save_note(n1)
        sha = n1.attachments[0].sha256
        assert sha and vault.has_blob(sha)
        n1_id = n1.id

        # B: пусто
        enter(B)
        config.set_sync_enabled(True)
        idB = identity.ensure_identity()

        peerA = peers.Peer(idA.device_id, "A", idA.cert_pem.decode(), "now")
        peerB = peers.Peer(idB.device_id, "B", idB.cert_pem.decode(), "now")
        engA = engine_mod.SyncEngine(idA, CtxStore(A), get_peers=lambda: [peerB])
        engB = engine_mod.SyncEngine(idB, CtxStore(B), get_peers=lambda: [peerA])

        def b_has_note(nid):
            enter(B); return vault.find_note(nid) is not None

        def b_has_blob(h):
            enter(B); return vault.has_blob(h)

        async def wait_until(pred, timeout):
            loop = asyncio.get_running_loop()
            end = loop.time() + timeout
            while loop.time() < end:
                if pred():
                    return True
                await asyncio.sleep(0.05)
            return pred()

        async def scenario():
            server = await engB.serve("127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            task = asyncio.create_task(engA.connect("127.0.0.1", port, idB.device_id))

            ok = await wait_until(lambda: b_has_note(n1_id) and b_has_blob(sha), 8.0)
            assert ok, "начальная синхронизация (заметка+blob) не сошлась"

            # push-on-change: A добавляет заметку → B получает без переподключения
            enter(A)
            n2 = Note.create_text(f.id, "<p>пуш</p>", "пуш-заметка")
            vault.save_note(n2)
            await engA.push_all()
            ok2 = await wait_until(lambda: b_has_note(n2.id), 8.0)
            assert ok2, "push-on-change не доставил новую заметку"

            for s in list(engA.sessions.values()):
                s.close()
            server.close()
            await server.wait_closed()
            task.cancel()

        asyncio.run(scenario())

        # финальная проверка содержимого на B
        enter(B)
        got = vault.find_note(n1_id)
        assert got is not None and got.plaintext == "file-note"
        assert vault.attachment_abspath(got, got.attachments[0]).exists()
        print("SYNC A8 engine OK: TLS-сессия, начальный синк, blob, push-on-change")
    finally:
        for cfg, vlt in (A, B):
            shutil.rmtree(cfg, ignore_errors=True)
            shutil.rmtree(vlt, ignore_errors=True)


def main() -> int:
    with tempfile.TemporaryDirectory() as cfg, tempfile.TemporaryDirectory() as vault:
        os.environ["XDG_CONFIG_HOME"] = cfg
        os.environ["QTNOTES_VAULT"] = vault
        run_identity()
        run_peers()
        run_oplog()
        run_merge()
        run_blobs()
        run_transport()
        run_discovery()
        run_pairing()
        run_engine()
    return 0


if __name__ == "__main__":
    sys.exit(main())
