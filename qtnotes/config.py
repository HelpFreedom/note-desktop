"""Пути хранилища и настройки приложения.

Настройки приложения (путь к хранилищу, шрифт) лежат ВНЕ хранилища —
в `$XDG_CONFIG_HOME/QtNotes/settings.json` (по умолчанию ~/.config/QtNotes),
потому что сам путь к хранилищу является настройкой.

Хранилище (vault) — обычная папка с файлами. Путь определяется так:
переменная окружения QTNOTES_VAULT  >  settings.json["vault_path"]  >  по умолчанию.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

APP_DIR_NAME = "QtNotes"

# Пусто = использовать системное семейство по умолчанию (его и разрешит Qt).
DEFAULT_FONT_FAMILY = ""
DEFAULT_FONT_SIZE = 14


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME")
    return Path(raw) if raw else Path.home() / ".local" / "share"


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else Path.home() / ".config"


def settings_path() -> Path:
    d = _xdg_config_home() / APP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d / "settings.json"


def load_settings() -> dict:
    p = settings_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_settings(settings: dict) -> None:
    # Атомарно и durably (tmp+fsync+replace): settings.json хранит путь к хранилищу и
    # флаг шифрования — обрыв на середине обычной записи оставил бы битый файл, и
    # load_settings вернул бы {} → дефолтный пустой vault + тихо выключенное шифрование.
    from . import fsutil
    data = json.dumps(settings, ensure_ascii=False, indent=2).encode("utf-8")
    fsutil.atomic_write_bytes(settings_path(), data)


def get_setting(key: str, default=None):
    return load_settings().get(key, default)


def set_setting(key: str, value) -> None:
    s = load_settings()
    s[key] = value
    save_settings(s)


def font_family() -> str:
    return get_setting("font_family") or DEFAULT_FONT_FAMILY  # "" → системное


def font_size() -> int:
    try:
        return int(get_setting("font_size") or DEFAULT_FONT_SIZE)
    except (TypeError, ValueError):
        return DEFAULT_FONT_SIZE


def _default_vault() -> Path:
    return _xdg_data_home() / APP_DIR_NAME


def vault_dir() -> Path:
    """Корневая папка хранилища. Создаётся при первом обращении."""
    override = os.environ.get("QTNOTES_VAULT")
    if override:
        base = Path(override)
    else:
        configured = get_setting("vault_path")
        base = Path(configured) if configured else _default_vault()
    base.mkdir(parents=True, exist_ok=True)
    return base


def set_vault_path(path: str | os.PathLike) -> None:
    set_setting("vault_path", str(path))


def folders_dir() -> Path:
    d = vault_dir() / "folders"
    d.mkdir(parents=True, exist_ok=True)
    return d


def calendar_dir() -> Path:
    d = vault_dir() / "calendar"
    d.mkdir(parents=True, exist_ok=True)
    return d


def blobs_dir() -> Path:
    """Content-addressed хранилище вложений: blobs/<sha256>. Для синка/дедупа."""
    d = vault_dir() / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tmpfs_dir(prefix: str) -> Path:
    """RAM-backed (tmpfs) каталог `<prefix>-<хэш vault>` для эфемерных plaintext-данных
    при включённом шифровании (индекс, расшифрованные блобы).

    Данные в tmpfs (`/dev/shm` или `XDG_RUNTIME_DIR`) живут только в памяти и исчезают
    при выключении. Имя привязано к пути vault (стабильно в рамках сессии, изолирует
    разные vault'ы). Крайний случай (нет tmpfs) — vault (на Linux не происходит)."""
    vid = hashlib.sha256(str(vault_dir()).encode("utf-8")).hexdigest()[:16]
    for base in ("/dev/shm", os.environ.get("XDG_RUNTIME_DIR"), tempfile.gettempdir()):
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            d = Path(base) / f"{prefix}-{vid}"
            d.mkdir(parents=True, exist_ok=True)
            return d
    return vault_dir()


def _ephemeral_index_dir() -> Path:
    """tmpfs-каталог для индекса при включённом шифровании (см. tmpfs_dir)."""
    return tmpfs_dir("qtnotes-index")


def index_path() -> Path:
    """Файл SQLite-индекса (перестраиваемый, в экспорт не входит).

    При включённом шифровании индекс уезжает в tmpfs (RAM), чтобы plaintext заметок
    не оседал на диске; перестраивается из расшифрованных заметок при старте/доступе.
    """
    if encryption_enabled():
        return _ephemeral_index_dir() / "index.sqlite"
    return vault_dir() / "index.sqlite"


# --- синхронизация (опционально) ---

def device_dir() -> Path:
    """Личность устройства (ключ, cert) — per-installation, НЕ в vault и НЕ
    синхронизируется. Лежит рядом с настройками приложения."""
    d = _xdg_config_home() / APP_DIR_NAME / "device"
    d.mkdir(parents=True, exist_ok=True)
    return d


def peers_path() -> Path:
    """Файл доверенных (сопряжённых) устройств."""
    return _xdg_config_home() / APP_DIR_NAME / "peers.json"


def sync_enabled() -> bool:
    return bool(get_setting("sync_enabled", False))


def set_sync_enabled(on: bool) -> None:
    set_setting("sync_enabled", bool(on))


def sync_port() -> int:
    """Сохранённый порт движка синхронизации. Стабильный порт между перезапусками,
    чтобы сохранённый телефоном адрес (host:port из QR) не устаревал. 0 — выбрать любой."""
    try:
        return int(get_setting("sync_port", 0) or 0)
    except (TypeError, ValueError):
        return 0


def set_sync_port(port: int) -> None:
    set_setting("sync_port", int(port))


# --- локальное шифрование (опционально) ---

def encryption_enabled() -> bool:
    """Включено ли шифрование хранилища at-rest. По умолчанию — выкл (поведение
    приложения не меняется, пока пользователь явно не настроит ПИН)."""
    return bool(get_setting("encryption_enabled", False))


def set_encryption_enabled(on: bool) -> None:
    set_setting("encryption_enabled", bool(on))


def keyring_dir() -> Path:
    """Метаданные ключа (обёртка MK, соли, верификаторы, состояние lockout).
    Рядом с настройками, НЕ в vault и НЕ синхронизируется."""
    d = _xdg_config_home() / APP_DIR_NAME / "keyring"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sync_db_path() -> Path:
    """Журнал операций (op-log). Живёт в vault, в экспорт не входит."""
    return vault_dir() / "sync.sqlite"
