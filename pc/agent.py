"""Компьютер ↔ колонка: сижу ли я за компьютером, заряд ноутбука, ссылки с телефона.

    python agent.py                 — работать в фоне (раз в 15 с сообщает серверу)
    python agent.py send <ссылка>   — отправить ссылку на телефон

Адрес сервера и токен берутся из server/.env (или --url, --token). Нужен только пакет
websockets — он уже стоит в server/.venv, проще всего запускать оттуда:
    ..\\server\\.venv\\Scripts\\python agent.py
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import logging
import sys
import webbrowser
from ctypes import wintypes
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

log = logging.getLogger("pc")
REPORT_S = 15


class _LastInput(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class _Power(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Сколько секунд не трогали мышь и клавиатуру."""
    li = _LastInput(cbSize=ctypes.sizeof(_LastInput))
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    return (ctypes.windll.kernel32.GetTickCount() - li.dwTime) / 1000.0


def battery() -> tuple[int, bool]:
    """Заряд ноутбука в процентах (−1 — батареи нет) и подключена ли зарядка."""
    p = _Power()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(p)) or p.BatteryLifePercent == 255:
        return -1, False
    return int(p.BatteryLifePercent), p.ACLineStatus == 1


def hello(token: str) -> str:
    return json.dumps({"type": "hello", "proto": 1, "token": token, "device": "pc", "fw": "0.1.0", "caps": ["pc"]})


async def run(url: str, token: str) -> None:
    delay = 1
    while True:
        try:
            async with connect(url) as ws:
                await ws.send(hello(token))
                if json.loads(await ws.recv()).get("type") != "welcome":
                    raise ConnectionError("сервер не принял")
                log.info("на связи с %s", url)
                delay = 1
                report = asyncio.create_task(_report(ws))
                try:
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "open_url" and msg.get("url"):
                            log.info("открываю %s", msg["url"])
                            webbrowser.open(msg["url"])
                finally:
                    report.cancel()
        except (OSError, ConnectionClosed, InvalidHandshake, ConnectionError, TimeoutError) as e:
            log.warning("нет связи (%s), повтор через %d с", e, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def _report(ws) -> None:
    while True:
        level, charging = battery()
        await ws.send(json.dumps({"type": "pc", "idle": round(idle_seconds()), "battery": level, "charging": charging}))
        await asyncio.sleep(REPORT_S)


async def send_to_phone(url: str, token: str, link: str) -> None:
    async with connect(url) as ws:
        await ws.send(hello(token))
        await ws.recv()
        await ws.send(json.dumps({"type": "to_phone", "url": link}))
    print("отправлено на телефон")


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    env = read_env(Path(__file__).resolve().parents[1] / "server" / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("action", nargs="?", default="run", choices=["run", "send"])
    p.add_argument("link", nargs="?")
    p.add_argument("--url", default=f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/pc")
    p.add_argument("--token", default=env.get("JARVIS_DEVICE_TOKEN", ""))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    try:
        if a.action == "send":
            if not a.link:
                p.error("нужна ссылка: agent.py send <ссылка>")
            asyncio.run(send_to_phone(a.url, a.token, a.link))
        else:
            asyncio.run(run(a.url, a.token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
