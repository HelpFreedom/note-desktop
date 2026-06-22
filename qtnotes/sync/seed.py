"""Засев журнала из текущего хранилища при первом включении синхронизации.

До включения синка op-log не ведётся, поэтому существующие папки/заметки/события
в журнале не отражены. При первом включении пробегаем по vault и пишем по одной
`*.put`-операции на сущность (плюс переводим вложения в blob-стор). Это и есть
одноразовая миграция «было локально → готово к синку». Идемпотентно (флаг seeded).
"""

from __future__ import annotations

from ..storage import vault
from . import oplog


def ensure_seeded() -> None:
    if oplog.get_meta("seeded") == "1":
        return
    for folder in vault.list_folders():
        oplog.append_local("folder.put", folder.id, folder.as_dict())
        for note in vault.list_notes(folder.id):
            # перевести вложения в blobs и сохранить sha256 на диск (без лога)
            if vault.ensure_blobs(note):
                vault.apply_note_put(note)
            oplog.append_local("note.put", note.id, note.as_dict())
    for ev in vault.list_events():
        oplog.append_local("event.put", ev.id, ev.as_dict())
    oplog.set_meta("seeded", "1")
