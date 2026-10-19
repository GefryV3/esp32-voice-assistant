"""Запуск: python -m jarvis"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from pathlib import Path

from collections.abc import Callable

from .audio import Audio
from .brain import Brain
from .agents import Agents
from .ha import HomeAssistant
from .bridge import Bridge, SettingsStore
from .ota import Ota
from .voice import Voice
from .wake import WakeWord, ensure_models
from .config import ConfigError, load_settings
from .console import RecentLogHandler, add_admin_route, run_console
from .devices import DeviceHub


async def wake_factory(model_dir: Path) -> Callable[[], WakeWord] | None:
    """Модели слова пробуждения (скачать при первом запуске). Не вышло — без слова, но ask работает."""
    try:
        await asyncio.to_thread(ensure_models, model_dir)
        return lambda: WakeWord(model_dir)
    except Exception as e:
        logging.getLogger("jarvis").warning("слово пробуждения выключено: %s", e)
        return None


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
    recent = RecentLogHandler()
    recent.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(recent)
    hub = DeviceHub(settings)
    add_admin_route(hub, settings.device_token)
    bridge = Bridge(hub, settings, SettingsStore(Path(settings.data_dir) / "settings.json"))
    audio = Audio(hub, settings.data_dir)
    Ota(hub, settings, os.environ.get("JARVIS_FIRMWARE", ""))
    ha = HomeAssistant(settings.ha_url, settings.ha_token) if settings.ha_url and settings.ha_token else None
    if ha is None:
        logging.getLogger("jarvis").info("Home Assistant не настроен — умного дома нет")
    brain = Brain(Path(settings.data_dir) / "brain.db", ha=ha)
    if settings.gemini_api_key:
        voice = Voice(hub, audio, settings, brain=brain,
                      wake_factory=await wake_factory(Path(settings.data_dir) / "models"))
        brain.agents = Agents(settings.gemini_api_key, settings.agent_model, voice.announce)
        brain.agents.start_delivery()
        brain.start(voice.remind)
    else:
        logging.getLogger("jarvis").warning("нет GEMINI_API_KEY — колонка без голоса")
    async with hub:
        bridge.start()
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                await run_console(hub)
            else:
                await asyncio.Event().wait()   # аддон: работаем, пока не остановят
        finally:
            if brain.agents:
                await brain.agents.stop()
            await brain.stop()
            if ha:
                await ha.close()
            await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
