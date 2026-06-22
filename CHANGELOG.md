# Changelog

All notable changes to this project are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [1.0.0] — Initial public release

First open-source release of the QtNotes desktop app.

### Added
- Telegram-style UI: folders-as-chats, notes-as-messages, message feed with a bottom
  input bar, calendar page.
- Rich text notes, image/file/album attachments, drag-and-drop, paste-with-formatting,
  paste images from the clipboard.
- Clickable URLs and note-to-note links.
- Calendar with colored events.
- Fuzzy search (SQLite FTS5 + rapidfuzz), folder/global scope, search by date.
- Zip export/import with index rebuild.
- Dark theme.
- Optional peer-to-peer sync: mutual-TLS, mDNS discovery, QR pairing, operation log with
  version vectors, last-writer-wins, tombstones. Cross-language conformance test suite.
- Optional local encryption: AES-256-GCM whole-vault, TPM 2.0 hardware gate with
  anti-rollback NV counter, PIN unlock.
- Duress mode: reverse-PIN crypto-erase + owned-paths wipe + decoy notes.
- Headless test suite (`QT_QPA_PLATFORM=offscreen`) covering storage, indexing, sync,
  crypto, and Python↔Dart conformance.

[1.0.0]: https://example.com/releases/v1.0.0
