"""F1+F2 (раунд-3, десктоп UI):
 F1 — refresh_folder обновляет ленту текущей папки БЕЗ discard-модалки и СОХРАНЯЕТ ввод.
 F2 — sidebar.set_folders diff-обновляет элементы (переиспользует виджеты, не пересоздаёт),
      сохраняя выделение.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_sync_refresh_ui.py
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
        time.sleep(0.02)
    return pred()


def main():
    base = tempfile.mkdtemp(prefix="qtnotes-frefresh-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "cfg")
    os.environ["QTNOTES_VAULT"] = os.path.join(base, "vault")
    os.makedirs(os.environ["XDG_CONFIG_HOME"]); os.makedirs(os.environ["QTNOTES_VAULT"])

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from qtnotes.storage import index, vault
    from qtnotes.storage.models import Note
    from qtnotes.sync import oplog
    oplog.reset_for_tests(); index.reset_for_tests()

    try:
        # --- F2: sidebar diff-update ---
        from qtnotes.ui.sidebar import Sidebar

        class F:
            def __init__(self, i, n):
                self.id, self.name, self.caption, self.color, self.icon = i, n, "", None, "letter"

        sb = Sidebar()
        sb.set_folders([F("a", "A"), F("b", "B")])
        item_a = sb._items["a"]
        sb.select_folder("a")
        # переименовали a, добавили c, b на месте
        sb.set_folders([F("a", "A2"), F("b", "B"), F("c", "C")])
        assert sb._items["a"] is item_a, "F2: элемент 'a' должен ПЕРЕИСПОЛЬЗОВАТЬСЯ, не пересоздаваться"
        assert "c" in sb._items, "F2: новая папка добавлена"
        assert sb._active_id == "a", "F2: выделение сохранено"
        # удалили b и c
        sb.set_folders([F("a", "A2")])
        assert "b" not in sb._items and "c" not in sb._items, "F2: пропавшие удалены"
        assert sb._items["a"] is item_a, "F2: оставшийся элемент тот же объект"
        print("OK F2: сайдбар diff-обновляется (переиспользует виджеты, хранит выделение)")

        # --- F1: refresh_folder без discard-гарда, ввод сохраняется ---
        f = vault.create_folder("Папка")
        vault.save_note(Note.create_text(f.id, "<p>1</p>", "1"))

        from qtnotes.ui.chat_view import ChatView
        cv = ChatView()
        cv.show_folder(f)
        assert _wait(app, lambda: len(cv._bubbles) == 1), "лента не загрузилась"

        # пользователь печатает; в этот момент приходит синк (новая заметка)
        cv._field.setPlainText("черновик не потерять")
        vault.save_note(Note.create_text(f.id, "<p>2</p>", "2"))
        cv.refresh_folder(f)  # как из _do_sync_refresh — БЕЗ модалки
        assert _wait(app, lambda: len(cv._bubbles) == 2), "F1: лента не обновилась входящей заметкой"
        assert cv._field.toPlainText() == "черновик не потерять", \
            "F1: набранный текст НЕ должен теряться при синк-обновлении"
        print("OK F1: refresh_folder обновил ленту и сохранил черновик (без discard-модалки)")

        # refresh_folder другой папки — игнор (не текущая)
        other = vault.create_folder("Другая")
        before = len(cv._bubbles)
        cv.refresh_folder(other)
        app.processEvents()
        assert len(cv._bubbles) == before, "F1: refresh не той папки не должен менять ленту"
        print("OK F1: refresh чужой папки игнорируется")

        print("ALL SYNC-REFRESH UI (F1+F2) TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
