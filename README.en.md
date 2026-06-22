# QtNotes Desktop

[Русская версия](README.md)

A Telegram-style desktop notes app built with **PySide6 / Qt 6** (Python 3.11).
Folders work like chats, notes like messages: a narrow folder column on the left,
a wide message feed on the right with an input box at the bottom.

It is fully **offline-first** — no servers, no telemetry. An optional peer-to-peer
sync engine lets your devices exchange notes directly over your local network, and an
optional local-encryption layer protects the vault at rest behind a PIN.

> Companion mobile app (Flutter/Android): **[qtnotes-mobile](../qtnotes-mobile)** —
> it talks the same sync protocol and crypto format, so phone and desktop converge.

---

## Features

- **Folders** as chats — colored icons, create / rename / delete, context menu.
- **Notes** as feed messages: plain text, rich text (bold / italic / underline /
  strikethrough), images, arbitrary files, file albums. Full-width bubbles.
- **Rich input**: typing, paste-with-formatting, **drag-and-drop files** into the
  window, **paste images** from the clipboard. Auto-growing input, auto-focus.
- **Clickable links** (URLs) and **note-to-note links** ("copy ID link" → jump).
- **Calendar** with colored events; markers on dates.
- **Fuzzy search** — SQLite FTS5 for fast candidates + `rapidfuzz` typo-tolerant
  ranking; scope by folder or globally; search by date.
- **Export / import** — a vault is just a folder of files; export is a zip, import
  merges by id and rebuilds the search index.
- **Dark theme** in the spirit of Telegram Desktop.

### Optional: peer-to-peer sync

- Direct device-to-device over **mutual-TLS** with certificate pinning.
- **mDNS** discovery on the local network + **QR pairing** (one-shot, TTL-limited).
- Conflict resolution via an append-only **operation log** with **version vectors**
  and last-writer-wins by modification time; deletions are tombstoned.
- A cross-language **conformance test suite** pins the wire/crypto/merge semantics so
  the desktop and the Flutter app cannot silently diverge.

### Optional: local encryption & duress

- Whole-vault encryption with **AES-256-GCM** (per-file subkeys via HKDF) behind a PIN.
- Hardware-backed gate on supported machines (**TPM 2.0** with an anti-rollback NV
  counter) protecting against offline PIN brute-force.
- A **duress** mode: a reverse PIN crypto-erases the keyring, wipes an explicit
  owned-paths allowlist, and presents decoy notes.

---

## Mental model

```
Folder  ==  chat
Note    ==  message
```

The right pane is a `QStackedWidget`: a message feed (`QScrollArea` of bubble widgets
with a bottom input bar) and a calendar page. Notes are stored as plain files on disk
plus a rebuildable SQLite/FTS5 index — so moving the vault is just moving a folder.

## Storage layout

```
<vault>/
  folders/<folder-id>/
    folder.json
    notes/<note-id>.json
    notes/attachments/<note-id>/
  calendar/events.json
  index.sqlite          # rebuildable search index (not part of export)
```

---

## Requirements

- Linux with a Qt-capable desktop session (developed on Debian 11).
- **Python 3.11**.
- Dependencies (see `requirements.txt`): `PySide6`, `rapidfuzz`, `python-dateutil`.
  Everything else is the standard library.

Optional hardware gate: `tpm2-tools` and a TPM 2.0 device (the app degrades to a
software PIN gate if unavailable).

## Install & run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh           # or: python -m qtnotes
```

The vault location defaults to `~/.local/share/QtNotes` and can be overridden with the
`QTNOTES_VAULT` environment variable. App settings live in
`$XDG_CONFIG_HOME/QtNotes/settings.json`.

## Tests

The logic layer (storage, indexing, sync, crypto) is covered by headless tests that
run without a display:

```bash
QT_QPA_PLATFORM=offscreen python tests/test_storage.py
# ...or run the whole suite:
for t in tests/test_*.py; do QT_QPA_PLATFORM=offscreen python "$t"; done
```

The cross-language conformance tests (`test_convergence_conformance.py`,
`test_golden_vectors.py`) additionally drive the Flutter app if `dart` is on `PATH`
and the `qtnotes-mobile` project is present next to this one; otherwise they verify the
Python side and skip the Dart side.

## Project structure

```
qtnotes/
  app.py, __main__.py, config.py, fsutil.py
  storage/    models, vault, index, exporter, crypto_fs, owned_paths
  sync/       engine, oplog, apply, wire, transport, discovery, pairing
  crypto/     unlock, keyvault, valuecrypt, duress, primitives
  ui/         main_window, chat_view, sidebar, message_bubble, calendar_view, ...
tests/        headless logic + conformance tests
```

---

## Security note

This is a personal project, not a professionally audited security product. The
encryption and duress features are **defense-in-depth**, designed mainly against
device theft and coercion when the device is locked. No software scheme protects data
once an attacker has root access to a device on which the app is currently unlocked
(the key is then in memory). Do not rely on this app as the sole protection for
life-critical secrets without your own review.

If you find a security issue, please open an issue (or report privately if you prefer).

## License

[GNU GPL v3.0](LICENSE) — you may use, study, modify and redistribute it, but
derivative works must also be released under the GPL.
