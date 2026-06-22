"""Надёжная запись файлов: атомарность + durability (fsync).

`os.replace(tmp, path)` атомарен на уровне ФС (читатель видит либо старый, либо новый
файл целиком), но БЕЗ fsync содержимое tmp может не дойти до диска до rename: при потере
питания сразу после записи файл окажется пустым/битым. Поэтому перед rename мы делаем
fsync файла, а после — fsync каталога (чтобы запись самого rename тоже была durable).

Эта функция — единственная точка записи контента хранилища (через crypto_fs) и состояния
keyring, чтобы гарантии действовали везде одинаково.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def fsync_dir(d: Path) -> None:
    """fsync каталога — фиксирует запись rename/создание файла. Best-effort: некоторые
    ФС/каталоги не поддерживают fsync каталога, это не критично."""
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
    except OSError:
        pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Атомарно и durably записать `data` в `path`.

    tmp создаётся УНИКАЛЬНЫМ (mkstemp, O_EXCL) рядом с целью — иначе два потока (UI-правка
    и применение входящего синка) писали бы в один `<name>.tmp` и портили друг друга /
    ловили ENOENT на rename. Содержимое fsync-ается до rename, каталог — после. tmp того
    же каталога → rename атомарен и без копирования между ФС."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmpname)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
