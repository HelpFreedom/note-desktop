"""Поиск заметок: нечёткий по содержанию (rapidfuzz) + по дате.

Кандидаты берутся из SQLite-индекса (storage/index.py) одной выборкой, без
чтения JSON-файлов — это и есть основной выигрыш по скорости. rapidfuzz даёт
устойчивое к опечаткам ранжирование. На очень больших коллекциях индекс
предварительно сужает множество кандидатов префиксным поиском FTS5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from . import index
from .models import Note

SCORE_THRESHOLD = 60

# Шаблоны даты: 17.06.2026 / 17.06 / 2026-06-17
_DATE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{2,4}|\d{1,2}\.\d{1,2})\s*$"
)


@dataclass
class SearchHit:
    note: Note
    folder_id: str
    score: float


def _try_date(query: str) -> str | None:
    """Если запрос похож на дату — вернуть строку YYYY-MM-DD (или префикс)."""
    if not _DATE_RE.match(query):
        return None
    try:
        from dateutil import parser as dtp
        dt = dtp.parse(query.strip(), dayfirst=True, fuzzy=False)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return None


def _note_from_row(row) -> Note:
    """Лёгкая Note из строки индекса: для сниппета и навигации по id хватает
    plaintext/created; полное содержимое подгрузится при открытии заметки."""
    return Note(
        id=row["id"],
        folder_id=row["folder_id"],
        plaintext=row["plaintext"] or "",
        date_tag=row["date_tag"],
        created=row["created"] or "",
    )


def search(query: str, folder_id: str | None = None, limit: int = 80) -> list[SearchHit]:
    """Поиск. folder_id=None → по всем папкам, иначе только в указанной."""
    q = query.strip()
    if not q:
        return []
    ql = q.lower()
    hits: list[SearchHit] = []
    seen: set[str] = set()

    # точное совпадение по дате — высший приоритет
    date_str = _try_date(q)
    if date_str:
        for row in index.date_rows(date_str, folder_id):
            hits.append(SearchHit(_note_from_row(row), row["folder_id"], 100.0))
            seen.add(row["id"])

    # нечёткое ранжирование кандидатов из индекса
    for row in index.candidate_rows(q, folder_id):
        if row["id"] in seen:
            continue
        text = row["plaintext"] or ""
        if not text:
            continue
        score = max(fuzz.partial_ratio(ql, text.lower()),
                    fuzz.token_set_ratio(ql, text.lower()))
        if score >= SCORE_THRESHOLD:
            hits.append(SearchHit(_note_from_row(row), row["folder_id"], score))

    hits.sort(key=lambda h: (-h.score, h.note.created))
    return hits[:limit]
