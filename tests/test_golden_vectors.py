"""M5: golden-vector конформанс Python↔Dart — защита от тихого дрейфа крипто/сериализации,
который молча сломал бы синхронизацию между десктопом и телефоном.

Оркестратор: генерим вектора с ФИКСИРОВАННЫМ MK → гоняем Dart-верификатор (subprocess)
→ проверяем взаимную совместимость формата (обе стороны) и сериализации моделей.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_golden_vectors.py
"""

import base64
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Dart-сторона конформанса опциональна: запускается, только если в PATH есть `dart`
# и рядом лежит мобильный проект (репозиторий qtnotes-mobile). Иначе — пропуск.
_DART = shutil.which("dart")


def main():
    from qtnotes.crypto import primitives as P
    from qtnotes.storage import crypto_fs as cfs
    from qtnotes.storage.models import Attachment, Event, Folder, Note

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    gdir = os.path.join(repo, "tests", "golden")
    os.makedirs(gdir, exist_ok=True)

    mk = bytes(range(32))
    relpath = "folders/f1/notes/n1.json"
    plaintext = "Привет 🎉".encode("utf-8")
    subkey = cfs._subkey(mk, relpath.encode("utf-8"))          # HKDF(mk,"file/"+relpath)
    py_sealed = cfs.MAGIC + P.seal(subkey, plaintext, aad=relpath.encode("utf-8"))

    note = Note(id="n1", folder_id="f1", kind="text", html="<p>Привет 🎉</p>",
                plaintext="Привет 🎉", caption_html="", attachments=[], date_tag=None,
                created="2026-01-01T00:00:00.000000+00:00",
                modified="2026-01-02T03:04:05.000000+00:00")

    # H1 (раунд-3): покрыть сериализацию ВСЕХ синкаемых моделей (раньше только Note) —
    # ловит дрейф формата Folder/Attachment/Event, который молча сломал бы синк.
    folder = Folder(id="f1", name="Папка 🎉", caption="подпись", color="#ff0000",
                    icon="star", order=3, created="2026-01-01T00:00:00.000000+00:00")
    att = Attachment(file="x.bin", mime="application/octet-stream", name="икс.bin",
                     size=1234, w=640, h=480, sha256="abcdef00")
    event = Event(id="e1", date="2026-03-15", name="Событие 🎂", color="#00ff00")
    note_att = Note(id="n2", folder_id="f1", kind="image", html="<p>подпись</p>",
                    plaintext="подпись", caption_html="<p>подпись</p>", attachments=[att],
                    date_tag="2026-03-15", created="2026-01-01T00:00:00.000000+00:00",
                    modified="2026-01-02T03:04:05.000000+00:00")

    vectors = {
        "mk_b64": base64.b64encode(mk).decode(),
        "relpath": relpath,
        "plaintext_b64": base64.b64encode(plaintext).decode(),
        "py_subkey_b64": base64.b64encode(subkey).decode(),
        "py_sealed_b64": base64.b64encode(py_sealed).decode(),
        "note_json": note.as_dict(),
    }
    with open(os.path.join(gdir, "vectors.json"), "w", encoding="utf-8") as f:
        json.dump(vectors, f, ensure_ascii=False)

    out_path = os.path.join(gdir, "dart_out.json")
    if os.path.exists(out_path):
        os.remove(out_path)

    # найти мобильный проект: либо подкаталог mobile/ (моно-лейаут), либо соседний
    # репозиторий ../qtnotes-mobile. Если нет dart или проекта — пропускаем Dart-сторону.
    mobile_dir = next(
        (d for d in (os.path.join(repo, "mobile"),
                     os.path.join(os.path.dirname(repo), "qtnotes-mobile"))
         if os.path.exists(os.path.join(d, "test", "golden_conformance_test.dart"))),
        None)
    if _DART is None or mobile_dir is None:
        print("⚠ dart или проект qtnotes-mobile не найдены — пропускаю Dart-сторону "
              "(Python-сторона проверена)")
        return
    env = dict(os.environ, GOLDEN_DIR=gdir)
    r = subprocess.run([_DART, "test", "test/golden_conformance_test.dart"],
                       cwd=mobile_dir, env=env,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        sys.stdout.write(r.stdout)
        sys.stderr.write(r.stderr)
        raise AssertionError("Dart golden-тест упал (конформанс нарушен)")
    assert os.path.exists(out_path), "Dart не записал dart_out.json"
    with open(out_path, encoding="utf-8") as f:
        dout = json.load(f)

    # 1) субключ совпал (вывод ключа детерминирован и одинаков)
    assert dout["dart_subkey_b64"] == vectors["py_subkey_b64"], "субключ разошёлся Python↔Dart"
    print("OK: вывод субключа HKDF совпал")

    # 2) сериализация моделей совпала (глубокое сравнение — порядок ключей не важен)
    assert dout["note_json"] == note.as_dict(), \
        f"JSON заметки разошёлся:\n py={note.as_dict()}\n dart={dout['note_json']}"
    assert dout["folder_json"] == folder.as_dict(), \
        f"JSON Folder разошёлся:\n py={folder.as_dict()}\n dart={dout['folder_json']}"
    assert dout["attachment_json"] == att.as_dict(), \
        f"JSON Attachment разошёлся:\n py={att.as_dict()}\n dart={dout['attachment_json']}"
    assert dout["event_json"] == event.as_dict(), \
        f"JSON Event разошёлся:\n py={event.as_dict()}\n dart={dout['event_json']}"
    assert dout["note_att_json"] == note_att.as_dict(), \
        f"JSON Note-с-вложением разошёлся:\n py={note_att.as_dict()}\n dart={dout['note_att_json']}"
    print("OK: JSON-сериализация Note/Folder/Attachment/Event совпала (все синкаемые модели)")

    # 3) Python→Dart уже проверено внутри Dart-теста (расшифровал py_sealed).
    #    Dart→Python: Python расшифровывает Dart-шифртекст
    dart_sealed = base64.b64decode(dout["dart_sealed_b64"])
    assert dart_sealed[:len(cfs.MAGIC)] == cfs.MAGIC, "Dart-шифртекст без MAGIC"
    dec = P.open_sealed(subkey, dart_sealed[len(cfs.MAGIC):], aad=relpath.encode("utf-8"))
    assert dec == plaintext, "Python не расшифровал Dart-шифртекст (формат разошёлся)"
    print("OK: формат crypto_fs совместим в ОБЕ стороны (Python↔Dart)")

    print("ALL GOLDEN-VECTOR TESTS PASSED")


if __name__ == "__main__":
    main()
