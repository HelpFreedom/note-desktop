"""Явный манифест путей, которыми владеет QtNotes.

⚠️ КРИТИЧНО ДЛЯ БЕЗОПАСНОСТИ. vault пользователя может совпадать с его личной папкой
(например, `~/Documents` с приватными файлами, ключами, паролями). Поэтому любое
массовое удаление (duress-стирание в Ф6) обязано работать ТОЛЬКО по этому списку и
НИКОГДА не делать rmtree по корню vault или glob-удалений.

Здесь — только перечисление путей. Никаких удалений: деструктив будет в Ф6 и будет
опираться на `owned_paths()` как на единственный источник истины.
"""

from __future__ import annotations

from pathlib import Path

from .. import config


def owned_paths() -> list[Path]:
    """Все пути (файлы и каталоги), принадлежащие QtNotes, которые допустимо стирать.

    Не включает settings.json: в нём лежит vault_path и прочие настройки приложения;
    при duress его не удаляют, а лишь сбрасывают sync_enabled (отдельной операцией).
    """
    vault = config.vault_dir()
    cfg = config._xdg_config_home() / config.APP_DIR_NAME
    paths = [
        # данные vault
        vault / "folders",
        vault / "calendar",
        vault / "blobs",
        config.index_path(),       # vault/index.sqlite
        config.sync_db_path(),     # vault/sync.sqlite (op-log)
        vault / "shared.json",     # общие настройки (тема/обои)
        # личность и синхронизация (вне vault, рядом с настройками)
        config.device_dir(),       # ~/.config/QtNotes/device
        config.peers_path(),       # ~/.config/QtNotes/peers.json
        cfg / "keyring",           # ~/.config/QtNotes/keyring (метаданные ключа)
        # временный бэкап миграции шифрования (плейнтекст ДО перешифровки): удаляется
        # после успеха, но пока существует — обязан быть стираемым duress'ом
        cfg / "migration-backup",  # ~/.config/QtNotes/migration-backup
    ]
    return paths


def is_owned(path: Path) -> bool:
    """True, если path лежит внутри одного из owned-путей (или равен ему).

    Защитная проверка для деструктивных операций: перед удалением можно убедиться,
    что цель действительно наша.
    """
    rp = Path(path).resolve()
    for owned in owned_paths():
        o = owned.resolve()
        if rp == o or o in rp.parents:
            return True
    return False
