"""H7/M7: фоновая загрузка папки и приём вложений вне UI-потока создают бабблы в UI.

Гоняет реальные QThread-воркеры под offscreen-Qt и ждёт доставки сигналов. Проверяет
ЛОГИКУ потоков (воркер→сигнал→баббл), но не визуальный рендер (его подтверждает
пользователь в X-сессии).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_chat_view_async.py
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
    base = tempfile.mkdtemp(prefix="qtnotes-chatasync-")
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
        f = vault.create_folder("F")
        for i in range(3):
            vault.save_note(Note.create_text(f.id, f"<p>n{i}</p>", f"n{i}"))

        from qtnotes.ui.chat_view import ChatView
        cv = ChatView()

        # H7: фоновая загрузка папки → бабблы появляются в UI-потоке
        cv.show_folder(f)
        assert _wait(app, lambda: len(cv._bubbles) == 3), \
            f"H7: бабблы не создались фоновой загрузкой: {len(cv._bubbles)}"
        print("OK: H7 фоновый декрипт папки создал бабблы в UI-потоке")

        # отбрасывание устаревшей загрузки: быстрый повторный show_folder с новой папкой
        f2 = vault.create_folder("F2")
        vault.save_note(Note.create_text(f2.id, "<p>x</p>", "x"))
        cv.show_folder(f2)
        assert _wait(app, lambda: len(cv._bubbles) == 1), \
            f"переключение папки: ожидался 1 баббл, got {len(cv._bubbles)}"
        print("OK: переключение папки — устаревший результат отброшен (gen)")

        # M7: фоновый приём вложения → баббл
        tmp_file = os.path.join(base, "doc.txt")
        with open(tmp_file, "w") as fh:
            fh.write("data")
        cv.show_folder(f)
        assert _wait(app, lambda: len(cv._bubbles) == 3)
        cv._attach_paths([tmp_file])
        assert cv._pending, "вложение не добавилось в лоток"
        cv._send_pending("")
        assert _wait(app, lambda: len(cv._bubbles) == 4), \
            f"M7: баббл вложения не создан: {len(cv._bubbles)}"
        print("OK: M7 фоновый приём вложения создал баббл, лоток очищен")
        assert not cv._pending

        print("ALL CHAT-VIEW ASYNC SMOKE PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
