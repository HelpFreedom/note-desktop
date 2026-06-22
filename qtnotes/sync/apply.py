"""Применение удалённых операций к файловому хранилищу с разрешением конфликтов.

Стратегия — LWW (last-writer-wins):
  * note.put применяется, только если входящая версия не старше локальной по
    `modified` (микросекундные ISO8601-метки сравниваются лексикографически в UTC).
    Это делает применение независимым от порядка: устаревший put не перезатрёт
    более свежую правку.
  * note.del удаляет безусловно (удаление — сильное намерение). Редкий пограничный
    случай «правка на одном устройстве одновременно с удалением на другом» решается
    в пользу удаления. Это СОЗНАТЕЛЬНЫЙ выбор пользователя (2026-06-22): удаление
    всегда побеждает, даже более позднюю по времени правку — оставлено намеренно,
    не приводить к LWW.
  * folder/event не имеют поля modified — применяются по порядку (последняя
    применённая операция выигрывает); конкурентные правки папок/событий редки.

Применение идёт через vault.apply_* — они НЕ пишут новую op (иначе приём чужого
изменения зациклил бы ретрансляцию).
"""

from __future__ import annotations

from ..storage import vault
from ..storage.models import Event, Folder, Note
from . import oplog


def _wall(s: str | None) -> str:
    return s or ""


def _suppressed_by_tombstone(op: dict) -> bool:
    """True, если для сущности есть ЛЮБОЙ tombstone → put не применяем.

    Вариант A (выбор пользователя 2026-06-22): «удаление побеждает НАВСЕГДА» —
    любое удаление подавляет ВСЕ put для этой сущности, независимо от времени/lamport.
    Это устраняет расходимость: раньше put новее tombstone'а «воскрешал» заметку, но
    note.del применяется безусловно (apply_op ниже), поэтому при доставке put→del
    выходило absent, а при del→put — present (двое устройств расходились НАВСЕГДА,
    т.к. vv равны и переобмена нет). Безусловное подавление делает результат
    независимым от порядка. id — UUID, поэтому легитимного повторного создания того же
    id не бывает (заново набранная заметка получает новый id). Конформанс: см.
    tests/test_convergence_conformance.py (сценарии put_resurrects_*/tie_lamport_put)."""
    return oplog.tombstone_for(op.get("entity_id")) is not None


def _apply_note_put(op: dict, payload: dict) -> None:
    if _suppressed_by_tombstone(op):
        return   # удаление новее — заметку не воскрешаем (антивоскрешение, H4)
    incoming = Note.from_dict(payload)
    existing = vault.find_note(incoming.id)
    if existing is not None and _wall(existing.modified) > _wall(incoming.modified):
        return   # локальная версия новее — сохраняем её (LWW по времени)
    vault.apply_note_put(incoming)


def apply_op(op: dict) -> None:
    """Применить одну операцию к хранилищу. Идемпотентно."""
    kind = op.get("kind")
    payload = op.get("payload")
    entity_id = op.get("entity_id")

    if kind == "note.put" and payload is not None:
        _apply_note_put(op, payload)
    elif kind == "note.del":
        vault.apply_note_del(entity_id)
    elif kind == "folder.put" and payload is not None:
        if not _suppressed_by_tombstone(op):
            vault.apply_folder_put(Folder.from_dict(payload))
    elif kind == "folder.del":
        vault.apply_folder_del(entity_id)
    elif kind == "event.put" and payload is not None:
        if not _suppressed_by_tombstone(op):
            vault.apply_event_put(Event.from_dict(payload))
    elif kind == "event.del":
        vault.apply_event_del(entity_id)
    elif kind == "setting.put" and payload is not None:
        vault.apply_setting_put(entity_id, payload)
    elif kind == "setting.del":
        vault.apply_setting_del(entity_id)
    else:
        # Неизвестный kind (или put без payload) — НЕ молчим. Бросок не даёт
        # record_and_apply записать op (vv не двигается, H5-контракт), поэтому op
        # переиграется после апгрейда схемы, а не потеряется, помеченным «видели».
        # Защита форвард-совместимости: старый клиент не «съест» op новой версии.
        raise ValueError(f"неизвестный/неполный kind операции: {kind!r}")


def apply_ops(ops: list[dict]) -> None:
    for op in ops:
        apply_op(op)
