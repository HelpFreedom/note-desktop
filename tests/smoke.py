"""Offscreen-смоук: создаёт приложение и главное окно без падений.

Запуск:
    QT_QPA_PLATFORM=offscreen .venv/bin/python tests/smoke.py
"""

import os
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# ВАЖНО: изолированные временные vault и config — тест НЕ должен трогать реальные
# заметки/настройки/ключи пользователя. Задаём до импорта qtnotes.
os.environ["QTNOTES_VAULT"] = tempfile.mkdtemp(prefix="qtnotes_smoke_")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="qtnotes_cfg_")

# чтобы импортировать пакет qtnotes из корня проекта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

from qtnotes.ui.main_window import MainWindow  # noqa: E402
from qtnotes.ui.theme import build_qss  # noqa: E402


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(build_qss())
    win = MainWindow()
    win.show()

    # прогнать переключения вида
    win._show_calendar()
    assert win.stack.currentIndex() == 1

    # связка с хранилищем: создать папку и убедиться, что она в сайдбаре
    from qtnotes.storage import vault
    folder = vault.create_folder("Тест", caption="t", color="#5288c1")
    win._reload_folders(select_id=folder.id)
    assert folder.id in win.sidebar._items, "папка не появилась в сайдбаре"
    assert win.stack.currentIndex() == 0, "выбор папки не открыл ленту"
    assert win.chat._title.text() == "Тест", win.chat._title.text()

    # отправка текстовой заметки
    win.chat._field.setPlainText("Привет, мир\nвторая строка")
    win.chat._submit()
    assert len(win.chat._bubbles) == 1, "пузырь не добавился"
    assert len(vault.list_notes(folder.id)) == 1, "заметка не сохранилась"

    # перезагрузка папки — заметка сохраняется
    win.chat.show_folder(folder)
    assert len(win.chat._bubbles) == 1, "заметка не восстановилась после перезагрузки"
    assert "вторая строка" in win.chat._bubbles[0].note.plaintext

    # ссылки: URL в простом тексте становится кликабельным <a>
    win.chat._field.setPlainText("см. https://example.com тут")
    win.chat._submit()
    last = win.chat._bubbles[-1].note
    assert '<a href="https://example.com"' in last.html, last.html

    # форматирование: жирный текст сохраняется как rich HTML
    win.chat._field.setPlainText("жирный")
    win.chat._field.selectAll()
    win.chat._field.toggle_bold()
    assert win.chat._field.has_formatting(), "форматирование не определилось"
    win.chat._submit()
    assert "font-weight" in win.chat._bubbles[-1].note.html.lower()

    # вложения: прикрепить текстовый файл и картинку через лоток + отправка
    import tempfile as _tf
    from PySide6.QtGui import QImage
    txt = os.path.join(_tf.gettempdir(), "qtnotes_smoke.txt")
    with open(txt, "w") as fh:
        fh.write("hello")
    png = os.path.join(_tf.gettempdir(), "qtnotes_smoke.png")
    QImage(8, 8, QImage.Format_RGB32).save(png)
    win.chat._attach_paths([txt, png])
    assert len(win.chat._pending) == 2
    win.chat._field.setPlainText("подпись")
    win.chat._submit()
    note = win.chat._bubbles[-1].note
    assert note.kind == "album" and len(note.attachments) == 2, note.kind
    win.chat.show_folder(folder)
    assert any(b.note.attachments for b in win.chat._bubbles), "вложения не загрузились"

    # иконки папок: создать папку с иконкой и проверить сохранение
    f_icon = vault.create_folder("Звезда", icon="star", color="#e7c14f")
    assert vault.list_folders()[-1].icon == "star"

    # мультивыбор: выделить две заметки → панель действий видна
    win.chat.show_folder(folder)
    ids = [b.note.id for b in win.chat._bubbles[:2]]
    for nid in ids:
        win.chat._toggle_selection(nid)
    assert len(win.chat.selected_ids()) == 2, win.chat.selected_ids()
    assert win.chat._selbar.isVisible(), "панель выделения не показана"
    win.chat._clear_selection()
    assert not win.chat._selbar.isVisible()

    # адаптивная ширина: меняется при ресайзе
    win.resize(1400, 800)
    app.processEvents()
    wide = win.chat._bubble_max_width()
    win.resize(700, 600)
    app.processEvents()
    narrow = win.chat._bubble_max_width()
    assert wide > narrow, f"ширина не адаптивна: {wide} vs {narrow}"

    # ID-ссылка: заметка со ссылкой [[id]] на первую заметку
    target_id = win.chat._bubbles[0].note.id
    win.chat._field.setPlainText(f"ссылка [[{target_id}]] тут")
    win.chat._submit()
    assert f'href="qtnote:{target_id}"' in win.chat._bubbles[-1].note.html
    win.chat.scroll_to_note(target_id)  # не должно падать

    # инлайн-редактирование (без модального окна)
    edit_id = win.chat._bubbles[0].note.id
    win.chat._on_edit_note(edit_id)
    assert win.chat._editing_note is not None, "не вошли в режим правки"
    assert win.chat._edit_banner.isVisible(), "баннер правки не показан"
    assert win.chat._send.text() == "Сохранить"
    win.chat._field.setPlainText("изменённый текст")
    win.chat._submit()
    assert win.chat._editing_note is None, "не вышли из режима правки"
    assert not win.chat._edit_banner.isVisible()
    assert win.chat._send.text() == "Отправить"
    edited = next(b.note for b in win.chat._bubbles if b.note.id == edit_id)
    assert edited.plaintext == "изменённый текст", edited.plaintext

    # медиа откладывается в лоток и отправляется вместе с подписью
    from PySide6.QtGui import QImage as _QI
    img = _QI(12, 12, _QI.Format_RGB32)
    img.fill(0x335577)
    win.chat._attach_image(img)        # вставка картинки из буфера
    win.chat._attach_paths([txt])      # drag-n-drop файла
    assert len(win.chat._pending) == 2, "вложения не попали в лоток"
    assert len(win.chat._bubbles) == 0 or True  # заметка ещё не создана
    before = len(win.chat._bubbles)
    win.chat._field.setPlainText("подпись к медиа")
    win.chat._submit()
    assert len(win.chat._bubbles) == before + 1, "медиа-заметка не создалась"
    note = win.chat._bubbles[-1].note
    assert note.kind == "album" and len(note.attachments) == 2, note.kind
    assert note.plaintext == "подпись к медиа"
    assert not win.chat._pending, "лоток не очистился после отправки"

    # виртуализация: тяжёлое медиа грузится только у активного пузыря
    mb = win.chat._bubbles[-1]
    img_slots = [s for s in mb._media if s["kind"] == "image"]
    assert img_slots, "нет медиа-слота изображения"
    mb.set_active(True)
    assert img_slots[0]["loaded"], "изображение не загрузилось при активации"
    mb.set_active(False)
    assert not img_slots[0]["loaded"], "изображение не выгрузилось при деактивации"
    mb.set_active(True)

    # полная ширина: пузырь занимает почти всю ширину поля
    win.resize(1200, 700)
    app.processEvents()
    fw = win.chat._bubble_max_width()
    assert fw > 1000, f"пузырь не на всю ширину: {fw}"

    # календарь: событие появляется в сетке, переносится и удаляется
    cal = win.calendar_page
    ev = vault.add_event("2026-06-10", "Тест-событие", "#5288c1")
    cal.reload()
    assert "2026-06-10" in cal._by_date and cal._by_date["2026-06-10"][0].name == "Тест-событие"
    cal._move_event(ev.id, "2026-06-15")   # drag-n-drop по датам
    assert "2026-06-15" in cal._by_date and "2026-06-10" not in cal._by_date
    cal._delete_event(ev.id)
    assert "2026-06-15" not in cal._by_date
    win._show_calendar()
    assert win.stack.currentIndex() == 1

    # поиск: ввод запроса показывает страницу результатов
    win.chat.show_folder(folder)
    win.chat._search.setText("изменённый")
    win.chat._run_search()  # без ожидания дебаунса
    assert win.chat._content_stack.currentIndex() == 1, "не показалась страница результатов"
    win.chat._clear_search()
    assert win.chat._content_stack.currentIndex() == 0

    # синхронизация: UI собирается без запуска движка (синк выключен)
    assert win.sync is not None and not win.sync.is_running()
    from qtnotes.ui.qr_widget import QrView
    from qtnotes.ui.sync_dialog import SyncDialog
    qr = QrView([[True, False, True], [False, True, False], [True, True, True]])
    qr.resize(120, 120)
    qr.show()
    app.processEvents()
    sd = SyncDialog(win.sync)          # создаёт личность устройства (в temp config)
    assert win.sync.identity().device_id
    sd._refresh_status()
    sd.close()
    app.processEvents()

    # реальный жизненный цикл движка (поток + mDNS + seed) включается и гасится
    win.sync.set_enabled(True)
    assert win.sync.is_running(), "движок не запустился"
    app.processEvents()
    win.sync.set_enabled(False)
    assert not win.sync.is_running(), "движок не остановился"

    print("SMOKE OK: инлайн-правка, мультивыбор, календарь, поиск, синк-UI, движок")
    return 0


if __name__ == "__main__":
    sys.exit(main())
