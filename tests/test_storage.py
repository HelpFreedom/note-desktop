"""Тесты слоя хранилища на временном vault (без GUI).

Запуск:
    .venv/bin/python tests/test_storage.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run() -> None:
    from qtnotes.storage import vault
    from qtnotes.storage.models import Note

    # папки
    f1 = vault.create_folder("Работа", caption="раб", color="#5288c1")
    f2 = vault.create_folder("Личное")
    folders = vault.list_folders()
    assert len(folders) == 2, folders
    assert [f.order for f in folders] == [0, 1]
    assert folders[0].name == "Работа" and folders[0].caption == "раб"

    # заметки
    n1 = Note.create_text(f1.id, "<p>привет</p>", "привет")
    vault.save_note(n1)
    n2 = Note.create_text(f1.id, "<p>мир</p>", "мир")
    vault.save_note(n2)
    notes = vault.list_notes(f1.id)
    assert len(notes) == 2, notes
    assert vault.list_notes(f2.id) == []

    # round-trip сериализации
    loaded = vault.list_notes(f1.id)[0]
    assert loaded.plaintext == "привет"
    assert loaded.folder_id == f1.id

    # поиск: нечёткий по содержанию (с опечаткой) и по области
    from qtnotes.storage import search as search_mod
    hits = search_mod.search("привт", folder_id=f1.id)  # опечатка
    assert any(h.note.plaintext == "привет" for h in hits), [h.note.plaintext for h in hits]
    assert search_mod.search("привет", folder_id=f2.id) == []   # в пустой папке нет
    assert any(h.note.plaintext == "привет"
               for h in search_mod.search("привет"))            # глобально находит

    # удаление заметки
    vault.delete_note(n1)
    assert len(vault.list_notes(f1.id)) == 1

    # перенос заметки между папками
    moved = vault.list_notes(f1.id)[0]
    vault.move_note(moved, f2.id)
    assert moved.folder_id == f2.id
    assert any(n.id == moved.id for n in vault.list_notes(f2.id)), "заметка не появилась в f2"
    assert all(n.id != moved.id for n in vault.list_notes(f1.id)), "заметка осталась в f1"

    # поиск заметки по id во всех папках
    found = vault.find_note(moved.id)
    assert found is not None and found.folder_id == f2.id
    assert vault.find_note("0" * 32) is None

    # события календаря
    ev = vault.add_event("2026-06-17", "Встреча", "#5288c1")
    vault.add_event("2026-06-17", "Второе", "#67b35e")
    events = vault.list_events()
    assert len(events) == 2
    assert events[0].date == "2026-06-17" and events[0].name == "Встреча"
    vault.delete_event(ev.id)
    assert len(vault.list_events()) == 1

    # удаление папки
    vault.delete_folder(f2.id)
    assert len(vault.list_folders()) == 1

    print("STORAGE OK: папки, заметки, события — CRUD, перенос, поиск")


def run_export() -> None:
    """Экспорт из одного хранилища и импорт в новое (перенос на др. машину)."""
    from qtnotes.storage import exporter, vault
    from qtnotes.storage.models import Note

    src = tempfile.mkdtemp()
    dst = tempfile.mkdtemp()
    zp = os.path.join(tempfile.gettempdir(), "qtnotes_export_test.zip")

    os.environ["QTNOTES_VAULT"] = src
    f = vault.create_folder("Экспорт", icon="star")
    vault.save_note(Note.create_text(f.id, "<p>данные</p>", "данные"))
    vault.add_event("2026-01-01", "Новый год", "#67b35e")
    exporter.export_all(zp)

    os.environ["QTNOTES_VAULT"] = dst
    assert vault.list_folders() == [], "новое хранилище должно быть пустым"
    count = exporter.import_archive(zp)
    assert count >= 2, count
    folders = vault.list_folders()
    assert len(folders) == 1 and folders[0].name == "Экспорт"
    assert folders[0].icon == "star"
    assert len(vault.list_notes(folders[0].id)) == 1
    assert len(vault.list_events()) == 1
    os.remove(zp)
    print("EXPORT OK: перенос всех данных в новое хранилище работает")


def run_index() -> None:
    """Индекс — это кэш: удаление файла и rebuild восстанавливают поиск и
    поиск заметки по id из JSON-файлов."""
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note

    f = vault.create_folder("Идея")
    n = Note.create_text(f.id, "<p>квантовая запутанность</p>", "квантовая запутанность")
    vault.save_note(n)

    # симулируем потерю/отсутствие индекса
    idx = config_index_path()
    index.reset_for_tests()
    for p in (idx, idx.with_suffix(idx.suffix + "-wal"), idx.with_suffix(idx.suffix + "-shm")):
        if p.exists():
            p.unlink()

    # ensure_ready должен перестроить индекс из файлов
    index.ensure_ready()
    from qtnotes.storage import search as search_mod
    hits = search_mod.search("запутанность")
    assert any(h.note.id == n.id for h in hits), [h.note.plaintext for h in hits]
    assert vault.find_note(n.id) is not None
    print("INDEX OK: rebuild из файлов восстанавливает поиск и find_note")


def config_index_path():
    from qtnotes import config
    return config.index_path()


def run_refs() -> None:
    """Обратный индекс ссылок [[id]]: кто на кого ссылается, и его обновление."""
    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note

    f = vault.create_folder("Связи")
    a = Note.create_text(f.id, "<p>цель</p>", "цель")
    vault.save_note(a)
    b = Note.create_text(f.id, f"<p>см [[{a.id}]]</p>", f"см [[{a.id}]]")
    vault.save_note(b)

    assert index.referrers(a.id) == [b.id], index.referrers(a.id)

    # правка B без ссылки → обратная ссылка исчезает
    b.plaintext = "без ссылки"
    vault.save_note(b)
    assert index.referrers(a.id) == [], index.referrers(a.id)

    # вернуть ссылку, затем удалить B → обратная ссылка снова исчезает
    b.plaintext = f"опять [[{a.id}]]"
    vault.save_note(b)
    assert index.referrers(a.id) == [b.id]
    vault.delete_note(b)
    assert index.referrers(a.id) == []

    # rebuild из файлов восстанавливает refs
    c = Note.create_text(f.id, f"<p>[[{a.id}]]</p>", f"[[{a.id}]]")
    vault.save_note(c)
    index.rebuild()
    assert index.referrers(a.id) == [c.id]
    print("REFS OK: обратный индекс ссылок [[id]] синхронизируется и rebuild'ится")


def main() -> int:
    # Изоляция: не читать реальный settings.json пользователя (там может быть включено
    # шифрование → запись в залоченный vault упадёт). Тесту нужен чистый конфиг.
    os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        run()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes.storage import index
        index.reset_for_tests()
        run_index()
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["QTNOTES_VAULT"] = tmp
        from qtnotes.storage import index
        index.reset_for_tests()
        run_refs()
    run_export()
    return 0


if __name__ == "__main__":
    sys.exit(main())
