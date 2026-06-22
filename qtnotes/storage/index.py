"""SQLite-индекс заметок: быстрый поиск и поиск заметки по id.

Индекс — это КЭШ, выводимый из файлов хранилища, а не источник правды.
Его можно удалить и перестроить из JSON (`rebuild`). Лежит в корне vault
(`index.sqlite`), поэтому в экспорт-архив не попадает — экспортируются только
`folders/` и `calendar/`.

Назначение:
  * глобальный поиск без чтения всех JSON-файлов (одна выборка из SQLite);
  * `folder_of(id)` за один lookup — переход по ссылкам [[id]] без скана диска;
  * ограничение latency на больших объёмах через предфильтр FTS5.

Все операции изменения заметок в vault.py обязаны звать `sync_note`/`drop_note`/
`drop_folder`, иначе индекс разъедется с диском. `ensure_ready()` при старте
перестраивает индекс, если он ещё не построен (например, после обновления или
импорта). Индекс — кэш: при любой ошибке записи приложение продолжает работать,
а рассинхронизация лечится перестройкой.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

from .. import config

# Индекс трогают ДВА потока: UI (правки/поиск) и поток движка синка (применение
# чужих ops через vault.apply_*). Даём каждому потоку СВОЁ соединение (threading.local)
# — в режиме WAL несколько соединений к одному файлу безопасны (busy_timeout ждёт).
_local = threading.local()

SCHEMA_VERSION = 2
# Ссылка на заметку в тексте: [[<32 hex>]] (тот же формат, что в textutils)
_REF_RE = re.compile(r"\[\[([0-9a-fA-F]{32})\]\]")
# Выше этого числа заметок полный нечёткий скан становится дорогим — сужаем
# множество кандидатов префиксным поиском FTS5 (ценой устойчивости к опечаткам
# в очень больших коллекциях; на типичных объёмах сканируем всё).
FUZZY_SCAN_CAP = 3000
_FTS_CANDIDATE_LIMIT = 5000

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notes (
    id        TEXT PRIMARY KEY,
    folder_id TEXT NOT NULL,
    created   TEXT NOT NULL DEFAULT '',
    modified  TEXT NOT NULL DEFAULT '',
    date_tag  TEXT,
    plaintext TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_notes_folder ON notes(folder_id);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    plaintext,
    id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS refs (
    src_id TEXT NOT NULL,   -- заметка, содержащая ссылку
    dst_id TEXT NOT NULL    -- заметка, на которую ссылаются
);
CREATE INDEX IF NOT EXISTS idx_refs_dst ON refs(dst_id);
CREATE INDEX IF NOT EXISTS idx_refs_src ON refs(src_id);
"""


def _extract_refs(text: str, self_id: str) -> set[str]:
    """Идентификаторы заметок, на которые ссылается текст (без ссылки на себя)."""
    return {m.group(1).lower() for m in _REF_RE.finditer(text or "")} - {self_id}

_ROW_COLS = "id, folder_id, created, date_tag, plaintext"

def _db() -> sqlite3.Connection:
    # соединение кэшируется per-thread (путь к vault может меняться — тогда переоткрываем)
    path = str(config.index_path())
    con = getattr(_local, "con", None)
    if con is not None and getattr(_local, "path", None) == path:
        return con
    if con is not None:
        try:
            con.close()
        except sqlite3.Error:
            pass
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=5000")
    _ensure_schema(con)
    _local.con = con
    _local.path = path
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        # схема устарела — сносим и пересоздаём (данные перестроятся из файлов)
        con.executescript(
            "DROP TABLE IF EXISTS refs;"
            "DROP TABLE IF EXISTS notes_fts;"
            "DROP TABLE IF EXISTS notes;"
            "DROP TABLE IF EXISTS meta;"
        )
        con.executescript(_CREATE_SQL)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
    else:
        con.executescript(_CREATE_SQL)


# --- запись (синхронизация из vault) ---

def sync_note(note) -> None:
    """Вставить/обновить заметку в индексе. Best-effort: ошибки не валят app."""
    try:
        con = _db()
        con.execute(
            "INSERT INTO notes(id, folder_id, created, modified, date_tag, plaintext)"
            " VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            "   folder_id=excluded.folder_id, created=excluded.created,"
            "   modified=excluded.modified, date_tag=excluded.date_tag,"
            "   plaintext=excluded.plaintext",
            (note.id, note.folder_id, note.created or "", note.modified or "",
             note.date_tag, note.plaintext or ""),
        )
        con.execute("DELETE FROM notes_fts WHERE id=?", (note.id,))
        con.execute("INSERT INTO notes_fts(plaintext, id) VALUES(?, ?)",
                    (note.plaintext or "", note.id))
        # обратные ссылки: перечитываем исходящие [[id]] этой заметки
        con.execute("DELETE FROM refs WHERE src_id=?", (note.id,))
        refs = _extract_refs(note.plaintext, note.id)
        if refs:
            con.executemany("INSERT INTO refs(src_id, dst_id) VALUES(?, ?)",
                            [(note.id, dst) for dst in refs])
        con.commit()
    except sqlite3.Error as e:  # pragma: no cover
        print(f"[index] sync_note failed: {e}")


def drop_note(note_id: str) -> None:
    try:
        con = _db()
        con.execute("DELETE FROM notes WHERE id=?", (note_id,))
        con.execute("DELETE FROM notes_fts WHERE id=?", (note_id,))
        con.execute("DELETE FROM refs WHERE src_id=?", (note_id,))
        con.commit()
    except sqlite3.Error as e:  # pragma: no cover
        print(f"[index] drop_note failed: {e}")


def drop_folder(folder_id: str) -> None:
    try:
        con = _db()
        con.execute("DELETE FROM notes_fts WHERE id IN"
                    " (SELECT id FROM notes WHERE folder_id=?)", (folder_id,))
        con.execute("DELETE FROM refs WHERE src_id IN"
                    " (SELECT id FROM notes WHERE folder_id=?)", (folder_id,))
        con.execute("DELETE FROM notes WHERE folder_id=?", (folder_id,))
        con.commit()
    except sqlite3.Error as e:  # pragma: no cover
        print(f"[index] drop_folder failed: {e}")


# --- чтение ---

def folder_of(note_id: str) -> str | None:
    """В какой папке лежит заметка (по индексу). None — если не найдена."""
    try:
        row = _db().execute("SELECT folder_id FROM notes WHERE id=?",
                            (note_id,)).fetchone()
        return row["folder_id"] if row else None
    except sqlite3.Error:  # pragma: no cover
        return None


def referrers(dst_id: str) -> list[str]:
    """ID заметок, которые ссылаются ([[dst_id]]) на заметку dst_id."""
    try:
        rows = _db().execute(
            "SELECT DISTINCT src_id FROM refs WHERE dst_id=?", (dst_id,)).fetchall()
        return [r["src_id"] for r in rows]
    except sqlite3.Error:  # pragma: no cover
        return []


def date_rows(date_str: str, folder_id: str | None = None) -> list[sqlite3.Row]:
    """Заметки, привязанные к дате (date_tag) или созданные в этот день."""
    con = _db()
    sql = (f"SELECT {_ROW_COLS} FROM notes"
           " WHERE (date_tag=? OR created LIKE ?)")
    params: list = [date_str, date_str + "%"]
    if folder_id:
        sql += " AND folder_id=?"
        params.append(folder_id)
    return con.execute(sql, params).fetchall()


def _fts_match(query: str) -> str | None:
    """Собрать безопасный MATCH-запрос FTS5: префиксный поиск по словам."""
    tokens = re.findall(r"\w+", query, re.UNICODE)
    if not tokens:
        return None
    # кавычки экранируют спецсимволы FTS5; '*' — префиксное совпадение
    return " OR ".join(f'"{t}"*' for t in tokens)


def candidate_rows(query: str, folder_id: str | None = None) -> list[sqlite3.Row]:
    """Строки-кандидаты для нечёткого ранжирования.

    На типичных объёмах возвращает все заметки области (полная устойчивость к
    опечаткам). На больших — сужает множество префиксным поиском FTS5.
    """
    con = _db()
    cnt_sql = "SELECT COUNT(*) FROM notes"
    cnt_params: list = []
    if folder_id:
        cnt_sql += " WHERE folder_id=?"
        cnt_params.append(folder_id)
    total = con.execute(cnt_sql, cnt_params).fetchone()[0]

    if total > FUZZY_SCAN_CAP:
        match = _fts_match(query)
        if match:
            sql = (f"SELECT {_ROW_COLS} FROM notes"
                   " WHERE id IN (SELECT id FROM notes_fts WHERE notes_fts MATCH ?)")
            params: list = [match]
            if folder_id:
                sql += " AND folder_id=?"
                params.append(folder_id)
            sql += f" LIMIT {_FTS_CANDIDATE_LIMIT}"
            try:
                return con.execute(sql, params).fetchall()
            except sqlite3.Error:  # pragma: no cover — кривой MATCH → полный скан
                pass

    sql = f"SELECT {_ROW_COLS} FROM notes"
    params = []
    if folder_id:
        sql += " WHERE folder_id=?"
        params.append(folder_id)
    sql += f" LIMIT {_FTS_CANDIDATE_LIMIT}"
    return con.execute(sql, params).fetchall()


# --- перестройка из файлов ---

def _is_built(con: sqlite3.Connection) -> bool:
    row = con.execute("SELECT value FROM meta WHERE key='built'").fetchone()
    return bool(row and row["value"] == "1")


def rebuild() -> None:
    """Перестроить индекс с нуля, прочитав все заметки из файлов хранилища."""
    from . import vault  # ленивый импорт: vault импортирует index
    con = _db()
    try:
        con.execute("DELETE FROM notes")
        con.execute("DELETE FROM notes_fts")
        con.execute("DELETE FROM refs")
        rows = []
        fts = []
        refs = []
        base = config.folders_dir()
        if base.exists():
            for folder_entry in base.iterdir():
                if not folder_entry.is_dir():
                    continue
                folder_id = folder_entry.name
                for note in vault.list_notes(folder_id):
                    rows.append((note.id, note.folder_id, note.created or "",
                                 note.modified or "", note.date_tag,
                                 note.plaintext or ""))
                    fts.append((note.plaintext or "", note.id))
                    refs.extend((note.id, dst)
                                for dst in _extract_refs(note.plaintext, note.id))
        con.executemany(
            "INSERT INTO notes(id, folder_id, created, modified, date_tag, plaintext)"
            " VALUES(?,?,?,?,?,?)", rows)
        con.executemany("INSERT INTO notes_fts(plaintext, id) VALUES(?, ?)", fts)
        con.executemany("INSERT INTO refs(src_id, dst_id) VALUES(?, ?)", refs)
        con.execute("INSERT INTO meta(key, value) VALUES('built','1')"
                    " ON CONFLICT(key) DO UPDATE SET value='1'")
        con.commit()
    except sqlite3.Error as e:  # pragma: no cover
        print(f"[index] rebuild failed: {e}")
        con.rollback()


def ensure_ready() -> None:
    """Если индекс ещё не построен — построить. Вызывается при старте app."""
    con = _db()
    if not _is_built(con):
        rebuild()


def reset_for_tests() -> None:
    """Сбросить кэш соединения текущего потока (тесты переключают QTNOTES_VAULT)."""
    con = getattr(_local, "con", None)
    if con is not None:
        try:
            con.close()
        except sqlite3.Error:
            pass
    _local.con = None
    _local.path = None


def wipe_ephemeral() -> None:
    """Удалить эфемерный (tmpfs) индекс. Зовётся при блокировке/выходе, когда
    шифрование включено, чтобы plaintext-кэш индекса не пережил сессию.

    Защита: каталог удаляется только если это НЕ сам vault (эфемерный индекс лежит
    в /dev/shm|XDG_RUNTIME_DIR|tmp с префиксом qtnotes-index-)."""
    import shutil

    reset_for_tests()  # закрыть соединение текущего потока
    try:
        d = config._ephemeral_index_dir()
        if d.resolve() != config.vault_dir().resolve() and "qtnotes-index-" in d.name:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:  # pragma: no cover
        pass
