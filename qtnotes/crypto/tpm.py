"""Низкоуровневый доступ к TPM 2.0 через утилиты tpm2-tools (subprocess).

Зачем tpm2-tools, а не tpm2-pytss: все нужные команды есть (tpm2-tools 5.0), нет
хрупкой сборки C-биндингов. subprocess запускает бинарники напрямую (без shell),
поэтому пользовательский профиль не подгружается; ПИН передаётся через stdin (pipe),
а не через файл — чтобы не писать его на диск.

Две функции для нашей схемы:
- `hmac(keyring_dir, data)` — детерминированный HMAC-SHA256 на НЕИЗВЛЕКАЕМОМ TPM-ключе.
  Это «аппаратный гейт»: без TPM значение не вычислить, поэтому офлайн-перебор ПИНа
  по украденным файлам невозможен. ПИН — это вход HMAC (а не auth ключа), поэтому TPM
  считает mac для ЛЮБОГО ПИНа — это и позволяет нашей логике различать прямой/обратный/
  неверный ПИН на уровне приложения.
- NV-счётчик (`counter_*`) — монотонный аппаратный счётчик попыток: его нельзя сбросить
  удалением файла (для устойчивой нарастающей блокировки; значение абсолютное —
  используем дельту от базлайна).

ВАЖНО: ключ привязан к ЭТОМУ TPM. tpm2_clear/смена владельца → ключ нечитаем → данные
невосстановимы локально (восстановление — со второго устройства). Это by design.
"""

from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

# Шаблон primary фиксирован → ключ детерминирован (тот же primary после перезагрузки,
# пока не очищен владелец). Все вызовы createprimary используют РОВНО эти параметры.
_PRIMARY_ARGS = ["-C", "o", "-g", "sha256", "-G", "ecc"]

_lock = threading.RLock()
# Кэш загруженного ключа на процесс: {"kd": <str>, "wd": Path, "hmac_ctx": <str>}
_ctx: dict | None = None


class TpmError(RuntimeError):
    """Ошибка при обращении к TPM (команда tpm2-tools завершилась с ошибкой)."""


def _run(args: list[str], input_bytes: bytes | None = None) -> bytes:
    """Запустить tpm2-инструмент. Возвращает stdout (bytes). Бросает TpmError."""
    try:
        p = subprocess.run(args, capture_output=True, input=input_bytes, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise TpmError(f"{args[0]}: запуск не удался: {e}") from e
    if p.returncode != 0:
        msg = p.stderr.decode("utf-8", "replace").strip()[:300]
        raise TpmError(f"{args[0]} rc={p.returncode}: {msg}")
    return p.stdout


def available() -> bool:
    """Доступен ли TPM (есть устройство и права)."""
    try:
        _run(["tpm2_getrandom", "--hex", "1"])
        return True
    except TpmError:
        return False


# --- неизвлекаемый HMAC-ключ ---

def _key_dir(keyring_dir) -> Path:
    d = Path(keyring_dir) / "tpm"
    d.mkdir(parents=True, exist_ok=True)
    return d


def has_hmac_key(keyring_dir) -> bool:
    kd = _key_dir(keyring_dir)
    return (kd / "hmac.pub").exists() and (kd / "hmac.priv").exists()


def ensure_hmac_key(keyring_dir) -> None:
    """Создать TPM HMAC-ключ (pub/priv), если его ещё нет. priv — TPM-обёрнут
    (бесполезен без этого TPM), хранится в keyring_dir/tpm/."""
    with _lock:
        if has_hmac_key(keyring_dir):
            return
        kd = _key_dir(keyring_dir)
        with tempfile.TemporaryDirectory(prefix="qtnotes-tpm-") as wd:
            wd = Path(wd)
            _run(["tpm2_createprimary", *_PRIMARY_ARGS, "-c", str(wd / "primary.ctx")])
            _run(["tpm2_create", "-C", str(wd / "primary.ctx"), "-G", "hmac",
                  "-u", str(kd / "hmac.pub"), "-r", str(kd / "hmac.priv")])


def _loaded_ctx(keyring_dir) -> str:
    """Путь к загруженному контексту HMAC-ключа (кэшируется на процесс)."""
    global _ctx
    ensure_hmac_key(keyring_dir)
    kd = _key_dir(keyring_dir)
    if _ctx is not None and _ctx["kd"] == str(kd):
        return _ctx["hmac_ctx"]
    wd = Path(tempfile.mkdtemp(prefix="qtnotes-tpm-"))
    _run(["tpm2_createprimary", *_PRIMARY_ARGS, "-c", str(wd / "primary.ctx")])
    _run(["tpm2_load", "-C", str(wd / "primary.ctx"),
          "-u", str(kd / "hmac.pub"), "-r", str(kd / "hmac.priv"),
          "-c", str(wd / "hmac.ctx")])
    _ctx = {"kd": str(kd), "wd": wd, "hmac_ctx": str(wd / "hmac.ctx")}
    return _ctx["hmac_ctx"]


def hmac(keyring_dir, data: bytes) -> bytes:
    """HMAC-SHA256(data) на неизвлекаемом TPM-ключе. Детерминирован; 32 байта."""
    with _lock:
        ctx = _loaded_ctx(keyring_dir)
        out = _run(["tpm2_hmac", "-c", ctx, "-g", "sha256"], input_bytes=data)
    if len(out) != 32:
        raise TpmError(f"неожиданная длина HMAC: {len(out)}")
    return out


def reset_cache() -> None:
    """Сбросить кэш загруженного ключа (тесты/смена keyring)."""
    global _ctx
    _ctx = None


# --- NV монотонный счётчик попыток ---

_COUNTER_ATTRS = "nt=counter|ownerread|ownerwrite|authread|authwrite"


def counter_exists(handle: str) -> bool:
    try:
        _run(["tpm2_nvreadpublic", handle])
        return True
    except TpmError:
        return False


def ensure_counter(handle: str) -> None:
    """Создать NV-счётчик, если его нет, и инициализировать (первый инкремент)."""
    with _lock:
        if counter_exists(handle):
            return
        _run(["tpm2_nvdefine", handle, "-C", "o", "-a", _COUNTER_ATTRS])
        _run(["tpm2_nvincrement", handle])  # активировать счётчик


def counter_read(handle: str) -> int:
    out = _run(["tpm2_nvread", handle])
    return int.from_bytes(out, "big")


def counter_increment(handle: str) -> int:
    """Увеличить счётчик на 1, вернуть новое значение."""
    with _lock:
        _run(["tpm2_nvincrement", handle])
        return counter_read(handle)


def counter_undefine(handle: str) -> None:
    """Удалить NV-счётчик (для тестов/сброса)."""
    try:
        _run(["tpm2_nvundefine", handle, "-C", "o"])
    except TpmError:
        pass
