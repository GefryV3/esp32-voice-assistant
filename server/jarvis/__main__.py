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
from .offline import Offline
from .agents import Agents, make_search
from .google import Google
from .ha import HomeAssistant
from .lights import Lights
from .lightstate import LightState
from .bridge import Bridge, SettingsStore
from .ota import Ota
from .presence import Presence
from .morning import Morning, MorningConfig
from .routines import Routines
from .transit import Transit, TransitConfig
from .umbrella import Umbrella, UmbrellaConfig
from .untis import Untis, UntisConfig
from .telegram import Telegram, parse_chat_ids
from .quiet import make_mute_factory
from .voice import Voice
from .wake import WakeWord, ensure_models
from .wake_vosk import MultiWake, VoskWake, ensure_model
from .config import ConfigError, load_settings, read_env_file
from .console import RecentLogHandler, add_admin_route, run_console
from .devices import DeviceHub


async def wake_factory(model_dir: Path, words: str = "мага,jarvis") -> Callable[[], object] | None:
    """Детекторы слов пробуждения (модели качаются при первом запуске). Каждый грузится сам:
    не вышел один (нет сети, нет vosk) — работают остальные; ни одного — без слова, но ask работает."""
    log = logging.getLogger("jarvis")
    names = [w.strip().casefold() for w in words.split(",") if w.strip()]
    makers: list[Callable[[], object]] = []
    custom = tuple(n for n in names if n != "jarvis")
    if custom:
        try:
            path = await asyncio.to_thread(ensure_model, model_dir)
            makers.append(lambda: VoskWake(path, custom))
        except Exception as e:
            log.warning("слова %s (Vosk) выключены: %s", ", ".join(custom), e)
    if "jarvis" in names:
        try:
            await asyncio.to_thread(ensure_models, model_dir)
            makers.append(lambda: WakeWord(model_dir))
        except Exception as e:
            log.warning("слово Hey Jarvis выключено: %s", e)
    if not makers:
        return None
    log.info("слова пробуждения: %s", ", ".join(names))
    return lambda: MultiWake([make() for make in makers])


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
    google = None
    if settings.google_client_id and settings.google_client_secret and settings.google_refresh_token:
        google = Google(settings.google_client_id, settings.google_client_secret,
                        settings.google_refresh_token, settings.google_tz)
        logging.getLogger("jarvis").info("Google: календарь, почта, задачи подключены")
    brain = Brain(Path(settings.data_dir) / "brain.db", ha=ha, google=google)
    if settings.gemini_api_key:
        voice = Voice(hub, audio, settings, brain=brain,
                      mute_factory=await make_mute_factory(Path(settings.data_dir) / "models"),
                      wake_factory=await wake_factory(Path(settings.data_dir) / "models", settings.wake_words))
        brain.agents = Agents(settings.gemini_api_key, settings.agent_model, voice.announce,
                              search=settings.agent_search, fallback_model=settings.fallback_text_model,
                              search_fn=make_search(settings.agent_search, settings.brave_key, settings.searxng_url,
                                                    settings.tavily_key, settings.serper_key))
        brain.agents.start_delivery()
    else:
        voice = None
        logging.getLogger("jarvis").warning("нет GEMINI_API_KEY — колонка без голоса")
    routines = None
    morning = None
    if voice and settings.routines:
        env = {**read_env_file(Path(".env")), **os.environ}
        mcfg = MorningConfig.from_env(env)
        morning = hub.morning = brain.morning = Morning(
            mcfg, voice.announce, untis=Untis(UntisConfig.from_env(env)), transit=Transit(TransitConfig.from_env(env)),
            bridge=bridge, brain=brain, ha=ha)
        umbrella = Umbrella(UmbrellaConfig.from_env(env), voice.announce, night=lambda: routines.night())
        routines = hub.routines = Routines(
            voice.announce, settings=lambda: bridge.store.values, brain=brain, ha=ha, briefing_at=settings.briefing_at,
            soft_wake_s=mcfg.soft_wake_s if mcfg.soft_wake else None, wake_context=morning.wake_context,
            on_leave=umbrella.on_leave)
        morning.routines = routines
        bridge.on_wake = routines.wake_signal
        routines.start()
        morning.start()
    lights = None
    if ha:
        lights = Lights(hub, ha, LightState(ha, path=Path(settings.data_dir) / "lightstate.json"),
                        bridge=bridge, voice=voice, brain=brain)
        brain.lights = lights
    chat_ids = parse_chat_ids(settings.telegram_chat_ids)
    tg = None
    if settings.telegram_token and chat_ids and settings.gemini_api_key:
        tg = hub.telegram = Telegram(
            settings.telegram_token, chat_ids, api_key=settings.gemini_api_key,
            model=settings.telegram_model, brain=brain, sarcasm=voice.sarcasm,
            device_send=lambda msg: _device_send(hub, msg), fallback_model=settings.fallback_text_model)
    presence = Presence(settings.presence, bridge=bridge, ha=ha, brain=brain,
                        pc_min=settings.presence_pc_min, phone_min=settings.presence_phone_min,
                        idle_min=settings.presence_idle_min)
    brain.presence = presence if presence.enabled else None
    hub.presence = presence
    Offline(hub, brain, clap_on=settings.clap, clap_sens=settings.clap_sens)   # хлопки и план без связи
    if voice:
        voice.presence = presence
    if tg:
        tg.presence = presence
    if voice or tg:
        async def remind(text: str) -> bool:
            # колонка не ответила (нет её или занята) — напоминание уходит в Telegram
            if voice and await voice.remind(text):
                return True
            return bool(tg and await tg.send(f"⏰ {text}"))
        brain.start(remind)
    async with hub:
        bridge.start()
        presence.start()
        if lights:
            lights.state.start()
        if tg:
            tg.start()
        try:
            if sys.stdin is not None and sys.stdin.isatty():
                await run_console(hub)
            else:
                await asyncio.Event().wait()   # аддон: работаем, пока не остановят
        finally:
            if lights:
                await lights.state.stop()
            if routines:
                await routines.stop()
            if morning:
                await morning.stop()
            if tg:
                await tg.stop()
            if brain.agents:
                await brain.agents.stop()
            await brain.stop()
            if ha:
                await ha.close()
            await presence.stop()
            await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
