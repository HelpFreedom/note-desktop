"""Преобразование текста в безопасный HTML с кликабельными ссылками.

Поддерживаются:
- внешние URL (http/https/www) — открываются в браузере;
- внутренние ссылки на заметки вида [[<id>]] — переход внутри приложения
  (схема qtnote:<id>).
"""

from __future__ import annotations

import re
from html import escape

from .theme import PALETTE

# URL вида http(s)://... или www....
_URL_RE = re.compile(r"((?:https?://|www\.)[^\s<>\"']+)", re.IGNORECASE)

# Ссылка на заметку: [[<32 hex>]]
_REF_RE = re.compile(r"\[\[([0-9a-fA-F]{32})\]\]")

# Вшитые в HTML шрифт и цвет (из старых заметок / toHtml) убираем при отображении,
# чтобы текст и ссылки брали цвета из текущей темы (палитры).
_FONT_FAMILY_RE = re.compile(r"font-family\s*:[^;\"']*;?", re.IGNORECASE)
# 'color:' но НЕ 'background-color:'
_COLOR_RE = re.compile(r"(?<![-\w])color\s*:[^;\"']*;?", re.IGNORECASE)


def strip_theme_overrides(html: str) -> str:
    return _COLOR_RE.sub("", _FONT_FAMILY_RE.sub("", html))


def strip_font_family(html: str) -> str:
    return _FONT_FAMILY_RE.sub("", html)


_ANCHOR_RE = re.compile(r"<a\b([^>]*)>", re.IGNORECASE)


def colorize_links(html: str, color: str) -> str:
    """Проставить цвет ссылок прямо в <a> при отображении (адаптивно под тему).

    Применять ПОСЛЕ strip_theme_overrides, чтобы не было дублей color.
    """
    def repl(m: re.Match) -> str:
        attrs = m.group(1)
        style = f"color:{color};text-decoration:underline;"
        if "style=" in attrs.lower():
            attrs = re.sub(r'style\s*=\s*"([^"]*)"',
                           lambda s: f'style="{style}{s.group(1)}"', attrs, count=1,
                           flags=re.IGNORECASE)
        else:
            attrs = f' style="{style}"' + attrs
        return f"<a{attrs}>"

    return _ANCHOR_RE.sub(repl, html)


def _wrap_url(url: str) -> str:
    # цвет НЕ вшиваем — его задаёт QPalette.Link (адаптивно под тему)
    href = url if url.lower().startswith("http") else "http://" + url
    return f'<a href="{escape(href, quote=True)}">{escape(url)}</a>'


def linkify_references(s: str) -> str:
    """Заменить токены [[id]] на внутренние ссылки qtnote:<id> (цвет — из палитры)."""
    def repl(m: re.Match) -> str:
        note_id = m.group(1).lower()
        short = note_id[:6]
        return f'<a href="qtnote:{note_id}">↪&nbsp;#{short}</a>'

    return _REF_RE.sub(repl, s)


def linkify_plain(text: str) -> str:
    """Простой текст → HTML: экранирование, URL и ссылки на заметки, переносы."""
    out = []
    last = 0
    for m in _URL_RE.finditer(text):
        out.append(escape(text[last:m.start()]))
        out.append(_wrap_url(m.group(1)))
        last = m.end()
    out.append(escape(text[last:]))
    html = "".join(out)
    html = linkify_references(html)
    return html.replace("\n", "<br>")
