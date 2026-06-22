#!/usr/bin/env bash
# Запуск QtNotes из изолированного venv.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "venv не найден. Создайте его:"
    echo "  python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

exec .venv/bin/python -m qtnotes "$@"
