"""Модели данных: папка, заметка, вложение, событие календаря.

Все модели сериализуются в JSON через as_dict()/from_dict() — это формат
хранения на диске и основа переносимости (экспорт = копия файлов).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    # микросекунды нужны для стабильного порядка заметок, созданных подряд
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- Папка ---

@dataclass
class Folder:
    id: str
    name: str
    caption: str = ""
    color: str | None = None
    icon: str = "letter"
    order: int = 0
    created: str = field(default_factory=now_iso)

    @classmethod
    def create(cls, name: str, caption: str = "", color: str | None = None,
               icon: str = "letter", order: int = 0) -> "Folder":
        return cls(id=new_id(), name=name.strip(), caption=caption.strip(),
                   color=color, icon=icon or "letter", order=order)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "caption": self.caption,
            "color": self.color,
            "icon": self.icon,
            "order": self.order,
            "created": self.created,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Folder":
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            caption=d.get("caption", ""),
            color=d.get("color"),
            icon=d.get("icon", "letter"),
            order=int(d.get("order", 0)),
            created=d.get("created", now_iso()),
        )


# --- Вложение ---

@dataclass
class Attachment:
    file: str          # имя файла (для отображения/расширения)
    mime: str = ""
    name: str = ""
    size: int = 0      # размер файла в байтах
    w: int = 0         # ширина (для изображений)
    h: int = 0         # высота (для изображений)
    sha256: str = ""   # хэш содержимого; если задан — файл лежит в blobs/<sha256>

    def as_dict(self) -> dict:
        return {"file": self.file, "mime": self.mime, "name": self.name,
                "size": self.size, "w": self.w, "h": self.h, "sha256": self.sha256}

    @classmethod
    def from_dict(cls, d: dict) -> "Attachment":
        return cls(
            file=d["file"], mime=d.get("mime", ""), name=d.get("name", ""),
            size=int(d.get("size", 0)), w=int(d.get("w", 0)), h=int(d.get("h", 0)),
            sha256=d.get("sha256", ""),
        )


# --- Заметка ---

@dataclass
class Note:
    id: str
    folder_id: str
    kind: str = "text"          # text | image | file | album
    html: str = ""              # рич-текст (HTML)
    plaintext: str = ""         # для индексации/поиска
    caption_html: str = ""      # подпись к картинке/альбому
    attachments: list[Attachment] = field(default_factory=list)
    date_tag: str | None = None  # YYYY-MM-DD, опц. привязка к дате
    created: str = field(default_factory=now_iso)
    modified: str = field(default_factory=now_iso)

    @classmethod
    def create_text(cls, folder_id: str, html: str, plaintext: str) -> "Note":
        return cls(id=new_id(), folder_id=folder_id, kind="text", html=html, plaintext=plaintext)

    def touch(self) -> None:
        self.modified = now_iso()

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "folder_id": self.folder_id,
            "kind": self.kind,
            "html": self.html,
            "plaintext": self.plaintext,
            "caption_html": self.caption_html,
            "attachments": [a.as_dict() for a in self.attachments],
            "date_tag": self.date_tag,
            "created": self.created,
            "modified": self.modified,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Note":
        return cls(
            id=d["id"],
            folder_id=d.get("folder_id", ""),
            kind=d.get("kind", "text"),
            html=d.get("html", ""),
            plaintext=d.get("plaintext", ""),
            caption_html=d.get("caption_html", ""),
            attachments=[Attachment.from_dict(a) for a in d.get("attachments", [])],
            date_tag=d.get("date_tag"),
            created=d.get("created", now_iso()),
            modified=d.get("modified", now_iso()),
        )


# --- Событие календаря ---

@dataclass
class Event:
    id: str
    date: str          # YYYY-MM-DD
    name: str
    color: str

    @classmethod
    def create(cls, date: str, name: str, color: str) -> "Event":
        return cls(id=new_id(), date=date, name=name.strip(), color=color)

    def as_dict(self) -> dict:
        return {"id": self.id, "date": self.date, "name": self.name, "color": self.color}

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(id=d["id"], date=d["date"], name=d.get("name", ""), color=d.get("color", ""))
