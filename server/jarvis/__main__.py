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
from .ha import HomeAssistant
from .bridge import Bridge, SettingsStore
from .ota import Ota
from .telegram import Telegram, parse_chat_ids
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


async def _device_send(hub: DeviceHub, msg) -> None:
    """Экранные команды функций (таймер, число напоминаний) — первой колонке, если она есть."""
    dev = hub.get()
    if dev is not None:
        await dev.send(msg)


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
    else:
        voice = None
        logging.getLogger("jarvis").warning("нет GEMINI_API_KEY — колонка без голоса")
    chat_ids = parse_chat_ids(settings.telegram_chat_ids)
    tg = None
    if settings.telegram_token and chat_ids and settings.gemini_api_key:
        tg = hub.telegram = Telegram(
            settings.telegram_token, chat_ids, api_key=settings.gemini_api_key,
            model=settings.telegram_model, brain=brain, sarcasm=voice.sarcasm,
            device_send=lambda msg: _device_send(hub, msg))
    if voice or tg:
        async def remind(text: str) -> bool:
            # колонка не ответила (нет её или занята) — напоминание уходит в Telegram
            if voice and await voice.remind(text):
                return True
            return bool(tg and await tg.send(f"⏰ {text}"))
        brain.start(remind)
    async with hub:
        bridge.start()
        if tg:
            tg.start()
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                await run_console(hub)
            else:
                await asyncio.Event().wait()   # аддон: работаем, пока не остановят
        finally:
            if tg:
                await tg.stop()
            await brain.stop()
            if ha:
                await ha.close()
            await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
