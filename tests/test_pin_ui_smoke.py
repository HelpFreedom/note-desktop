"""Offscreen-смоук экранов ПИНа (Ф4b): строятся без падений, логика ввода работает.

Живой вид проверяет пользователь; здесь — что виджеты создаются, ввод цифр доходит до
completed, настройка требует совпадения и отвергает палиндром, разблокировка реагирует
на верный/неверный ПИН.

Запуск:
    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_pin_ui_smoke.py
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)


def _feed(entry, digits: str) -> None:
    for d in digits:
        entry._add_digit(d)
    QTest.qWait(150)  # дать сработать отложенному completed (singleShot 60мс)


def run_setup() -> None:
    from qtnotes.ui.pin_dialog import PinSetupDialog

    dlg = PinSetupDialog()

    # палиндром отвергается на первом вводе
    _feed(dlg._entry, "12321")
    assert dlg._first is None, "палиндром не должен приниматься"
    assert dlg.pin() is None

    # корректный ПИН, но второй ввод не совпал → сброс к первому шагу
    _feed(dlg._entry, "13579")
    assert dlg._first == "13579"
    _feed(dlg._entry, "97531")
    assert dlg._first is None, "несовпадение должно сбрасывать"
    assert dlg.pin() is None

    # совпадение → принято
    _feed(dlg._entry, "13579")
    _feed(dlg._entry, "13579")
    assert dlg.pin() == "13579"
    assert dlg.result() == QDialog.Accepted
    print("OK setup-экран: палиндром отвергнут, несовпадение сброшено, совпадение принято")


def run_unlock() -> None:
    from qtnotes.crypto.keyvault import UnlockResult, UnlockStatus
    from qtnotes.ui.pin_dialog import PinUnlockDialog

    def check(pin):
        if pin == "13579":
            return UnlockResult(UnlockStatus.OK, master_key=b"\x00" * 32)
        return UnlockResult(UnlockStatus.WRONG, fail_count=1)

    dlg = PinUnlockDialog(check=check, remaining=lambda: 0)
    _feed(dlg._entry, "00000")
    assert dlg.result() != QDialog.Accepted  # неверный — не принят
    _feed(dlg._entry, "13579")
    assert dlg.result() == QDialog.Accepted  # верный — принят
    print("OK unlock-экран: неверный отклонён, верный принят")


def run_lockout_view() -> None:
    """При remaining>0 пад заблокирован и показан отсчёт."""
    from qtnotes.crypto.keyvault import UnlockResult, UnlockStatus
    from qtnotes.ui.pin_dialog import PinUnlockDialog

    dlg = PinUnlockDialog(check=lambda p: UnlockResult(UnlockStatus.LOCKED, retry_after=90),
                          remaining=lambda: 90)
    QTest.qWait(50)
    assert not dlg._entry._pad.isEnabled(), "при блокировке numpad должен быть отключён"
    print("OK lockout-вид: numpad заблокирован при активной блокировке")


def main() -> None:
    run_setup()
    run_unlock()
    run_lockout_view()
    print("\nСМОУК ЭКРАНОВ ПИНА ПРОЙДЕН")


if __name__ == "__main__":
    main()
