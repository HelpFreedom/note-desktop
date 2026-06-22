"""Desktop UI регрессии (взаимодействие с F3-чанковым рендером):

 1) scroll_to_note на БОЛЬШОЙ папке: переход к заметке (клик по результату поиска /
    [[ссылке]]) обязан сработать, даже если цель в ещё не отрендеренном чанке. Раньше
    go() искал баббл один раз через singleShot(0); при чанковом рендере цель ещё не
    создана → переход молча проваливался (прокрутка в никуда).

 2) Выделение НЕ должно теряться при sync-refresh: пользователь выделил заметки, пришёл
    синк → refresh_folder → _clear_bubbles → _selected.clear() молча сбрасывал выбор
    перед тем как юзер жмёт Удалить/Переместить.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_chat_nav_select.py
"""

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _wait(app, pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end and not pred():
        app.processEvents()
        time.sleep(0.01)
    return pred()


def _bubble(cv, note_id):
    for b in cv._bubbles:
        if b.note.id == note_id:
            return b
    return None


def main():
    base = tempfile.mkdtemp(prefix="qtnotes-navsel-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "cfg")
    os.environ["QTNOTES_VAULT"] = os.path.join(base, "vault")
    os.makedirs(os.environ["XDG_CONFIG_HOME"]); os.makedirs(os.environ["QTNOTES_VAULT"])

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import oplog
    from qtnotes.ui.chat_view import ChatView
    oplog.reset_for_tests(); index.reset_for_tests()

    try:
        # === 1) scroll_to_note на большой папке (цель в позднем чанке) ===
        big = vault.create_folder("Большая")
        ids = []
        for i in range(100):                       # >2 чанков по 40
            n = Note.create_text(big.id, f"<p>{i}</p>", f"заметка {i}")
            vault.save_note(n)
            ids.append(n.id)
        target = ids[90]                           # заведомо в последнем чанке

        cv = ChatView()
        cv.show_folder(big)
        # сразу просим переход — как main_window._on_reference (рендер ещё не закончен)
        cv.scroll_to_note(target)
        assert _wait(app, lambda: len(cv._bubbles) == 100), "большая лента не отрендерилась"
        # после фикса целевой баббл найден и подсвечен; до фикса — никогда
        ok = _wait(app, lambda: (_bubble(cv, target) is not None
                                 and _bubble(cv, target).is_selected()), timeout=3.0)
        assert ok, ("scroll_to_note не дошёл до цели в позднем чанке — переход по "
                    "результату/ссылке в большую папку молча проваливается")
        print("OK: scroll_to_note доходит до заметки даже в ещё-не-отрендеренном чанке")

        # === 2) выделение переживает sync-refresh ===
        small = vault.create_folder("Малая")
        a = Note.create_text(small.id, "<p>a</p>", "a"); vault.save_note(a)
        b = Note.create_text(small.id, "<p>b</p>", "b"); vault.save_note(b)
        c = Note.create_text(small.id, "<p>c</p>", "c"); vault.save_note(c)

        cv.show_folder(small)
        assert _wait(app, lambda: len(cv._bubbles) == 3), "малая лента не отрендерилась"
        cv._toggle_selection(a.id)
        cv._toggle_selection(c.id)
        assert cv._selected == {a.id, c.id}, "подготовка: должны быть выделены a и c"

        # пришёл синк: новая заметка d в этой же папке → refresh_folder
        d = Note.create_text(small.id, "<p>d</p>", "d"); vault.save_note(d)
        cv.refresh_folder(small)
        assert _wait(app, lambda: len(cv._bubbles) == 4), "лента не обновилась входящей заметкой"

        assert cv._selected == {a.id, c.id}, (
            f"❌ выделение сброшено при sync-refresh: было {{a,c}}, стало {cv._selected}")
        # и подсветка восстановлена на уцелевших бабблах
        assert _bubble(cv, a.id).is_selected() and _bubble(cv, c.id).is_selected(), \
            "подсветка выделения не восстановлена после refresh"
        assert not _bubble(cv, d.id).is_selected(), "новая заметка не должна быть выделена"
        print("OK: выделение и подсветка сохраняются через sync-refresh")

        print("ALL CHAT NAV/SELECT TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
