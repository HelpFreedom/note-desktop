"""CRUD папок, заметок и вложений на диске.

Раскладка:
    <vault>/folders/<folder-id>/folder.json
    <vault>/folders/<folder-id>/notes/<note-id>.json
    <vault>/folders/<folder-id>/notes/attachments/<note-id>/<файлы>

Запись атомарная (через временный файл + rename), чтобы не повредить данные
при сбое.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
from pathlib import Path

from .. import config
from . import crypto_fs
from . import index
from .models import Event, Folder, Note

# Агрегатные файлы (events.json/shared.json) переписываются целиком по схеме
# read-modify-write. UI-поток и поток синка могут делать это одновременно → потеря
# обновления (оба прочитали старое, оба записали). Этот замок сериализует такие операции
# между потоками (per-file записи отдельных заметок защищены уникальным tmp в fsutil).
_agg_lock = threading.RLock()


def _log(kind: str, entity_id: str, payload: dict | None) -> None:
    """Записать операцию в журнал синка (только если синк включён). Best-effort:
    сбой журнала никогда не ломает сохранение данных."""
    if not config.sync_enabled():
        return
    try:
        from ..sync import oplog
        oplog.append_local(kind, entity_id, payload)
    except Exception as e:  # noqa: BLE001 — журнал не должен ронять запись
        print(f"[sync] oplog append failed: {e}")


# --- низкоуровневые помощники ---

def _write_json(path: Path, data: dict) -> None:
    # запись идёт через крипто-слой: при выключенном шифровании — обычный plaintext
    # (поведение прежнее), при включённом и разблокированном — зашифрованный файл.
    crypto_fs.write_json(path, data)


def _read_json(path: Path) -> dict | None:
    # чтение через крипто-слой: автоопределение plaintext/зашифрованного формата.
    return crypto_fs.read_json(path)


def _folder_path(folder_id: str) -> Path:
    return config.folders_dir() / folder_id


def _folder_json(folder_id: str) -> Path:
    return _folder_path(folder_id) / "folder.json"


def _notes_dir(folder_id: str) -> Path:
    return _folder_path(folder_id) / "notes"


def _note_json(folder_id: str, note_id: str) -> Path:
    return _notes_dir(folder_id) / f"{note_id}.json"


def attachments_dir(folder_id: str, note_id: str) -> Path:
    d = _notes_dir(folder_id) / "attachments" / note_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def attachment_abspath(note, att) -> Path:
    """Абсолютный путь к файлу вложения.

    Если у вложения есть sha256 — оно лежит в content-addressed blobs/<sha256>
    (режим синка). Иначе — в legacy-папке attachments/<note-id>/ (старые данные,
    режим без синка). Обратная совместимость сохраняется."""
    if att.sha256:
        return blob_path(att.sha256)
    return attachments_dir(note.folder_id, note.id) / att.file


# --- content-addressed хранилище вложений (blobs) ---

def blob_path(sha256: str) -> Path:
    return config.blobs_dir() / sha256


def has_blob(sha256: str) -> bool:
    return blob_path(sha256).exists()


def write_blob(sha256: str, data: bytes) -> Path:
    """Записать blob (если ещё нет). `data` — ПЛЕЙНТЕКСТ; на диск пишется зашифрованным,
    когда шифрование включено (sha256 — хэш плейнтекста, для дедупа/синка). Возвращает путь."""
    p = blob_path(sha256)
    if not p.exists():
        crypto_fs.write_bytes(p, data)  # атомарно; шифрует при включённом шифровании
    return p


def read_blob_bytes(sha256: str) -> bytes | None:
    """Прочитать blob как ПЛЕЙНТЕКСТ (расшифровать при необходимости)."""
    p = blob_path(sha256)
    if not p.exists():
        return None
    return crypto_fs.read_bytes(p)


def _verify_blob(sha256: str) -> bool:
    """Blob читается и его плейнтекст даёт ожидаемый sha256 (для обоих режимов)."""
    try:
        data = read_blob_bytes(sha256)
    except Exception:  # noqa: BLE001 — повреждение/нет ключа → считаем неподтверждённым
        return False
    return data is not None and hashlib.sha256(data).hexdigest() == sha256


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_blobs(note: Note) -> bool:
    """Перевести вложения заметки в blob-стор (идемпотентно).

    Для каждого вложения без sha256: посчитать хэш по legacy-файлу, записать blob,
    проставить sha256 и удалить legacy-файл (дедуп + готовность к синку). Возвращает
    True, если что-то изменилось (заметку нужно перезаписать). Заметку на диск НЕ
    пишет — это делает вызывающий."""
    changed = False
    for att in note.attachments:
        if att.sha256:
            continue
        legacy = attachments_dir(note.folder_id, note.id) / att.file
        if not legacy.exists():
            continue  # файл пропал — оставляем без sha256 (покажется «отсутствует»)
        sha = _sha256_file(legacy)
        blob = blob_path(sha)
        if not blob.exists():
            write_blob(sha, legacy.read_bytes())  # пишет (за)шифрованно через crypto_fs
        # blob подтверждён (читается и sha совпал) — legacy можно убрать. Проверка по
        # содержимому, а не по размеру: у зашифрованного blob размер отличается.
        if blob.exists() and _verify_blob(sha):
            att.sha256 = sha
            legacy.unlink()
            changed = True
    # подчистить опустевшую legacy-папку
    adir = _notes_dir(note.folder_id) / "attachments" / note.id
    if adir.exists() and not any(adir.iterdir()):
        adir.rmdir()
    return changed


# --- расшифровка блобов «на доступ» (для UI: шифртекст нельзя отдать в QPixmap/плеер) ---

def _blob_cache_dir() -> Path:
    return config.tmpfs_dir("qtnotes-blobs")


def _decrypt_blob_to_cache(sha256: str, enc_path: Path, att) -> Path | None:
    """Расшифровать blob в tmpfs-файл и вернуть его путь (кэшируется по sha).

    None — если расшифровка не удалась (нет ключа/повреждение/целостность не сошлась).
    ВАЖНО: НИКОГДА не возвращаем путь к шифртексту — иначе UI скормит его QPixmap/плееру/
    openUrl и покажет мусор. Пусть лучше будет явная плашка «недоступно»."""
    ext = Path(att.name or att.file or "").suffix
    out = _blob_cache_dir() / f"{sha256}{ext}"
    if out.exists():
        return out
    try:
        data = crypto_fs.read_bytes(enc_path)
    except Exception:  # noqa: BLE001 — нет ключа/повреждение: деградируем без краха UI
        return None
    if data is None or hashlib.sha256(data).hexdigest() != sha256:
        return None  # не расшифровали/целостность не сошлась
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, out)
    return out


def attachment_access_path(note, att) -> Path | None:
    """Путь к вложению, пригодный для ПРЯМОГО чтения UI (QPixmap/QMovie/плеер/внешнее
    открытие). Если blob зашифрован — расшифровывает в tmpfs-кэш и возвращает его путь;
    иначе возвращает обычный путь без копирования (поведение прежнее).

    None — только когда blob зашифрован, но расшифровать не удалось: вызывающий обязан
    показать «недоступно», а НЕ передавать шифртекст в просмотрщик."""
    p = attachment_abspath(note, att)
    if att.sha256 and crypto_fs.is_encrypted_file(p):
        return _decrypt_blob_to_cache(att.sha256, p, att)
    return p


def _referenced_blob_shas() -> set[str] | None:
    """sha256 всех блобов, на которые ссылается хотя бы одна заметка или настройка
    (обои). Возвращает None, если разметка НЕПОЛНА (нечитаемая заметка при включённом
    шифровании) — тогда GC обязан воздержаться, иначе сметёт нужный блоб."""
    refs: set[str] = set()
    base = config.folders_dir()
    if base.exists():
        for folder_entry in base.iterdir():
            if not folder_entry.is_dir():
                continue
            ndir = folder_entry / "notes"
            if not ndir.exists():
                continue
            for entry in ndir.glob("*.json"):
                data = _read_json(entry)
                if data is None:
                    return None  # не смогли прочитать заметку — разметка неполна
                for att in data.get("attachments", []):
                    sha = att.get("sha256")
                    if sha:
                        refs.add(sha)
    w = list_shared().get("wallpaper")
    if w:
        refs.add(w)
    return refs


def gc_blobs(min_age_seconds: float = 60.0) -> int:
    """mark-and-sweep: удалить блобы, на которые не ссылается ни заметка, ни настройка.

    Безопасность:
    - при включённом шифровании без разблокировки — НЕ запускаем (заметки нечитаемы);
    - если хоть одна заметка не прочиталась — воздерживаемся (неполная разметка);
    - блобы моложе `min_age_seconds` пропускаем: их мог только что докачать синк для
      ещё не применённой заметки (гонка mark/sweep между потоками UI и синка)."""
    import time
    if config.encryption_enabled() and not _session_unlocked():
        return 0
    refs = _referenced_blob_shas()
    if refs is None:
        return 0
    bdir = config.blobs_dir()
    if not bdir.exists():
        return 0
    now = time.time()
    removed = 0
    for entry in bdir.iterdir():
        if not entry.is_file() or entry.name.endswith(".tmp") or entry.name in refs:
            continue
        try:
            if now - entry.stat().st_mtime < min_age_seconds:
                continue  # свежий — возможно, в полёте
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _session_unlocked() -> bool:
    from ..crypto import session
    return session.is_unlocked()


def wipe_blob_cache() -> None:
    """Удалить tmpfs-кэш расшифрованных блобов (при блокировке/выходе). Защита от
    случайного удаления vault — как у index.wipe_ephemeral."""
    try:
        d = _blob_cache_dir()
        if d.resolve() != config.vault_dir().resolve() and "qtnotes-blobs-" in d.name:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:  # pragma: no cover
        pass


# --- папки ---

def list_folders() -> list[Folder]:
    folders: list[Folder] = []
    base = config.folders_dir()
    for entry in base.iterdir():
        if not entry.is_dir():
            continue
        data = _read_json(entry / "folder.json")
        if data:
            folders.append(Folder.from_dict(data))
    folders.sort(key=lambda f: (f.order, f.created))
    return folders


def save_folder(folder: Folder) -> None:
    _write_json(_folder_json(folder.id), folder.as_dict())
    _log("folder.put", folder.id, folder.as_dict())


def create_folder(name: str, caption: str = "", color: str | None = None,
                  icon: str = "letter") -> Folder:
    order = len(list_folders())
    folder = Folder.create(name=name, caption=caption, color=color, icon=icon, order=order)
    save_folder(folder)
    return folder


def delete_folder(folder_id: str) -> None:
    path = _folder_path(folder_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    index.drop_folder(folder_id)
    _log("folder.del", folder_id, None)
    gc_blobs()  # папка унесла свои заметки → подобрать осиротевшие блобы


# --- заметки ---

def list_notes(folder_id: str) -> list[Note]:
    notes: list[Note] = []
    ndir = _notes_dir(folder_id)
    if not ndir.exists():
        return notes
    for entry in ndir.glob("*.json"):
        data = _read_json(entry)
        if data:
            notes.append(Note.from_dict(data))
    notes.sort(key=lambda n: n.created)
    return notes


def save_note(note: Note) -> None:
    # вложения переводятся в blob-стор ДО записи/лога: при синке — чтобы note.put нёс
    # sha256 (пир докачает blob); при шифровании — чтобы вложение легло в зашифрованный
    # blob, а не plaintext-файлом в attachments/.
    if config.sync_enabled() or config.encryption_enabled():
        ensure_blobs(note)
    _write_json(_note_json(note.folder_id, note.id), note.as_dict())
    index.sync_note(note)
    _log("note.put", note.id, note.as_dict())


def delete_note(note: Note) -> None:
    jp = _note_json(note.folder_id, note.id)
    if jp.exists():
        jp.unlink()
    adir = _notes_dir(note.folder_id) / "attachments" / note.id
    if adir.exists():
        shutil.rmtree(adir, ignore_errors=True)
    index.drop_note(note.id)
    _log("note.del", note.id, None)


def move_note(note: Note, target_folder_id: str) -> None:
    """Перенести заметку (json + вложения) в другую папку."""
    if target_folder_id == note.folder_id:
        return
    old_folder = note.folder_id
    old_json = _note_json(old_folder, note.id)
    old_att = _notes_dir(old_folder) / "attachments" / note.id

    note.folder_id = target_folder_id
    save_note(note)  # пишет в новую папку

    # перенести вложения
    if old_att.exists():
        new_att = attachments_dir(target_folder_id, note.id)
        for child in old_att.iterdir():
            shutil.move(str(child), str(new_att / child.name))
        shutil.rmtree(old_att, ignore_errors=True)

    if old_json.exists():
        old_json.unlink()


def find_note(note_id: str):
    """Найти заметку по id. Сначала — lookup в индексе (O(1)); при промахе —
    полный скан по файлам с досинхронизацией индекса (самовосстановление)."""
    fid = index.folder_of(note_id)
    if fid:
        data = _read_json(_note_json(fid, note_id))
        if data:
            return Note.from_dict(data)
    # промах/устаревший индекс — скан по файлам
    base = config.folders_dir()
    for folder_entry in base.iterdir():
        if not folder_entry.is_dir():
            continue
        jp = folder_entry / "notes" / f"{note_id}.json"
        if jp.exists():
            data = _read_json(jp)
            if data:
                note = Note.from_dict(data)
                index.sync_note(note)  # чиним индекс на будущее
                return note
    return None


# --- события календаря ---

def _events_path() -> Path:
    return config.calendar_dir() / "events.json"


def list_events() -> list[Event]:
    data = _read_json(_events_path())
    if not isinstance(data, list):
        return []
    return [Event.from_dict(d) for d in data]


def _save_events(events: list[Event]) -> None:
    crypto_fs.write_json(_events_path(), [e.as_dict() for e in events])


def add_event(date: str, name: str, color: str) -> Event:
    with _agg_lock:
        events = list_events()
        ev = Event.create(date=date, name=name, color=color)
        events.append(ev)
        _save_events(events)
    _log("event.put", ev.id, ev.as_dict())
    return ev


def delete_event(event_id: str) -> None:
    with _agg_lock:
        events = [e for e in list_events() if e.id != event_id]
        _save_events(events)
    _log("event.del", event_id, None)


def update_event(event_id: str, *, name: str | None = None,
                 color: str | None = None, date: str | None = None) -> None:
    """Обновить поля события (имя/цвет/дату). Переданные — заменяются."""
    with _agg_lock:
        events = list_events()
        updated = None
        for ev in events:
            if ev.id == event_id:
                if name is not None:
                    ev.name = name
                if color is not None:
                    ev.color = color
                if date is not None:
                    ev.date = date
                updated = ev
                break
        _save_events(events)
    if updated is not None:
        _log("event.put", updated.id, updated.as_dict())


# --- применение удалённых операций (БЕЗ записи в журнал) ---
# Используются sync/apply.py. Меняют файлы+индекс, но НЕ пишут новую op, иначе
# приём чужого изменения породил бы бесконечную ретрансляцию.

def apply_note_put(note: Note) -> None:
    """Записать заметку из удалённой op. Учитывает перенос между папками."""
    existing = find_note(note.id)
    if existing is not None and existing.folder_id != note.folder_id:
        # заметка переехала в другую папку — убрать старый файл
        old = _note_json(existing.folder_id, note.id)
        if old.exists():
            old.unlink()
        old_att = _notes_dir(existing.folder_id) / "attachments" / note.id
        if old_att.exists():
            shutil.rmtree(old_att, ignore_errors=True)
    _write_json(_note_json(note.folder_id, note.id), note.as_dict())
    index.sync_note(note)


def apply_note_del(note_id: str) -> None:
    existing = find_note(note_id)
    if existing is not None:
        jp = _note_json(existing.folder_id, note_id)
        if jp.exists():
            jp.unlink()
        adir = _notes_dir(existing.folder_id) / "attachments" / note_id
        if adir.exists():
            shutil.rmtree(adir, ignore_errors=True)
    index.drop_note(note_id)


def apply_folder_put(folder: Folder) -> None:
    _write_json(_folder_json(folder.id), folder.as_dict())


def apply_folder_del(folder_id: str) -> None:
    path = _folder_path(folder_id)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    index.drop_folder(folder_id)


def apply_event_put(event: Event) -> None:
    with _agg_lock:
        events = [e for e in list_events() if e.id != event.id]
        events.append(event)
        _save_events(events)


def apply_event_del(event_id: str) -> None:
    with _agg_lock:
        events = [e for e in list_events() if e.id != event_id]
        _save_events(events)


# --- общие (синхронизируемые) настройки: тема, обои и т.п. ---

def _shared_path() -> Path:
    return config.vault_dir() / "shared.json"


def list_shared() -> dict:
    data = _read_json(_shared_path())
    return data if isinstance(data, dict) else {}


def get_shared(key: str):
    return list_shared().get(key)


def set_shared(key: str, value) -> None:
    """Локально задать общую настройку (и записать op для синка)."""
    with _agg_lock:
        data = list_shared()
        data[key] = value
        _write_json(_shared_path(), data)
    _log("setting.put", key, value)


def apply_setting_put(key: str, value) -> None:
    with _agg_lock:
        data = list_shared()
        data[key] = value
        _write_json(_shared_path(), data)


def apply_setting_del(key: str) -> None:
    with _agg_lock:
        data = list_shared()
        if key in data:
            del data[key]
            _write_json(_shared_path(), data)
