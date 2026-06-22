"""Миграция существующего хранилища в зашифрованный формат (на месте).

Вызывается при включении шифрования, чтобы зашифровать НЕ только новые, но и уже
существующие данные. Безопасна:
- идемпотентна и устойчива к прерыванию: файлы читаются через crypto_fs (понимает и
  plaintext, и шифр), пишутся зашифрованными; повторный прогон до-шифрует остаток;
- перед миграцией делается zip-бэкап данных vault (для отката).

Требует: encryption_enabled() == True и session разблокирован (MK в памяти).
"""

from __future__ import annotations

import time
import zipfile
from pathlib import Path

from .. import config
from ..crypto import session
from ..crypto.errors import VaultLockedError
from . import crypto_fs, index, vault

# Данные vault, подлежащие бэкапу/миграции (index — перестраиваемый, не входит).
_DATA_ITEMS = ["folders", "calendar", "blobs", "shared.json", "sync.sqlite"]


def _require_unlocked() -> None:
    if not config.encryption_enabled() or not session.is_unlocked():
        raise VaultLockedError("миграция требует включённого шифрования и разблокировки")


def _backup_dir() -> Path:
    """Каталог временных бэкапов миграции — в config (owned_paths → стирается duress'ом),
    а НЕ рядом с vault (там плейнтекст пережил бы duress-стирание)."""
    return config._xdg_config_home() / config.APP_DIR_NAME / "migration-backup"


def cleanup_backup(path: Path | str | None) -> None:
    """Удалить временный бэкап миграции (после успешной перешифровки плейнтекст-копия
    больше не нужна и не должна лежать на диске). Безопасно: только внутри owned-каталога."""
    if not path:
        return
    p = Path(path)
    try:
        if p.resolve().parent == _backup_dir().resolve() and p.is_file():
            p.unlink(missing_ok=True)
    except OSError:
        pass


def backup_zip(dest: Path | None = None) -> Path:
    """Сделать zip-бэкап данных vault (для отката). Возвращает путь к архиву.
    Бэкапит ТОЛЬКО данные QtNotes (не всю папку vault — она может быть личной).

    По умолчанию пишется в config/migration-backup (owned_paths → erasable), удаляется
    после успешной миграции (cleanup_backup)."""
    v = config.vault_dir()
    if dest is None:
        bdir = _backup_dir()
        bdir.mkdir(parents=True, exist_ok=True)
        dest = bdir / f"{v.name}-backup-{int(time.time())}.zip"
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for name in _DATA_ITEMS:
            p = v / name
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(v))
            elif p.is_file():
                z.write(p, p.relative_to(v))
    return dest


def migrate_encrypt() -> dict:
    """Зашифровать все существующие plaintext-данные на месте. Возвращает статистику."""
    _require_unlocked()
    stats = {"folders": 0, "notes": 0, "blobs": 0, "events": 0, "shared": 0, "ops": 0}

    # 1) папки + заметки (+ legacy-вложения → зашифрованные blobs через ensure_blobs)
    for folder in vault.list_folders():
        crypto_fs.write_json(vault._folder_json(folder.id), folder.as_dict())
        stats["folders"] += 1
        for note in vault.list_notes(folder.id):
            vault.ensure_blobs(note)  # legacy-вложения → blobs (теперь шифруются)
            crypto_fs.write_json(vault._note_json(folder.id, note.id), note.as_dict())
            stats["notes"] += 1

    # 2) blobs: перешифровать любые ещё-plaintext блобы на месте
    bdir = config.blobs_dir()
    if bdir.exists():
        for p in bdir.iterdir():
            if not p.is_file() or p.suffix == ".tmp":
                continue
            if crypto_fs.is_encrypted_file(p):
                continue  # уже зашифрован
            data = crypto_fs.read_bytes(p)
            if data is not None:
                crypto_fs.write_bytes(p, data)
                stats["blobs"] += 1

    # 3) события + общие настройки
    ev = vault._events_path()
    if ev.exists():
        data = crypto_fs.read_json(ev)
        if data is not None:
            crypto_fs.write_json(ev, data)
            stats["events"] = 1
    sh = vault._shared_path()
    if sh.exists():
        data = crypto_fs.read_json(sh)
        if data is not None:
            crypto_fs.write_json(sh, data)
            stats["shared"] = 1

    # 4) oplog: перешифровать payload'ы
    if config.sync_db_path().exists():
        from ..sync import oplog
        stats["ops"] = oplog.reencrypt_payloads()

    # 5) удалить устаревший plaintext-индекс в vault (теперь индекс живёт в tmpfs) и
    # перестроить из расшифрованных заметок
    for suffix in ("", "-wal", "-shm"):
        stale = config.vault_dir() / f"index.sqlite{suffix}"
        stale.unlink(missing_ok=True)
    index.reset_for_tests()
    index.ensure_ready()

    return stats
