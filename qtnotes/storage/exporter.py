"""Экспорт и импорт данных в zip-архив — перенос между машинами.

Хранилище это обычные файлы, поэтому экспорт = zip папок `folders/` и
`calendar/`. Поисковый индекс не хранится (вычисляется на лету), настройки
приложения лежат вне хранилища — в архив не входят.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from .. import config

_EXPORT_SUBDIRS = ("folders", "calendar", "blobs")


def export_folder(folder_id: str, dest_zip: str | Path) -> Path:
    """Заархивировать одну папку заметок (включая её blob-вложения)."""
    from . import vault
    src = config.folders_dir() / folder_id
    dest = Path(dest_zip)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                arc = Path("folders") / folder_id / p.relative_to(src)
                z.write(p, str(arc))
        # blob-вложения этой папки лежат вне неё (vault/blobs) — добавить явно
        seen: set[str] = set()
        for note in vault.list_notes(folder_id):
            for att in note.attachments:
                if att.sha256 and att.sha256 not in seen:
                    seen.add(att.sha256)
                    bp = config.blobs_dir() / att.sha256
                    if bp.is_file():
                        z.write(bp, str(Path("blobs") / att.sha256))
    return dest


def export_all(dest_zip: str | Path) -> Path:
    """Заархивировать всё хранилище (папки + календарь)."""
    vault = config.vault_dir()
    dest = Path(dest_zip)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for sub in _EXPORT_SUBDIRS:
            base = vault / sub
            if not base.exists():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    z.write(p, str(p.relative_to(vault)))
    return dest


def _is_safe_member(name: str) -> bool:
    """Защита от path traversal: запрет абсолютных путей и '..'."""
    if name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name).parts
    return ".." not in parts


def _within(child: Path, parent: Path) -> bool:
    """child лежит внутри parent (после раскрытия симлинков)? parent — уже resolved."""
    return child == parent or parent in child.parents


def import_archive(zip_path: str | Path) -> int:
    """Распаковать архив в хранилище, объединяя с существующими данными.

    Возвращает число извлечённых файлов. Небезопасные пути пропускаются.

    Защита от zip-slip/symlink: помимо запрета '..'/абсолютных путей, проверяем, что
    РЕАЛЬНЫЙ путь записи (после раскрытия симлинков в родителях) остаётся внутри vault,
    и не пишем сквозь существующий симлинк — иначе архив мог бы увести запись наружу.
    """
    vault = config.vault_dir()
    vault_resolved = vault.resolve()
    extracted = 0
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            if not _is_safe_member(member):
                continue
            # принимаем только данные хранилища
            top = Path(member).parts[0]
            if top not in _EXPORT_SUBDIRS:
                continue
            target = vault / member
            target.parent.mkdir(parents=True, exist_ok=True)
            # реальный каталог записи обязан остаться внутри vault (ловит симлинк наружу)
            try:
                real_parent = target.parent.resolve(strict=True)
            except OSError:
                continue
            if not _within(real_parent, vault_resolved):
                continue
            # не перезаписывать/не писать сквозь симлинк (он мог бы указывать вовне)
            if target.is_symlink():
                continue
            with z.open(member) as srcf, open(target, "wb") as dstf:
                dstf.write(srcf.read())
            extracted += 1
    # на диск легли новые заметки — перестроить индекс под них
    from . import index
    index.rebuild()
    return extracted
