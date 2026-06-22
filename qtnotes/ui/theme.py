"""Тема в духе Telegram Desktop. Палитра и фон берутся из pywal
(`~/.cache/wal/colors.json`) — как у walogram-темы Telegram, адаптивно под обои.
Если pywal недоступен, используется встроенная тёмная палитра."""

from __future__ import annotations

import json
import os
from pathlib import Path

# --- Встроенная тёмная палитра (фолбэк) ---
DEFAULT_PALETTE = {
    "sidebar_bg": "#0e141b",
    "sidebar_item_hover": "#19222c",
    "sidebar_item_active": "#244162",
    "main_bg": "#070b10",
    "header_bg": "#0e141b",
    "bubble_bg": "#131c26",
    "bubble_own_bg": "#1f3650",
    "input_bg": "#0e141b",
    "field_bg": "#1a232e",
    "border": "#05080c",
    "text": "#eef2f6",
    "text_secondary": "#6b7c8e",
    "accent": "#4a82bd",
    "accent_hover": "#5a93ce",
    "link": "#62a8ea",
    "danger": "#e06b6b",
}


def _clamp(v: int) -> int:
    return max(0, min(255, v))


def _adjust(hexc: str, factor: float) -> str:
    """factor>0 — светлее, <0 — темнее (в долях от 0..1)."""
    h = hexc.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    if factor >= 0:
        r, g, b = (_clamp(int(c + (255 - c) * factor)) for c in (r, g, b))
    else:
        r, g, b = (_clamp(int(c * (1 + factor))) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _mix(a: str, b: str, t: float) -> str:
    ah, bh = a.lstrip("#"), b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        ca, cb = int(ah[i:i + 2], 16), int(bh[i:i + 2], 16)
        out.append(_clamp(int(ca * (1 - t) + cb * t)))
    return f"#{out[0]:02x}{out[1]:02x}{out[2]:02x}"


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base)


def _wal_cache() -> Path:
    return _cache_dir() / "wal" / "colors.json"


def _hex6(v: str) -> str:
    """Привести #RRGGBB/#RRGGBBAA к #RRGGBB (Qt QSS не понимает альфу tdesktop)."""
    h = v.lstrip("#")
    return "#" + h[:6]


def _load_tdesktop_colors() -> dict | None:
    """Прочитать сгенерированную walogram тему Telegram и вернуть name->#hex.

    Берём точные цвета, которые Telegram назначает элементам (windowBg, msgOutBg,
    historyLinkInFg и т.д.), разрешая ссылки между именами.
    """
    import re
    import zipfile

    path = _cache_dir() / "walogram" / "wal.tdesktop-theme"
    if not path.exists():
        return None
    try:
        with zipfile.ZipFile(path) as z:
            text = z.read("colors.tdesktop-theme").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None

    raw: dict[str, str] = {}
    line_re = re.compile(r"^\s*([A-Za-z0-9_]+)\s*:\s*([^;]+);")
    for line in text.splitlines():
        line = line.split("//", 1)[0]
        m = line_re.match(line)
        if m:
            raw[m.group(1)] = m.group(2).strip()

    # разрешить ссылки (name: othername) в hex
    resolved: dict[str, str] = {}

    def resolve(name: str, depth: int = 0):
        if depth > 10:
            return None
        v = raw.get(name)
        if v is None:
            return None
        if v.startswith("#"):
            return _hex6(v)
        return resolve(v, depth + 1)

    for name in raw:
        hexv = resolve(name)
        if hexv:
            resolved[name] = hexv
    return resolved or None


def _map_tdesktop(td: dict) -> dict:
    """Семантика Telegram → палитра приложения (точные цвета темы)."""
    def c(name: str, fallback: str) -> str:
        v = td.get(name)
        return v if v else fallback

    window_bg = c("windowBg", "#101516")
    accent = c("windowBgActive", "#8F7A77")
    # ссылки — тем же цветом, что и кнопка (accent)
    link = accent
    return {
        "sidebar_bg": window_bg,
        "sidebar_item_hover": c("windowBgOver", _adjust(window_bg, 0.2)),
        "sidebar_item_active": accent,
        "main_bg": window_bg,
        "header_bg": window_bg,
        "bubble_bg": c("msgInBg", "#363c42"),
        "bubble_own_bg": c("msgOutBg", "#32373d"),
        "input_bg": window_bg,
        "field_bg": c("msgInBg", "#363c42"),
        "border": _adjust(window_bg, -0.45),
        "text": c("windowFg", "#b4c5d9"),
        "text_secondary": c("windowSubTextFg", "#909eae"),
        "accent": accent,
        "accent_hover": _adjust(accent, 0.2),
        "link": link,
        "danger": "#e06b6b",
    }


def _load_wal() -> dict | None:
    try:
        return json.loads(_wal_cache().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError):
        return None


def rgba(hexc: str, alpha: float) -> str:
    """#rrggbb + alpha(0..1) → 'rgba(r, g, b, a)' для QSS."""
    h = hexc.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def load_palette() -> dict:
    td = _load_tdesktop_colors()
    if td:
        return _map_tdesktop(td)
    data = _load_wal()
    if not data:
        return dict(DEFAULT_PALETTE)
    try:
        bg = data["special"]["background"]
        fg = data["special"]["foreground"]
        c = data["colors"]
        accent = c.get("color4", "#4a82bd")
        link = c.get("color6", c.get("color12", "#62a8ea"))
    except (KeyError, TypeError):
        return dict(DEFAULT_PALETTE)
    return {
        "sidebar_bg": _adjust(bg, -0.12),
        "sidebar_item_hover": _adjust(bg, 0.18),
        "sidebar_item_active": accent,
        "main_bg": bg,
        "header_bg": _adjust(bg, -0.06),
        "bubble_bg": _adjust(bg, 0.30),
        "bubble_own_bg": _mix(accent, bg, 0.25),
        "input_bg": _adjust(bg, -0.04),
        "field_bg": _adjust(bg, 0.22),
        "border": _adjust(bg, -0.45),
        "text": fg,
        "text_secondary": _mix(fg, bg, 0.45),
        "accent": accent,
        "accent_hover": _adjust(accent, 0.22),
        "link": _adjust(link, 0.25),
        "danger": "#e06b6b",
    }


def wallpaper_path() -> str | None:
    data = _load_wal()
    if data:
        wp = data.get("wallpaper")
        if wp and Path(wp).exists():
            return wp
    return None


PALETTE = load_palette()

# --- 10 цветов для событий календаря (имя -> hex) ---
EVENT_COLORS = [
    ("Красный", "#e56555"),
    ("Оранжевый", "#e9924d"),
    ("Жёлтый", "#e7c14f"),
    ("Зелёный", "#67b35e"),
    ("Бирюзовый", "#4db6ac"),
    ("Голубой", "#5aa7e0"),
    ("Синий", "#5288c1"),
    ("Фиолетовый", "#9b72d4"),
    ("Розовый", "#e57aa8"),
    ("Серый", "#8a98a8"),
]

EVENT_COLOR_HEXES = [hexv for _, hexv in EVENT_COLORS]
DEFAULT_EVENT_COLOR = EVENT_COLORS[6][1]  # синий


def build_qss() -> str:
    """Глобальная таблица стилей приложения."""
    p = PALETTE
    return f"""
    QWidget {{
        background-color: {p['main_bg']};
        color: {p['text']};
    }}

    /* ---- Сайдбар ---- */
    #Sidebar {{
        background-color: {p['sidebar_bg']};
        border-right: 1px solid {p['border']};
    }}
    #SidebarScroll, #SidebarScroll > QWidget > QWidget {{
        background-color: {p['sidebar_bg']};
        border: none;
    }}
    FolderItem {{
        background-color: transparent;
        border: none;
        border-radius: 10px;
    }}
    FolderItem:hover {{
        background-color: {p['sidebar_item_hover']};
    }}
    FolderItem[active="true"] {{
        background-color: {p['sidebar_item_active']};
    }}
    FolderItem QLabel {{
        background: transparent;
        color: {p['text_secondary']};
        font-size: 11px;
    }}
    FolderItem[active="true"] QLabel {{
        color: {p['text']};
    }}

    QLabel#SidebarHint {{
        background: transparent;
        color: {p['text_secondary']};
        font-size: 10px;
    }}

    QToolButton#SidebarButton {{
        background-color: transparent;
        border: none;
        border-radius: 10px;
        padding: 6px;
    }}
    QToolButton#SidebarButton:hover {{
        background-color: {p['sidebar_item_hover']};
    }}
    QToolButton#SidebarButton:pressed {{
        background-color: {p['sidebar_item_active']};
    }}

    /* ---- Заголовок основной области ---- */
    #ChatHeader, #CalendarHeader {{
        background-color: {p['header_bg']};
        border-bottom: 1px solid {p['border']};
    }}
    #ChatTitle {{
        font-size: 16px;
        font-weight: 600;
    }}
    #SelectionBar {{
        background-color: {p['sidebar_item_active']};
        border-bottom: 1px solid {p['border']};
    }}
    #SelectionBar QLabel {{ background: transparent; color: {p['text']}; font-weight: 600; }}
    #SearchField {{
        background-color: {p['field_bg']};
        border: 1px solid {p['border']};
        border-radius: 14px;
        padding: 4px 8px;
    }}
    #SearchField:focus {{ border: 1px solid {p['accent']}; }}
    QPushButton#Ghost:checked {{ color: {p['accent_hover']}; }}
    #ResultRow {{ background-color: {p['bubble_bg']}; border-radius: 10px; }}
    #ResultRow:hover {{ background-color: {p['bubble_own_bg']}; }}
    #ResultRow QLabel {{ background: transparent; }}
    #ResultFolder {{ color: {p['accent_hover']}; font-size: 12px; font-weight: 600; }}

    /* ---- Лента: слои прозрачны (под ними обои), пузыри — нет ---- */
    #ChatScroll {{ background: transparent; border: none; }}
    QScrollArea#ChatScroll > QWidget {{ background: transparent; }}
    #ChatContent, #ChatFeed, #ResultsHost {{ background: transparent; }}

    /* ---- Лоток ожидающих вложений ---- */
    #PendingTray {{ background: transparent; }}
    #PendingChip {{
        background-color: {p['field_bg']};
        border-radius: 8px;
    }}
    #PendingChip QLabel {{ background: transparent; color: {p['text']}; font-size: 12px; }}

    /* ---- Пузыри заметок ---- */
    #Bubble {{
        background-color: {rgba(p['bubble_own_bg'], 0.80)};
        border-radius: 14px;
        border: 2px solid transparent;
    }}
    #Bubble[selected="true"] {{
        border: 2px solid {p['accent']};
        background-color: {p['sidebar_item_active']};
    }}
    #BubbleText {{ background: transparent; color: {p['text']}; }}
    #BubbleTime {{ background: transparent; color: {p['text_secondary']}; font-size: 11px; }}
    #FileChip {{
        background-color: {p['field_bg']};
        border-radius: 10px;
    }}
    #FileChip:hover {{ background-color: {p['sidebar_item_active']}; }}
    #FileChip QLabel {{ background: transparent; color: {p['text']}; }}
    /* вложение пропало с диска — приглушённый вид, без hover-подсветки */
    #FileChip[missing="true"] {{ background-color: {p['field_bg']}; }}
    #FileChip[missing="true"]:hover {{ background-color: {p['field_bg']}; }}
    #FileChip[missing="true"] QLabel {{ color: {p['text_secondary']}; }}

    /* ---- Панель форматирования ---- */
    #FormatToolbar {{ background: transparent; }}
    QToolButton#FmtButton {{
        background-color: transparent;
        color: {p['text_secondary']};
        border: none;
        border-radius: 6px;
    }}
    QToolButton#FmtButton:hover {{
        background-color: {p['field_bg']};
        color: {p['text']};
    }}

    /* ---- Поле ввода ---- */
    #InputBar {{
        background-color: {p['input_bg']};
        border-top: 1px solid {p['border']};
    }}
    #InputField {{
        background-color: {p['field_bg']};
        border: none;
        border-radius: 18px;
        padding: 8px 12px;
        color: {p['text']};
    }}
    #EditBanner {{
        background-color: {p['field_bg']};
        border-left: 3px solid {p['accent']};
        border-radius: 6px;
    }}
    #EditBanner QLabel {{ background: transparent; color: {p['accent_hover']}; font-weight: 600; }}

    /* ---- Кнопки ---- */
    QPushButton {{
        background-color: {p['accent']};
        color: white;
        border: none;
        border-radius: 8px;
        padding: 8px 16px;
    }}
    QPushButton:hover {{ background-color: {p['accent_hover']}; }}
    QPushButton:disabled {{ background-color: {p['field_bg']}; color: {p['text_secondary']}; }}
    QPushButton#Ghost {{
        background-color: transparent;
        color: {p['text_secondary']};
    }}
    QPushButton#Ghost:hover {{ color: {p['text']}; }}

    /* ---- Поля ввода / диалоги ---- */
    QLineEdit, QPlainTextEdit, QTextEdit {{
        background-color: {p['field_bg']};
        border: 1px solid {p['border']};
        border-radius: 8px;
        padding: 6px 10px;
        color: {p['text']};
        selection-background-color: {p['accent']};
    }}
    QLineEdit:focus {{ border: 1px solid {p['accent']}; }}

    QMenu {{
        background-color: {p['sidebar_bg']};
        border: 1px solid {p['border']};
        border-radius: 8px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
    QMenu::item:selected {{ background-color: {p['sidebar_item_active']}; }}

    QPushButton#IconPick {{
        background-color: {p['field_bg']};
        border: 2px solid transparent;
        border-radius: 8px;
        padding: 2px;
    }}
    QPushButton#IconPick:hover {{ border: 2px solid {p['text_secondary']}; }}
    QPushButton#IconPick:checked {{ border: 2px solid {p['accent']}; }}

    QLabel#EmptyState {{
        color: {p['text_secondary']};
        font-size: 15px;
    }}

    /* ---- Календарь (Google-стиль) ---- */
    #CalHeader {{
        background-color: {p['header_bg']};
        border-bottom: 1px solid {p['border']};
    }}
    #WeekHeader {{ background-color: {p['header_bg']}; }}
    #WeekdayLabel, #WeekendLabel {{
        background: transparent;
        padding: 6px 0;
        font-size: 12px;
        font-weight: 600;
    }}
    #WeekdayLabel {{ color: {p['text_secondary']}; }}
    #WeekendLabel {{ color: {p['accent_hover']}; }}

    #DayCell {{
        background-color: {_adjust(p['main_bg'], 0.10)};
        border: 1px solid {p['border']};
    }}
    #DayCell[dim="true"] {{ background-color: {p['main_bg']}; }}
    #DayCell[today="true"] {{
        background-color: {_adjust(p['accent'], -0.55)};
        border: 2px solid {p['accent']};
    }}
    #DayCell[drop="true"] {{ background-color: {p['sidebar_item_active']}; }}
    #DayCell QLabel {{ background: transparent; }}
    #DayNumber {{ color: {p['text']}; font-size: 12px; font-weight: 600; }}
    #DayCell[dim="true"] #DayNumber {{ color: {p['text_secondary']}; }}
    #DayNumberToday {{
        color: white;
        background-color: {p['accent']};
        border-radius: 9px;
        min-width: 18px; max-width: 18px;
        min-height: 18px; max-height: 18px;
        font-size: 11px; font-weight: 700;
    }}
    #MoreEvents {{ color: {p['text_secondary']}; font-size: 10px; }}

    /* ---- Скроллбары ---- */
    QScrollBar:vertical {{
        background: transparent; width: 8px; margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        background: {p['field_bg']}; border-radius: 4px; min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p['sidebar_item_active']}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    """
