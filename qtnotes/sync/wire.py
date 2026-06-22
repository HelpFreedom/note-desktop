"""Кадрирование и (де)сериализация сообщений протокола синхронизации.

Работает поверх asyncio StreamReader/StreamWriter. Формат — см. docs/sync-protocol.md:
кадр = uint32 длина + uint8 тип (1=CONTROL JSON, 2=BLOB) + тело.
"""

from __future__ import annotations

import json
import struct

CONTROL = 1
BLOB = 2
# Раздельные потолки (H6): CONTROL (JSON: hello/have/ops/want_blobs) много меньше, чем
# BLOB (вложения/видео). Это не даёт пиру форсировать огромную аллокацию управляющим
# кадром. Тип читается из заголовка ДО чтения тела, поэтому лимит применяется до аллокации.
MAX_CONTROL_FRAME = 64 * 1024 * 1024    # 64 MiB на управляющий кадр (с запасом на синк)
MAX_BLOB_FRAME = 256 * 1024 * 1024      # 256 MiB на blob-кадр (крупные вложения)
MAX_FRAME = MAX_BLOB_FRAME              # обратная совместимость (абсолютный потолок)
_HASH_HEX = 64                  # sha256 в hex


class ProtocolError(Exception):
    pass


async def _write_frame(writer, type_byte: int, body: bytes) -> None:
    writer.write(struct.pack(">I", len(body) + 1) + bytes([type_byte]) + body)
    await writer.drain()


async def write_message(writer, obj: dict) -> None:
    """Отправить CONTROL-сообщение (JSON-объект с полем type)."""
    await _write_frame(writer, CONTROL, json.dumps(obj, ensure_ascii=False).encode("utf-8"))


async def write_blob(writer, sha256: str, data: bytes) -> None:
    """Отправить BLOB-кадр: 64-байтный hex sha256 + сырые байты."""
    if len(sha256) != _HASH_HEX:
        raise ProtocolError("sha256 должен быть 64 hex-символа")
    await _write_frame(writer, BLOB, sha256.encode("ascii") + data)


async def read_frame(reader):
    """Прочитать один кадр.

    Возвращает ("control", dict) или ("blob", (sha256, bytes)).
    """
    # читаем длину + тип (5 байт) — тип нужен, чтобы применить ЛИМИТ ДО аллокации тела
    head = await reader.readexactly(5)
    (length,) = struct.unpack(">I", head[:4])
    type_byte = head[4]
    if length < 1:
        raise ProtocolError(f"некорректная длина кадра: {length}")
    cap = MAX_CONTROL_FRAME if type_byte == CONTROL else MAX_BLOB_FRAME
    if length > cap:
        raise ProtocolError(
            f"кадр типа {type_byte} превышает лимит: {length} > {cap}")
    payload = await reader.readexactly(length - 1)  # тело без байта типа
    if type_byte == CONTROL:
        return "control", json.loads(payload.decode("utf-8"))
    if type_byte == BLOB:
        if len(payload) < _HASH_HEX:
            raise ProtocolError("BLOB-кадр короче заголовка")
        sha = payload[:_HASH_HEX].decode("ascii")
        return "blob", (sha, payload[_HASH_HEX:])
    raise ProtocolError(f"неизвестный тип кадра: {type_byte}")
