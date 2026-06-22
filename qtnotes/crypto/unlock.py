"""Контроллер разблокировки (платформонезависимая логика Ф4a).

Связывает воедино:
- keyvault (обёртка MK, различение прямой/обратный/неверный ПИН, базовый lockout);
- аппаратный бэкенд (TpmHardwareKey на десктопе; инъекция software в тестах);
- NV-счётчик TPM как «пол» для счётчика неудач — устойчивость блокировки к удалению
  файла keyring (см. ниже);
- session (MK в памяти процесса) и индекс (перестройка/очистка).

GUI (Ф4b) вызывает: is_configured(), setup_pin(), try_unlock(), lock(), remaining_lockout().

Состояние на диске — `<keyring>/keyring.json`:
    {"keyring": <KeyringState>, "nv_baseline": int, "backend": "tpm"|"software"}

NV-«пол»: счётчик неудач берётся как max(файловый, NV−baseline). Удаление/откат
keyring.json не уменьшит его ниже аппаратного значения. Удаление файла целиком =
потеря обёртки MK (данные не расшифровать), а не обход блокировки.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config
from . import keyvault as KV
from . import primitives as P
from . import session
from .hwbackend import HardwareKey, TpmHardwareKey
from .keyvault import KeyringState, UnlockResult, UnlockStatus

_KEYRING_FILE = "keyring.json"
# Боевой NV-индекс счётчика блокировки (тесты подменяют на свой и удаляют).
NV_HANDLE = "0x01800002"

# Integrity-MAC keyring: HMAC на ТОМ ЖЕ неизвлекаемом аппаратном ключе (backend.mac),
# что и разблокировка → на TPM он надёжен ровно настолько, насколько работает вход.
# Защищает fail_count/nv_baseline от подделки (иначе перевод nv_baseline отменяет
# аппаратный «пол» и открывает офлайн-перебор). Подделка → НЕразрушающая блокировка,
# НИКОГДА не стирание (false-positive в худшем случае восстановим из бэкапа).
_MAC_SALT = b"qtnotes/keyring-integrity/v1"


class NotConfiguredError(RuntimeError):
    pass


def keyring_path() -> Path:
    return config.keyring_dir() / _KEYRING_FILE


def is_configured() -> bool:
    return keyring_path().exists()


def default_backend() -> HardwareKey:
    """Боевой бэкенд десктопа — TPM. Бросает, если TPM недоступен."""
    from . import tpm
    if not tpm.available():
        raise RuntimeError("TPM недоступен — шифрование на этом устройстве невозможно")
    return TpmHardwareKey(config.keyring_dir())


@dataclass
class _Stored:
    state: KeyringState
    nv_baseline: int
    backend: str
    mac: str | None = None  # integrity-MAC; None у legacy-файлов (до этой версии)


def _payload(state: KeyringState, nv_baseline: int, backend_name: str) -> dict:
    kr = state.to_dict()
    # Обратная совместимость integrity-MAC: поле `kdf` добавлено позже (M1, медленный KDF).
    # У legacy-keyring его нет, и его MAC считался без него. Если kdf отсутствует (None) —
    # НЕ включаем ключ в канонический payload, иначе MAC старого файла ложно «не сходится»
    # → ложная блокировка. Когда kdf задан (после апгрейда на scrypt) — он попадает в MAC.
    if kr.get("kdf") is None:
        kr.pop("kdf", None)
    return {"keyring": kr, "nv_baseline": nv_baseline, "backend": backend_name}


def _compute_mac(backend: HardwareKey, state: KeyringState, nv_baseline: int,
                 backend_name: str) -> str:
    """Детерминированный MAC над защищаемыми полями на аппаратном ключе."""
    canonical = json.dumps(_payload(state, nv_baseline, backend_name),
                           sort_keys=True, separators=(",", ":"))
    return backend.mac(_MAC_SALT, canonical).hex()


def _compute_mac_retry(backend: HardwareKey, state: KeyringState, nv_baseline: int,
                       backend_name: str, attempts: int = 3) -> str:
    """MAC с ретраями: TPM мог быть кратко занят (RC_RETRY/контеншн). При стойком
    сбое пробрасываем — вызывающий НЕ должен записать keyring без mac (E1)."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            return _compute_mac(backend, state, nv_baseline, backend_name)
        except Exception as e:  # noqa: BLE001 — соберём и перебросим после ретраев
            last = e
    raise last if last is not None else RuntimeError("MAC недоступен")


def _verify(stored: _Stored, backend: HardwareKey | None) -> bool:
    """True, если файл цел или legacy (без MAC). False — только при ЯВНОЙ подделке
    (MAC присутствует, но не сходится). Без backend проверить нельзя → считаем целым.

    При сбое вычисления MAC (аппаратная заминка) НЕ блокируем — возвращаем True: лучше
    не сработавшая защита, чем ложный лок-аут реального хранилища."""
    if backend is None or stored.mac is None:
        return True  # legacy/нет ключа: обратная совместимость, не блокируем
    try:
        expected = _compute_mac(backend, stored.state, stored.nv_baseline, stored.backend)
        return P.const_eq(bytes.fromhex(expected), bytes.fromhex(stored.mac))
    except Exception:  # noqa: BLE001 — сбой MAC не должен блокировать вход
        return True


def _read() -> _Stored | None:
    p = keyring_path()
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return _Stored(
        state=KeyringState.from_dict(d["keyring"]),
        nv_baseline=int(d.get("nv_baseline", 0)),
        backend=d.get("backend", "tpm"),
        mac=d.get("mac"),
    )


def _write(stored: _Stored, backend: HardwareKey | None = None) -> None:
    from .. import fsutil
    p = keyring_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = _payload(stored.state, stored.nv_baseline, stored.backend)
    if backend is not None:
        # E1 (раунд-3): НЕ глотаем сбой MAC молча. Раньше при ошибке TPM keyring
        # записывался БЕЗ поля mac, а _verify такому файлу доверяет вечно (legacy) →
        # атакующий, спровоцировав единичную ошибку TPM в момент записи, навсегда
        # срезал бы защиту счётчика перебора. Ретраим; при стойком сбое — пробрасываем
        # (лучше явная ошибка входа «попробуйте ещё раз», чем тихое ослабление защиты).
        payload["mac"] = _compute_mac_retry(backend, stored.state, stored.nv_baseline,
                                            stored.backend)
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    fsutil.atomic_write_bytes(p, data)  # durable: tmp+fsync+replace+fsync(dir)


def _uses_nv(backend: HardwareKey) -> bool:
    return isinstance(backend, TpmHardwareKey)


def _nv_count(backend: HardwareKey) -> int:
    from . import tpm
    tpm.ensure_counter(NV_HANDLE)
    return tpm.counter_read(NV_HANDLE)


# --- публичный API ---

def setup_pin(pin: str, backend: HardwareKey | None = None,
              with_duress: bool = True) -> bytes:
    """Первичная настройка ПИНа: создать обёртку MK, включить шифрование, разблокировать.
    Возвращает MK. Бросает keyvault.PinError при плохом ПИНе.

    with_duress=False — для подложки после duress-стирания: у неё нет обратного ПИНа,
    чтобы исходный (прямой) ПИН не имел никакого особого смысла и не оставлял следа."""
    backend = backend or default_backend()
    KV.validate_pin(pin)
    state, mk = KV.setup(pin, backend, with_duress=with_duress)
    baseline = _nv_count(backend) if _uses_nv(backend) else 0
    backend_name = "tpm" if _uses_nv(backend) else "software"
    _write(_Stored(state=state, nv_baseline=baseline, backend=backend_name), backend)
    session.set_master_key(mk)
    config.set_encryption_enabled(True)
    return mk


def remaining_lockout(backend: HardwareKey | None = None, now: float | None = None) -> int:
    """Сколько секунд ещё длится блокировка (0 — можно вводить)."""
    stored = _read()
    if stored is None:
        return 0
    state = _effective_state(stored, backend)
    return KV.remaining_lockout(state, now)


def _effective_state(stored: _Stored, backend: HardwareKey | None) -> KeyringState:
    """Состояние с поднятым по NV счётчиком неудач (аппаратный «пол»).

    Если обнаружена подделка integrity-MAC — НЕ доверяем счётчику/baseline из файла и
    выставляем длительную НЕразрушающую блокировку (24ч по расписанию). Стирание отсюда
    не инициируется никогда (см. try_unlock): ложное срабатывание восстановимо из бэкапа."""
    state = stored.state
    if not _verify(stored, backend):
        return KeyringState(**{**state.__dict__,
                               "fail_count": 6,  # ≥6 → максимум расписания (24ч)
                               "last_fail_ts": time.time()})
    if backend is not None and _uses_nv(backend):
        nv_fails = max(0, _nv_count(backend) - stored.nv_baseline)
        if nv_fails > state.fail_count:
            state = KeyringState(**{**state.__dict__, "fail_count": nv_fails})
    return state


def try_unlock(pin: str, backend: HardwareKey | None = None,
               now: float | None = None) -> UnlockResult:
    """Попытка разблокировки. На OK кладёт MK в сессию и готовит индекс. На DURESS
    НЕ разблокирует (стирание — Ф6). Состояние/NV обновляются и сохраняются."""
    backend = backend or default_backend()
    stored = _read()
    if stored is None:
        raise NotConfiguredError("ПИН не настроен")

    state = _effective_state(stored, backend)
    # ПИН проверяется (KV.unlock тестирует прямой/обратный/неверный + таймерная блокировка),
    # затем решаем про самостирание — чтобы верный 6-й ввод НЕ стёр данные.
    new_state, res = KV.unlock(state, pin, backend, now=now)

    # DURESS: обратный ПИН → необратимое стирание реальных данных и создание подложки.
    # Снаружи это выглядит как ОБЫЧНАЯ разблокировка (открываемся в подложку) — без
    # предупреждений, как и задумано. Состояние НЕ сохраняем (всё стирается).
    if res.status is UnlockStatus.DURESS:
        from . import duress
        decoy_mk = duress.execute(pin, backend)
        _prepare_after_unlock()
        return UnlockResult(UnlockStatus.OK, master_key=decoy_mk)

    if res.status is UnlockStatus.WRONG and _uses_nv(backend):
        from . import tpm
        tpm.counter_increment(NV_HANDLE)  # аппаратный счётчик неудач (нельзя сбросить)
        # пересчитать эффективный счётчик по NV (актуально после инкремента)
        new_state = KeyringState(**{**new_state.__dict__,
                                    "fail_count": max(0, _nv_count(backend) - stored.nv_baseline)})

    # САМОСТИРАНИЕ: подтверждённый неверный ПИН сверх лимита → стереть всё безвозвратно.
    # Только на устройстве с аппаратным счётчиком (NV) — иначе лимит обходится подделкой
    # файла. Таймерные блокировки при этом продолжают работать (на попытках 2..5).
    # стирать только если файл НЕ подделан (подделка → LOCKED выше, сюда не дойдём;
    # но guard явный: распознанная подделка никогда не инициирует разрушение).
    if (res.status is UnlockStatus.WRONG and _uses_nv(backend)
            and new_state.fail_count > KV.WIPE_AFTER_FAILS
            and _verify(stored, backend)):
        from . import duress
        duress.wipe_and_reset()
        return UnlockResult(UnlockStatus.WIPED, fail_count=new_state.fail_count)

    new_baseline = stored.nv_baseline
    if res.status is UnlockStatus.OK and _uses_nv(backend):
        new_baseline = _nv_count(backend)  # сбросить «пол»: nv_fails станет 0

    _write(_Stored(state=new_state, nv_baseline=new_baseline, backend=stored.backend),
           backend)

    if res.status is UnlockStatus.OK:
        session.set_master_key(res.master_key)
        _prepare_after_unlock()
    return res


def _prepare_after_unlock() -> None:
    """После разблокировки: построить индекс (tmpfs пуст после старта)."""
    try:
        from ..storage import index
        index.ensure_ready()
    except Exception:  # noqa: BLE001 — индекс перестраиваемый, не валим разблокировку
        pass


def lock() -> None:
    """Заблокировать: забыть MK и стереть эфемерные plaintext-кэши (индекс, блобы)."""
    session.lock()
    try:
        from ..storage import index, vault
        index.wipe_ephemeral()
        vault.wipe_blob_cache()
    except Exception:  # noqa: BLE001
        pass
