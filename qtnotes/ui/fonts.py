"""Загрузка встроенного цветного emoji-шрифта (Noto Color Emoji).

Делает эмодзи цветными без системного шрифта: добавляем шрифт в приложение и
ставим его запасным в семействе шрифтов — Qt подмешивает глифы эмодзи из него.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtGui import QFontDatabase

_emoji_family: str | None = None
_loaded = False

_BUNDLED = Path(__file__).resolve().parent.parent / "resources" / "fonts" / "NotoColorEmoji.ttf"


def ensure_emoji_font() -> None:
    """Установить цветной emoji-шрифт в пользовательские шрифты (без sudo).

    Тогда fontconfig подхватывает его автоматически как фолбэк ТОЛЬКО для
    эмодзи-символов — текст остаётся обычным шрифтом. Вызывать до QApplication.
    """
    if not _BUNDLED.exists():
        return
    dest_dir = Path.home() / ".local" / "share" / "fonts"
    dest = dest_dir / "NotoColorEmoji.ttf"
    if dest.exists():
        return
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_BUNDLED, dest)
        subprocess.run(["fc-cache", "-f", str(dest_dir)],
                       timeout=30, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def emoji_family() -> str | None:
    global _emoji_family, _loaded
    if _loaded:
        return _emoji_family
    _loaded = True
    path = Path(__file__).resolve().parent.parent / "resources" / "fonts" / "NotoColorEmoji.ttf"
    if path.exists():
        fid = QFontDatabase.addApplicationFont(str(path))
        if fid != -1:
            fams = QFontDatabase.applicationFontFamilies(fid)
            if fams:
                _emoji_family = fams[0]
    return _emoji_family
