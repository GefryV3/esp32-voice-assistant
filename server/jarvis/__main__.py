"""Запуск: python -m jarvis"""

from __future__ import annotations

import asyncio
import logging
import sys

from pathlib import Path

from .audio import Audio
from .bridge import Bridge, SettingsStore
from .config import ConfigError, load_settings
from .console import add_admin_route, run_console
from .devices import DeviceHub


async def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as e:
        sys.exit(f"Ошибка настроек: {e}")

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)  # о подключениях пишет devices
    hub = DeviceHub(settings)
    add_admin_route(hub, settings.device_token)
    bridge = Bridge(hub, settings, SettingsStore(Path(settings.data_dir) / "settings.json"))
    Audio(hub, settings.data_dir)
    async with hub:
        bridge.start()
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                await run_console(hub)
            else:
                await asyncio.Event().wait()   # аддон: работаем, пока не остановят
        finally:
            await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
