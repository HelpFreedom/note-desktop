"""Регрессия (data-loss класс): save_settings обязан быть КРАШ-БЕЗОПАСНЫМ.

settings.json хранит путь к хранилищу (vault_path) и флаг шифрования — то есть КАКОЙ
vault открывать и шифровать ли его. Если запись прервётся на середине (сбой питания,
OOM-kill, диск полон), файл не должен оказаться битым: иначе load_settings вернёт {},
приложение откроет дефолтный ПУСТОЙ vault (реальный «исчезнет») и тихо выключит шифрование.

Тест моделирует обрыв записи (перехват os.write: пишет половину байт и бросает) и требует,
чтобы на диске остались ПРЕЖНИЕ валидные настройки. Красный на нативном write_text,
зелёный после перехода на atomic_write_bytes (tmp+fsync+replace: dest не трогается до
успешного коммита).

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_settings_atomic.py
"""

import builtins
import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from qtnotes import config

    base = tempfile.mkdtemp(prefix="qtnotes-settcrash-")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(base, "cfg")
    try:
        # 1) контрольная запись: настройки реально сохраняются и читаются
        good = {"vault_path": "/home/user/real-vault", "encryption_enabled": True,
                "font_size": 14}
        config.save_settings(good)
        assert config.load_settings() == good, "контрольная запись не сохранилась"

        dest = str(config.settings_path())

        # 2) структурный инвариант атомарности: целевой файл НЕЛЬЗЯ открывать на прямую
        # запись (обрыв на середине оставил бы битый settings.json → потеря vault_path).
        # Атомарная запись пишет в tmp и подменяет через os.replace. Перехватываем оба
        # io.open/builtins.open (pathlib зовёт io.open) и os.replace.
        opened_for_write = []
        replaced = []
        real_io_open, real_bi_open, real_replace = io.open, builtins.open, os.replace

        def track_open(file, mode="r", *a, **k):
            try:
                m = mode if isinstance(mode, str) else "r"
                if any(c in m for c in ("w", "a", "x", "+")):
                    opened_for_write.append(os.fspath(file))
            except TypeError:
                pass
            return real_io_open(file, mode, *a, **k)

        def track_replace(src, dst, *a, **k):
            replaced.append((os.fspath(src), os.fspath(dst)))
            return real_replace(src, dst, *a, **k)

        io.open = builtins.open = track_open
        os.replace = track_replace
        try:
            new = {"vault_path": "/home/user/real-vault", "encryption_enabled": True,
                   "font_size": 18}
            config.save_settings(new)
        finally:
            io.open, builtins.open, os.replace = real_io_open, real_bi_open, real_replace

        assert dest not in opened_for_write, (
            "❌ НЕАТОМАРНО: save_settings открыл целевой settings.json на прямую запись — "
            "обрыв питания в середине оставит битый файл и потеряет vault_path/шифрование. "
            "Нужна запись через tmp + atomic_write_bytes.")
        assert any(d == dest for _, d in replaced), (
            "целевой settings.json должен появляться атомарно через os.replace из tmp")

        # 3) и обновление при этом действительно применилось
        assert config.load_settings() == new, "после атомарной записи настройки не обновились"
        print("OK: settings.json пишется атомарно (tmp+replace), целевой файл не трогается напрямую")
        print("ALL SETTINGS-ATOMICITY TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
