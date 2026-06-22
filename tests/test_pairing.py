"""S2: пейринг — одноразовый токен + TTL.

Проверяем на РЕАЛЬНОМ TLS-loopback:
  1) первая пара проходит, оба заносят друг друга;
  2) повторная пара тем же токеном ОТКЛОНЕНА (consumed → слушатель закрыт);
  3) истёкший токен (ttl=0) ОТКЛОНЁН.

Запуск: QT_QPA_PLATFORM=offscreen .venv/bin/python tests/test_pairing.py
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    from qtnotes.sync import identity, pairing

    base = tempfile.mkdtemp(prefix="qtnotes-pair-")
    cfgA = os.path.join(base, "cfgA")  # десктоп (слушает)
    cfgB = os.path.join(base, "cfgB")  # телефон (подключается)
    os.makedirs(cfgA); os.makedirs(cfgB)

    try:
        os.environ["XDG_CONFIG_HOME"] = cfgA
        idA = identity.ensure_identity()
        os.environ["XDG_CONFIG_HOME"] = cfgB
        idB = identity.ensure_identity()
        assert idA.device_id != idB.device_id

        async def scenario():
            # --- 1) первая пара успешна ---
            token = pairing.generate_token()
            paired = []
            server = await pairing.serve_pairing(
                idA, token, on_paired=paired.append, host="127.0.0.1", port=0)
            port = server.sockets[0].getsockname()[1]
            payload = json.loads(
                pairing.make_pairing_payload(idA, "127.0.0.1", port, token))
            peer = await pairing.pair_with(payload, idB)
            assert peer.device_id == idA.device_id, peer.device_id
            assert paired and paired[0].device_id == idB.device_id
            print("OK: первая пара прошла, обе стороны занесены")

            # --- 2) повторная пара тем же токеном ОТКЛОНЕНА (одноразовость) ---
            await asyncio.sleep(0.1)  # дать finally закрыть слушатель
            try:
                await pairing.pair_with(payload, idB)
                raise AssertionError("повторная пара НЕ должна проходить")
            except (pairing.PairingError, OSError):
                print("OK: повторная пара отклонена (consumed/закрыт слушатель)")

            # --- 3) истёкший токен (ttl=0) ОТКЛОНЁН ---
            token2 = pairing.generate_token()
            server2 = await pairing.serve_pairing(
                idA, token2, host="127.0.0.1", port=0, ttl=0.0)
            port2 = server2.sockets[0].getsockname()[1]
            payload2 = json.loads(
                pairing.make_pairing_payload(idA, "127.0.0.1", port2, token2))
            await asyncio.sleep(0.05)
            try:
                await pairing.pair_with(payload2, idB)
                raise AssertionError("истёкший токен НЕ должен проходить")
            except (pairing.PairingError, OSError):
                print("OK: истёкший токен отклонён")
            server2.close()

        asyncio.run(scenario())
        print("ALL PAIRING TESTS PASSED")
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
