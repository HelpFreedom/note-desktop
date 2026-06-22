"""P8: импорт zip защищён от path-traversal и symlink-escape.

- член с '..' в пути не пишется наружу;
- запись сквозь существующий симлинк, ведущий за пределы vault, отклоняется;
- легитимный член импортируется.

Запуск: .venv/bin/python tests/test_zip_slip.py
"""

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _env(root):
    os.environ["QTNOTES_VAULT"] = os.path.join(root, "vault")
    os.environ["XDG_CONFIG_HOME"] = os.path.join(root, "config")
    os.environ["XDG_DATA_HOME"] = os.path.join(root, "data")


def main():
    from pathlib import Path
    with tempfile.TemporaryDirectory() as root:
        _env(root)
        from qtnotes import config
        from qtnotes.storage import exporter

        outside = Path(root) / "OUTSIDE"
        outside.mkdir()
        vault = config.vault_dir()  # создаёт vault

        # симлинк внутри vault, ведущий наружу
        (vault / "folders").mkdir(parents=True, exist_ok=True)
        link = vault / "folders" / "link"
        link.symlink_to(outside, target_is_directory=True)

        zp = Path(root) / "evil.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("folders/good/note.json", b'{"ok":1}')           # легитимный
            z.writestr("folders/../../escape.txt", b"pwned")            # traversal
            z.writestr("folders/link/through_symlink.txt", b"pwned")    # сквозь симлинк

        extracted = exporter.import_archive(zp)

        # наружу ничего не утекло
        assert not (Path(root) / "escape.txt").exists(), "traversal записал файл наружу!"
        assert not (outside / "through_symlink.txt").exists(), "запись прошла сквозь симлинк!"
        # легитимный член — на месте
        assert (vault / "folders" / "good" / "note.json").exists(), "легитимный член не импортирован"
        assert extracted == 1, f"ожидали 1 безопасный член, извлекли {extracted}"
    print("ZIP-SLIP OK: traversal и symlink-escape отклонены, легитимный член импортирован")
    print("ALL P8 TESTS PASSED")


if __name__ == "__main__":
    main()
