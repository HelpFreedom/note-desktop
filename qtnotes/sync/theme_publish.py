"""Публикация темы десктопа в общие синхронизируемые настройки.

Десктоп при старте синхронизации кладёт свою палитру и обои (как blob) в shared
("theme"), откуда телефон их подхватывает и применяет. Только десктоп публикует —
у телефона своей walogram-темы нет.
"""

from __future__ import annotations

import hashlib
import os


def _map_palette(p: dict) -> dict:
    """Палитра десктопа → ключи, понятные мобильному приложению."""
    return {
        "background": p["main_bg"],
        "appBar": p["header_bg"],
        "bubble": p["bubble_own_bg"],
        "bubbleAlt": p["bubble_bg"],
        "accent": p["accent"],
        "field": p["input_bg"],
        "fieldBorder": p["border"],
        "text": p["text"],
        "textSecondary": p["text_secondary"],
        "link": p["link"],
    }


def publish_theme() -> None:
    try:
        from ..storage import vault
        from ..ui import theme

        palette = _map_palette(theme.PALETTE)
        wallpaper_sha = ""
        wp = theme.wallpaper_path()
        if wp and os.path.isfile(wp):
            data = open(wp, "rb").read()
            wallpaper_sha = hashlib.sha256(data).hexdigest()
            vault.write_blob(wallpaper_sha, data)

        value = {"palette": palette, "wallpaper": wallpaper_sha}
        if vault.get_shared("theme") != value:
            vault.set_shared("theme", value)
    except Exception as e:  # noqa: BLE001 — публикация темы не должна ронять синк
        print(f"[theme] publish failed: {e}")
