"""Отправить команды колонке через работающий сервер (служебный вход /admin).

    python tools/send.py dice 5
    python tools/send.py "emotion happy; weather 3 -2; devices"
    python tools/send.py rec 5        # запись с микрофона → server/recordings/*.wav

Команды — те же, что в консоли сервера (help — список). Несколько команд — через «;»,
между ними пауза --gap секунд. Токен, адрес (JARVIS_SERVER) и порт берутся из server/.env.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.config import read_env_file  # noqa: E402


RECORDINGS = Path(__file__).resolve().parents[1] / "recordings"


async def run(url: str, token: str, commands: list[str], gap: float) -> None:
    async with connect(url, max_size=None) as ws:
        await ws.send(token)
        for i, cmd in enumerate(commands):
            if i:
                await asyncio.sleep(gap)
            await ws.send(cmd)
            reply = await ws.recv()
            wav = None
            if isinstance(reply, bytes):   # rec/echo: сначала WAV, потом текст
                wav, reply = reply, await ws.recv()
            print(reply)
            if wav is not None:
                m = re.search(r"запись (\S+\.(?:wav|png))", reply)
                path = RECORDINGS / (m.group(1) if m else "запись.wav")
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(wav)
                print(f"сохранено: {path}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="+")
    p.add_argument("--url", default=f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin")
    p.add_argument("--token", default=env.get("JARVIS_DEVICE_TOKEN", ""))
    p.add_argument("--gap", type=float, default=0.0, help="пауза между командами, с")
    args = p.parse_args()
    commands = [c.strip() for c in " ".join(args.command).split(";") if c.strip()]
    asyncio.run(run(args.url, args.token, commands, args.gap))


if __name__ == "__main__":
    main()
