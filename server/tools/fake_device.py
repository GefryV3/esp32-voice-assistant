"""Фейковая колонка: подключается к серверу как настоящая и печатает, что ей прислали.

    python tools/fake_device.py [--url ws://127.0.0.1:8765/device] [--token ...]

Токен по умолчанию берётся из server/.env.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.config import read_env_file  # noqa: E402

STARTED = time.monotonic()


def status() -> dict:
    return {"type": "status", "uptime": int(time.monotonic() - STARTED),
            "heap": 180_000, "psram": 1_900_000, "rssi": -55, "fps": 30}


async def run(url: str, token: str, device: str) -> None:
    async with connect(url) as ws:
        await ws.send(json.dumps({"type": "hello", "proto": 1, "token": token,
                                  "device": device, "fw": "fake-0.1", "caps": ["display"]}))
        print(f"подключилась к {url} как {device}")

        async def report() -> None:
            while True:
                await ws.send(json.dumps(status()))
                await asyncio.sleep(30)

        reporter = asyncio.create_task(report())
        try:
            async for raw in ws:
                msg = json.loads(raw)
                print("←", msg)
                if msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong", "id": msg.get("id")}))
        except ConnectionClosed as e:
            print(f"сервер закрыл соединение: {e.rcvd.code if e.rcvd else '?'} {e.rcvd.reason if e.rcvd else ''}")
        finally:
            reporter.cancel()


def main() -> None:
    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=f"ws://127.0.0.1:{env.get('JARVIS_PORT', '8765')}/device")
    p.add_argument("--token", default=env.get("JARVIS_DEVICE_TOKEN", ""))
    p.add_argument("--device", default="fake-hub")
    args = p.parse_args()
    try:
        asyncio.run(run(args.url, args.token, args.device))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
