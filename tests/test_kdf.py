"""M1: медленный KDF (scrypt) поверх аппаратного гейта + апгрейд legacy keyring при входе.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_kdf.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_legacy(kv, P, hw, pin):
    """Собрать keyring СТАРОГО формата (kdf=None) — как до M1."""
    sw, sd = P.random_bytes(16), P.random_bytes(16)
    mk = P.random_bytes(32)
    wrapped = P.seal(kv._wrap_key(hw, sw, pin, None), mk)         # без растяжения = legacy
    tag = kv._duress_tag(hw, sd, pin[::-1], None)
    return kv.KeyringState(version=1, salt_wrap=sw, salt_duress=sd,
                           wrapped_mk=wrapped, duress_tag=tag, kdf=None), mk


def main():
    from qtnotes.crypto import keyvault as kv
    from qtnotes.crypto import primitives as P
    from qtnotes.crypto.hwbackend import SoftwareHardwareKey

    hw = SoftwareHardwareKey.generate()

    # 1) новый keyring использует scrypt; OK/WRONG/DURESS работают
    t0 = time.time()
    state, mk = kv.setup("13579", hw)
    setup_ms = (time.time() - t0) * 1000
    assert state.kdf and state.kdf["algo"] == "scrypt", state.kdf
    t0 = time.time()
    _, res = kv.unlock(state, "13579", hw)
    unlock_ms = (time.time() - t0) * 1000
    assert res.status == kv.UnlockStatus.OK and res.master_key == mk
    _, rw = kv.unlock(state, "00000", hw)
    assert rw.status == kv.UnlockStatus.WRONG
    _, rd = kv.unlock(state, "97531", hw)  # обратный к 13579
    assert rd.status == kv.UnlockStatus.DURESS
    print(f"OK: новый keyring scrypt — OK/WRONG/DURESS (setup {setup_ms:.0f}мс, unlock {unlock_ms:.0f}мс)")
    assert unlock_ms < 2000, f"unlock слишком долгий: {unlock_ms:.0f}мс"

    # 2) legacy keyring (kdf=None) разворачивается И апгрейдится на scrypt при входе
    legacy, mk2 = _make_legacy(kv, P, hw, "24680")
    assert legacy.kdf is None
    ns2, res2 = kv.unlock(legacy, "24680", hw)
    assert res2.status == kv.UnlockStatus.OK and res2.master_key == mk2
    assert ns2.kdf and ns2.kdf["algo"] == "scrypt", "legacy должен апгрейдиться при входе"
    # после апгрейда тот же ПИН по-прежнему разворачивает (уже через scrypt)
    _, res3 = kv.unlock(ns2, "24680", hw)
    assert res3.status == kv.UnlockStatus.OK and res3.master_key == mk2
    # duress сохранился после апгрейда
    _, res4 = kv.unlock(ns2, "08642", hw)  # обратный к 24680
    assert res4.status == kv.UnlockStatus.DURESS
    # неверный после апгрейда — WRONG
    _, res5 = kv.unlock(ns2, "11111", hw)
    assert res5.status == kv.UnlockStatus.WRONG
    print("OK: legacy keyring разворачивается, апгрейдится на scrypt, duress/wrong сохранены")

    # 3) сериализация сохраняет kdf
    d = state.to_dict()
    assert d["kdf"] == state.kdf
    state2 = kv.KeyringState.from_dict(d)
    assert state2.kdf == state.kdf
    _, res6 = kv.unlock(state2, "13579", hw)
    assert res6.status == kv.UnlockStatus.OK
    print("OK: kdf сериализуется и читается обратно")

    print("ALL KDF TESTS PASSED")


if __name__ == "__main__":
    main()
