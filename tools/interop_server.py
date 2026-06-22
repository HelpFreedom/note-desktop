"""Кросс-языковая проверка синка: Python-сторона на НАСТОЯЩЕМ потоковом движке.

Поднимает движок через engine.start() (отдельный поток, как в приложении), создаёт
заметку и ждёт подключения Dart-клиента. Параллельно сохраняет ещё заметку из
ГЛАВНОГО потока — это нагружает sqlite (oplog/index) из двух потоков и проверяет
исправление check_same_thread. Координация через файлы в /tmp/interop.
"""

import json
import os
import sys
import time

BASE = "/tmp/interop"
os.makedirs(BASE, exist_ok=True)
os.environ["XDG_CONFIG_HOME"] = f"{BASE}/py_cfg"
os.environ["QTNOTES_VAULT"] = f"{BASE}/py_vault"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qtnotes import config  # noqa: E402
from qtnotes.storage import vault  # noqa: E402
from qtnotes.storage.models import Note  # noqa: E402
from qtnotes.sync import engine as engine_mod  # noqa: E402
from qtnotes.sync import identity, oplog, peers, store  # noqa: E402


def main() -> None:
    config.set_sync_enabled(True)
    oplog.reset_for_tests()
    ident = identity.ensure_identity()

    f = vault.create_folder("FromDesktop")
    vault.save_note(Note.create_text(f.id, "<p>привет с десктопа</p>", "привет с десктопа"))

    # тема: палитра + обои как blob (проверяем синк общих настроек)
    import hashlib
    wall = b"FAKE-WALLPAPER-BYTES-" * 200
    wsha = hashlib.sha256(wall).hexdigest()
    vault.write_blob(wsha, wall)
    vault.set_shared("theme", {
        "palette": {"background": "#101820", "accent": "#ff8800", "bubble": "#223344"},
        "wallpaper": wsha,
    })

    dart_cert_path = f"{BASE}/dart_cert.pem"
    for _ in range(120):
        if os.path.exists(dart_cert_path):
            break
        time.sleep(0.5)
    dart_cert = open(dart_cert_path, encoding="utf-8").read()
    dart_id = identity.device_id_from_cert_pem(dart_cert.encode())
    peers.add_peer(dart_id, "DartPhone", dart_cert)

    eng = engine_mod.SyncEngine(ident, store.GlobalStore(), get_peers=peers.list_peers)
    eng.start()  # отдельный поток (как в приложении)
    for _ in range(50):
        if eng._port:
            break
        time.sleep(0.1)

    with open(f"{BASE}/py_ready.json", "w", encoding="utf-8") as fp:
        json.dump({"port": eng._port, "cert": ident.cert_pem.decode(),
                   "device_id": ident.device_id}, fp)

    # сохранение из ГЛАВНОГО потока, пока движок крутится в своём — кросс-поточный sqlite
    time.sleep(2)
    vault.save_note(Note.create_text(f.id, "<p>вторая</p>", "вторая с десктопа"))
    eng.notify_change()

    for _ in range(120):
        if os.path.exists(f"{BASE}/dart_result.json"):
            break
        time.sleep(0.5)
    time.sleep(1)

    got_dart = any(
        n.plaintext == "привет с телефона"
        for fl in vault.list_folders() for n in vault.list_notes(fl.id))
    with open(f"{BASE}/py_check.json", "w", encoding="utf-8") as fp:
        json.dump({"gotDart": got_dart}, fp)
    print(f"PY: got dart note = {got_dart}")
    eng.stop()


if __name__ == "__main__":
    import shutil
    for p in ("py_cfg", "py_vault"):
        shutil.rmtree(f"{BASE}/{p}", ignore_errors=True)
    for fn in ("py_ready.json", "py_check.json"):
        try:
            os.remove(f"{BASE}/{fn}")
        except OSError:
            pass
    main()
