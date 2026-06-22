"""H6: раздельные лимиты кадров (CONTROL много меньше BLOB), применяются ДО аллокации тела.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_wire_caps.py
"""

import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _hdr(length, type_byte):
    return struct.pack(">I", length) + bytes([type_byte])


def main():
    from qtnotes.sync import wire

    async def scenario():
        # 1) нормальный round-trip CONTROL
        r = asyncio.StreamReader()
        body = b'{"type":"hello"}'
        r.feed_data(_hdr(len(body) + 1, wire.CONTROL) + body)
        kind, msg = await wire.read_frame(r)
        assert kind == "control" and msg["type"] == "hello"
        print("OK: round-trip control-кадра")

        # 2) CONTROL сверх лимита → ProtocolError (передаём ТОЛЬКО заголовок: тело не
        #    аллоцируется, значит лимит сработал до чтения тела)
        r2 = asyncio.StreamReader()
        r2.feed_data(_hdr(wire.MAX_CONTROL_FRAME + 1, wire.CONTROL))
        r2.feed_eof()
        try:
            await wire.read_frame(r2)
            raise AssertionError("ожидали ProtocolError на превышении control-лимита")
        except wire.ProtocolError:
            print("OK: CONTROL сверх лимита отвергнут до аллокации тела")

        # 3) BLOB той же длины — В ПРЕДЕЛАХ blob-лимита → лимит НЕ срабатывает; падает уже
        #    на нехватке тела (IncompleteReadError), а не на размере
        r3 = asyncio.StreamReader()
        r3.feed_data(_hdr(wire.MAX_CONTROL_FRAME + 1, wire.BLOB))
        r3.feed_eof()
        try:
            await wire.read_frame(r3)
            raise AssertionError("не должно дойти")
        except wire.ProtocolError:
            raise AssertionError("BLOB в пределах blob-лимита не должен отвергаться по размеру")
        except asyncio.IncompleteReadError:
            print("OK: BLOB в пределах blob-лимита проходит проверку размера")

        # 4) BLOB сверх blob-лимита → ProtocolError
        r4 = asyncio.StreamReader()
        r4.feed_data(_hdr(wire.MAX_BLOB_FRAME + 1, wire.BLOB))
        r4.feed_eof()
        try:
            await wire.read_frame(r4)
            raise AssertionError("ожидали ProtocolError на превышении blob-лимита")
        except wire.ProtocolError:
            print("OK: BLOB сверх blob-лимита отвергнут")

    asyncio.run(scenario())
    print("ALL WIRE-CAP TESTS PASSED")


if __name__ == "__main__":
    main()
