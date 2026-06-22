"""Журнал операций (op-log) для синхронизации.

Каждое изменение сущности (заметка/папка/событие) порождает op с логическими
часами Лампорта и device_id. Op самодостаточен: payload для `*.put` — полный JSON
сущности, поэтому пир применяет его без дополнительного контекста (LWW по modified).

Версионный вектор `vv` = {device_id: max_lamport}. Синхронизация: пир сообщает свой
vv, мы отдаём `ops_since(vv)` — всё, чего у него ещё нет. Входящие ops пишутся через
`record_remote` (для хранения/ретрансляции и продвижения vv); их ПРИМЕНЕНИЕ к файлам
делает sync/apply.py.

Журнал — отдельная БД `<vault>/sync.sqlite` (в экспорт не входит). Это механизм:
включение/выключение синхронизации решает vault (через config.sync_enabled()).
"""

from __future__ import annotations

import json
import sqlite3
import threading

from .. import config
from ..crypto import primitives as P
from ..crypto import session, valuecrypt
from ..storage.models import now_iso
from . import identity

SCHEMA_VERSION = 1

# Контексты вывода субключей шифрования колонок (aad=op_id привязывает к строке).
# payload — текст заметок; P13: kind/entity_id/wall — метаданные (какая сущность, какого
# типа операция, когда) — тоже шифруем at-rest, чтобы образ sync.sqlite не выдавал
# активность. device_id/lamport/op_id остаются открытыми: нужны для version-vector и
# ops_since (и device_id/lamport всё равно содержатся в op_id="did:lamport").
_PAYLOAD_INFO = b"oplog/payload/v1"
_KIND_INFO = b"oplog/kind/v1"
_ENTITY_INFO = b"oplog/entity/v1"
_WALL_INFO = b"oplog/wall/v1"
# Tombstones (H4): антивоскрешение. На каждый *.del храним «часы удаления»
# (wall, lamport, device_id); входящий *.put подавляется, если удаление не старше его.
# ekey — ключ поиска: keyed-hash под MK при шифровании (не выдаёт entity_id в образе
# БД, как и прочая мета P13), иначе сам entity_id. Колонка wall шифруется (aad=ekey).
_TOMB_INFO = b"oplog/tomb-wall/v1"
_TOMB_KEY_INFO = b"oplog/tomb-key/v1"


def _encrypting() -> bool:
    return config.encryption_enabled() and session.is_unlocked()


def _tomb_ekey(entity_id: str) -> str:
    """Ключ поиска tombstone. При шифровании — HMAC(HKDF(MK), entity_id) в hex; иначе
    сам entity_id (детерминирован → одинаков для del и последующего put)."""
    if _encrypting():
        k = P.hkdf(session.get_master_key(), info=_TOMB_KEY_INFO)
        return P.hmac_sha256(k, entity_id.encode("utf-8")).hex()
    return entity_id


def _store_payload(payload: dict | None, op_id: str) -> str | None:
    """Сериализовать payload для хранения в БД, зашифровав при включённом шифровании.
    aad=op_id привязывает шифртекст к строке журнала."""
    if payload is None:
        return None
    js = json.dumps(payload, ensure_ascii=False)
    return valuecrypt.seal_str(js, info=_PAYLOAD_INFO, aad=op_id.encode("utf-8"))


def _load_payload(stored: str | None, op_id: str) -> dict | None:
    """Обратное к _store_payload: расшифровать (если нужно) и распарсить JSON."""
    if stored is None:
        return None
    js = valuecrypt.open_str(stored, info=_PAYLOAD_INFO, aad=op_id.encode("utf-8"))
    return json.loads(js)


def _enc(value: str, info: bytes, op_id: str) -> str:
    """Зашифровать строковую колонку при включённом шифровании (иначе plaintext)."""
    return valuecrypt.seal_str(value, info=info, aad=op_id.encode("utf-8"))


def _dec(stored: str, info: bytes, op_id: str) -> str:
    """Расшифровать строковую колонку (понимает plaintext и шифртекст — backward-compat)."""
    return valuecrypt.open_str(stored, info=info, aad=op_id.encode("utf-8"))

# Журнал используют ДВА потока: UI (локальные правки через vault._log) и поток
# движка (приём/применение чужих ops). Поэтому соединение check_same_thread=False,
# а доступ сериализуется этим локом.
_lock = threading.RLock()

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ops (
    op_id     TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    lamport   INTEGER NOT NULL,
    wall      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ops_dev_lam ON ops(device_id, lamport);
CREATE TABLE IF NOT EXISTS clock (
    device_id   TEXT PRIMARY KEY,
    max_lamport INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tombstones (
    ekey      TEXT PRIMARY KEY,
    wall      TEXT NOT NULL,
    lamport   INTEGER NOT NULL,
    device_id TEXT NOT NULL
);
"""

_con: sqlite3.Connection | None = None
_con_path: str | None = None
_local_id: str | None = None
_change_listener = None   # вызывается после локальной op (движок → push-on-change)


def set_change_listener(fn) -> None:
    """Зарегистрировать колбэк, вызываемый после каждой локальной op.

    Движок ставит сюда уведомление о пуше. Развязывает vault и движок: запись в
    хранилище не зависит от UI/сети."""
    global _change_listener
    _change_listener = fn


def _db() -> sqlite3.Connection:
    global _con, _con_path
    path = str(config.sync_db_path())
    if _con is not None and _con_path == path:
        return _con
    if _con is not None:
        try:
            _con.close()
        except sqlite3.Error:
            pass
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_CREATE_SQL)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()
    _con, _con_path = con, path
    return con


def local_device_id() -> str:
    global _local_id
    if _local_id is None:
        _local_id = identity.ensure_identity().device_id
    return _local_id


# --- запись ---

def _next_lamport(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT MAX(max_lamport) FROM clock").fetchone()
    base = row[0] if row and row[0] is not None else 0
    return base + 1


def _bump_clock(con: sqlite3.Connection, device_id: str, lamport: int) -> None:
    con.execute(
        "INSERT INTO clock(device_id, max_lamport) VALUES(?, ?)"
        " ON CONFLICT(device_id) DO UPDATE SET"
        " max_lamport=MAX(max_lamport, excluded.max_lamport)",
        (device_id, lamport),
    )


def _clock_tuple(wall: str, lamport: int, device_id: str) -> tuple[str, int, str]:
    return (wall or "", int(lamport or 0), device_id or "")


def _record_tombstone(con: sqlite3.Connection, entity_id: str, wall: str,
                      lamport: int, device_id: str) -> None:
    """Запомнить «часы удаления» сущности (для подавления устаревших put)."""
    ekey = _tomb_ekey(entity_id)
    incoming = _clock_tuple(wall, lamport, device_id)
    row = con.execute(
        "SELECT wall, lamport, device_id FROM tombstones WHERE ekey=?", (ekey,)).fetchone()
    if row is not None:
        cur_wall = valuecrypt.open_str(row["wall"], info=_TOMB_INFO, aad=ekey.encode())
        if not incoming > _clock_tuple(cur_wall, row["lamport"], row["device_id"]):
            return  # уже есть не более старое удаление — оставляем его
    con.execute(
        "INSERT INTO tombstones(ekey, wall, lamport, device_id) VALUES(?,?,?,?)"
        " ON CONFLICT(ekey) DO UPDATE SET"
        " wall=excluded.wall, lamport=excluded.lamport, device_id=excluded.device_id",
        (ekey, valuecrypt.seal_str(wall, info=_TOMB_INFO, aad=ekey.encode()),
         int(lamport), device_id))


def tombstone_for(entity_id: str) -> tuple[str, int, str] | None:
    """Часы удаления сущности (wall, lamport, device_id) или None."""
    with _lock:
        con = _db()
        ekey = _tomb_ekey(entity_id)
        row = con.execute(
            "SELECT wall, lamport, device_id FROM tombstones WHERE ekey=?", (ekey,)).fetchone()
        if row is None:
            return None
        wall = valuecrypt.open_str(row["wall"], info=_TOMB_INFO, aad=ekey.encode())
        return _clock_tuple(wall, row["lamport"], row["device_id"])


def append_local(kind: str, entity_id: str, payload: dict | None) -> str:
    """Записать локальную операцию. Возвращает op_id."""
    with _lock:
        con = _db()
        did = local_device_id()
        lam = _next_lamport(con)
        op_id = f"{did}:{lam}"
        wall = now_iso()
        con.execute(
            "INSERT OR REPLACE INTO ops(op_id, device_id, lamport, wall, kind, entity_id, payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (op_id, did, lam,
             _enc(wall, _WALL_INFO, op_id),
             _enc(kind, _KIND_INFO, op_id),
             _enc(entity_id, _ENTITY_INFO, op_id),
             _store_payload(payload, op_id)),
        )
        if kind.endswith(".del"):
            _record_tombstone(con, entity_id, wall, lam, did)
        _bump_clock(con, did, lam)
        con.commit()
        global _appends_since_compact
        _appends_since_compact += 1
    maybe_compact()  # амортизированная подрезка истории (вне основного лока коммита)
    if _change_listener is not None:
        try:
            _change_listener()
        except Exception:  # noqa: BLE001 — уведомление не должно ронять запись
            pass
    return op_id


def has_op(op_id: str) -> bool:
    """Есть ли уже такая операция (дедуп до применения, H5)."""
    with _lock:
        con = _db()
        return con.execute("SELECT 1 FROM ops WHERE op_id=?", (op_id,)).fetchone() is not None


def record_remote(op: dict) -> bool:
    """Сохранить операцию, полученную от пира. True — если новая (не было)."""
    with _lock:
        con = _db()
        op_id = op["op_id"]
        exists = con.execute("SELECT 1 FROM ops WHERE op_id=?", (op_id,)).fetchone()
        if exists:
            return False
        con.execute(
            "INSERT INTO ops(op_id, device_id, lamport, wall, kind, entity_id, payload)"
            " VALUES(?,?,?,?,?,?,?)",
            (op_id, op["device_id"], int(op["lamport"]),
             _enc(op["wall"], _WALL_INFO, op_id),
             _enc(op["kind"], _KIND_INFO, op_id),
             _enc(op["entity_id"], _ENTITY_INFO, op_id),
             _store_payload(op.get("payload"), op_id)),
        )
        if op["kind"].endswith(".del"):
            _record_tombstone(con, op["entity_id"], op["wall"],
                              int(op["lamport"]), op["device_id"])
        _bump_clock(con, op["device_id"], int(op["lamport"]))
        con.commit()
        return True


# --- чтение ---

def version_vector() -> dict[str, int]:
    with _lock:
        con = _db()
        return {r["device_id"]: r["max_lamport"]
                for r in con.execute("SELECT device_id, max_lamport FROM clock")}


def _row_to_op(r: sqlite3.Row) -> dict:
    op_id = r["op_id"]
    return {
        "op_id": op_id,
        "device_id": r["device_id"],
        "lamport": r["lamport"],
        "wall": _dec(r["wall"], _WALL_INFO, op_id),
        "kind": _dec(r["kind"], _KIND_INFO, op_id),
        "entity_id": _dec(r["entity_id"], _ENTITY_INFO, op_id),
        "payload": _load_payload(r["payload"], op_id),
    }


def ops_since(remote_vv: dict[str, int]) -> list[dict]:
    """Все ops, которых нет у пира с версионным вектором remote_vv.

    Для каждого устройства d отдаём ops с lamport > remote_vv[d] (или все, если
    устройства нет в векторе пира). Порядок — по lamport (применять удобнее)."""
    with _lock:
        con = _db()
        devices = [r["device_id"] for r in
                   con.execute("SELECT DISTINCT device_id FROM ops")]
        out: list[dict] = []
        for d in devices:
            th = int(remote_vv.get(d, 0))
            rows = con.execute(
                "SELECT * FROM ops WHERE device_id=? AND lamport>? ORDER BY lamport",
                (d, th))
            out.extend(_row_to_op(r) for r in rows)
        out.sort(key=lambda o: (o["lamport"], o["device_id"]))
        return out


def all_ops() -> list[dict]:
    with _lock:
        con = _db()
        return [_row_to_op(r) for r in
                con.execute("SELECT * FROM ops ORDER BY lamport, device_id")]


# --- компакция (B1, раунд-3) -------------------------------------------------
# Журнал хранит ПОЛНЫЙ снимок сущности на каждый put → без подрезки растёт без границ,
# и новый пир качает всю историю правок. Компакция оставляет по сущности только
# op-победителя (по той же логике, что apply.py: LWW по modified для заметок,
# (lamport,device) для папок/событий, tombstone для удалений) и удаляет доминируемые.
#
# БЕЗОПАСНОСТЬ (почему сходимость не ломается): таблицы `clock` (vv) и `tombstones`
# НЕ трогаются. ops_since(remote_vv) выдаёт пиру строки по его vv, и победитель —
# полный снимок, поэтому ЛЮБОЙ пир (свежий с пустым vv, отставший, актуальный) после
# применения оставшихся ops приходит к тому же финальному состоянию. Удалённый
# «проигравший» либо уже не нужен (победитель новее и тоже дойдёт), либо подавился бы
# при применении. Консервативно: сущность со смесью удаления и ПЕРЕЖИВШЕГО его put
# (воскрешение) не компактим — там тонкое взаимодействие с безусловным note.del.

_MOD_KEY = "modified"


def _best_put(puts: list[dict]) -> dict:
    """Победитель среди put: заметки — по (modified, lamport, device); папки/события
    (без modified) — по (lamport, device). Совпадает с apply._apply_note_put."""
    def key(o: dict):
        m = ((o.get("payload") or {}).get(_MOD_KEY)) or ""
        return (m, int(o.get("lamport") or 0), o.get("device_id") or "")
    return max(puts, key=key)


def _winner_or_none(ops: list[dict]) -> dict | None:
    """Единственный op-победитель сущности, или None если компактить нельзя
    (воскрешение: есть удаление и переживший его put)."""
    dels = [o for o in ops if o["kind"].endswith(".del")]
    puts = [o for o in ops if not o["kind"].endswith(".del")]
    if dels:
        tomb = max(_clock_tuple(o["wall"], o["lamport"], o["device_id"]) for o in dels)
        survivors = [o for o in puts
                     if not (tomb >= _clock_tuple(o["wall"], o["lamport"], o["device_id"]))]
        if survivors:
            return None  # воскрешение — консервативно не компактим
        # все put подавлены удалением → победитель = сильнейшее удаление
        return max(dels, key=lambda o: _clock_tuple(o["wall"], o["lamport"], o["device_id"]))
    return _best_put(puts) if puts else None


def compact() -> int:
    """Схлопнуть журнал до победителя на сущность. Возвращает число удалённых ops.
    Идемпотентно. clock/tombstones не трогаются (vv и антивоскрешение сохранны)."""
    from collections import defaultdict
    with _lock:
        con = _db()
        rows = [_row_to_op(r) for r in con.execute("SELECT * FROM ops")]
        by_entity: dict[str, list[dict]] = defaultdict(list)
        for op in rows:
            by_entity[op["entity_id"]].append(op)
        to_delete: list[str] = []
        for ops in by_entity.values():
            if len(ops) < 2:
                continue
            winner = _winner_or_none(ops)
            if winner is None:
                continue
            for op in ops:
                if op["op_id"] != winner["op_id"]:
                    to_delete.append(op["op_id"])
        for op_id in to_delete:
            con.execute("DELETE FROM ops WHERE op_id=?", (op_id,))
        if to_delete:
            con.commit()
        return len(to_delete)


# Авто-компакция: триггерится из append_local, когда журнал заметно превысил число
# сущностей. Порог с запасом, чтобы не гонять компакцию на каждую правку.
_appends_since_compact = 0
_COMPACT_EVERY = 200


def maybe_compact() -> int:
    """Компактить, если накопилось много локальных правок (амортизированно)."""
    global _appends_since_compact
    if _appends_since_compact < _COMPACT_EVERY:
        return 0
    _appends_since_compact = 0
    return compact()


def reencrypt_payloads() -> int:
    """Перешифровать payload И метаданные (wall/kind/entity_id) под текущий режим (для
    миграции при включении шифрования). Идемпотентно: _dec/_load_payload понимают plaintext
    и шифр, _enc/_store_payload пишут по текущему флагу. Возвращает число строк."""
    with _lock:
        con = _db()
        rows = con.execute(
            "SELECT op_id, wall, kind, entity_id, payload FROM ops").fetchall()
        n = 0
        for r in rows:
            op_id = r["op_id"]
            payload = (_store_payload(_load_payload(r["payload"], op_id), op_id)
                       if r["payload"] is not None else None)
            con.execute(
                "UPDATE ops SET wall=?, kind=?, entity_id=?, payload=? WHERE op_id=?",
                (_enc(_dec(r["wall"], _WALL_INFO, op_id), _WALL_INFO, op_id),
                 _enc(_dec(r["kind"], _KIND_INFO, op_id), _KIND_INFO, op_id),
                 _enc(_dec(r["entity_id"], _ENTITY_INFO, op_id), _ENTITY_INFO, op_id),
                 payload, op_id))
            n += 1
        con.commit()
        return n


def get_meta(key: str) -> str | None:
    with _lock:
        row = _db().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _lock:
        con = _db()
        con.execute("INSERT INTO meta(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        con.commit()


def reset_for_tests() -> None:
    """Сбросить кэш соединения и локальный id (тесты меняют vault/identity)."""
    global _con, _con_path, _local_id, _appends_since_compact
    if _con is not None:
        try:
            _con.close()
        except sqlite3.Error:
            pass
    _con, _con_path, _local_id = None, None, None
    _appends_since_compact = 0
