"""Duress-стирание: ввод ПИНа задом наперёд необратимо уничтожает реальные данные и
создаёт подложку. Снаружи — обычная разблокировка (без предупреждений).

Последовательность (fail-safe, крипто-стирание прежде всего):
1. Закрыть открытые соединения БД и эфемерные plaintext-кэши.
2. КРИПТО-СТИРАНИЕ: уничтожить материал ключа (обёртка MK + соли + TPM-ключ) →
   реальный шифртекст становится невосстановимым.
3. Удалить реальные данные строго по allowlist (owned_paths) — контент vault и
   синк-метаданные. ЛИЧНЫЕ файлы в корне vault (приватные документы, ключи и т.п.) НЕ трогаются.
4. Отключить синхронизацию.
5. Подложка: новый ключ под ОБРАТНЫЙ ПИН как новый нормальный (без второго уровня
   duress — чтобы исходный ПИН не имел особого смысла и не оставлял следа).
6. Наполнить подложку: папка «123» и три заметки.

⚠️ Деструктивно и необратимо. Работает ТОЛЬКО по owned_paths — см. инвариант
безопасности (vault может совпадать с личной папкой пользователя).
"""

from __future__ import annotations

import shutil

from .. import config
from . import session
from ..storage import owned_paths

# I1 (раунд-3): подложка собирается СЛУЧАЙНО из пулов — чтобы у разных устройств декой
# не был байт-в-байт одинаков (узнаваем по одному образцу/исходникам). На устройстве
# создаётся один раз и далее стабилен.
DECOY_FOLDERS = ["123", "Заметки", "Личное", "Дела", "Списки"]
DECOY_POOL = [
    "Тест 1",
    "Напомнить пройти последний уровень в Brotato",
    "Досмотреть 2 сезон игры в кальмара",
    "Купить молоко и хлеб",
    "Позвонить маме в выходные",
    "Записаться к стоматологу",
    "Оплатить интернет до 25-го",
    "Забрать посылку с пункта выдачи",
    "Скинуть отчёт коллеге",
    "Полить цветы",
]
# обратная совместимость имени для внешних ссылок
DECOY_FOLDER = DECOY_FOLDERS[0]


def _rm(path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _close_db_and_caches() -> None:
    try:
        from ..sync import oplog
        oplog.reset_for_tests()
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..storage import index
        index.reset_for_tests()
        index.wipe_ephemeral()
    except Exception:  # noqa: BLE001
        pass
    try:
        from ..storage import vault
        vault.wipe_blob_cache()
    except Exception:  # noqa: BLE001
        pass
    try:
        # сбросить кэш загруженного TPM-ключа: ниже файлы ключа будут удалены и
        # пересозданы, иначе подложка зашифруется старым (кэшированным) ключом.
        from . import tpm
        tpm.reset_cache()
    except Exception:  # noqa: BLE001
        pass
    session.lock()  # забыть реальный MK


def _wipe_owned() -> None:
    """Удалить ВСЕ owned-данные QtNotes. Крипто-стирание (keyring) — первым."""
    kr = config.keyring_dir()
    paths = owned_paths.owned_paths()

    def _pass() -> None:
        # сначала ключевой материал — даже при прерывании дальше шифртекст уже мёртв
        _rm(kr)
        for p in paths:
            rp = p.resolve()
            if rp == kr.resolve() or kr.resolve() in rp.parents:
                continue  # keyring уже удалён
            # защита: стираем только то, что действительно наше
            if owned_paths.is_owned(p):
                _rm(p)

    def _leftovers() -> list:
        return [p for p in ([kr] + list(paths))
                if p.exists() and (p == kr or owned_paths.is_owned(p))]

    _pass()
    # I2 (раунд-3): верификация полноты. Duress обязан УНИЧТОЖИТЬ данные — молчаливое
    # частичное стирание (заблокированный файл/права) недопустимо. Повторяем и, если что-то
    # уцелело, шумим в stderr (пользователь под принуждением — лучше сигнал, чем тишина).
    if _leftovers():
        _pass()
        rest = _leftovers()
        if rest:
            import sys
            print(f"[duress] НЕ удалось стереть {len(rest)} путей: "
                  f"{[str(x) for x in rest]}", file=sys.stderr)


def _create_decoy() -> None:
    import secrets
    from ..storage import vault
    from ..storage.models import Note
    folder = vault.create_folder(secrets.choice(DECOY_FOLDERS), icon="letter")
    count = secrets.choice([2, 3, 4])
    pool = list(DECOY_POOL)
    notes = [pool.pop(secrets.randbelow(len(pool))) for _ in range(count)]
    for text in notes:
        vault.save_note(Note.create_text(folder.id, f"<p>{text}</p>", text))


def execute(reverse_pin: str, backend) -> bytes:
    """Выполнить duress-стирание и создать подложку. Возвращает MK подложки.

    На вход — ВВЕДЁННЫЙ (обратный) ПИН: он становится новым нормальным ПИНом подложки.
    """
    _close_db_and_caches()
    _wipe_owned()
    config.set_sync_enabled(False)

    # подложка: обратный ПИН как новый нормальный, без второго уровня duress
    from . import unlock
    decoy_mk = unlock.setup_pin(reverse_pin, backend, with_duress=False)
    _create_decoy()
    return decoy_mk


def wipe_and_reset() -> None:
    """Самостирание после превышения лимита неверных ПИНов. В отличие от duress —
    БЕЗ подложки: уничтожить ключ и ВСЕ данные QtNotes (по allowlist), выключить
    шифрование и синхронизацию. Устройство возвращается к чистому незашифрованному
    состоянию; данные восстанавливаются со второго устройства. Личные файлы в корне
    vault не трогаются (как и при duress)."""
    _close_db_and_caches()
    _wipe_owned()
    config.set_sync_enabled(False)
    config.set_encryption_enabled(False)
    session.lock()
