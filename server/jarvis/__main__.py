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
from .habits import Habits, parse_keywords, parse_windows
from .health import GEMINI_ERRORS, Health
from .screenshot import Screenshot
from .weatherfeed import WeatherFeed
from .digest import Digest
from .ha import HomeAssistant
from .lights import Lights
from .lightstate import LightState
from .bridge import Bridge, SettingsStore
from .claude_status import ClaudeStatus
from .ota import Ota
from .presence import Presence
from .morning import Morning, MorningConfig
from .hunches import Hunches
from .learning import Learning
from .routines import Routines
from .transit import Transit, TransitConfig
from .umbrella import Umbrella, UmbrellaConfig
from .memorize import Memorize
from .school import School, SchoolConfig
from .school_inbox import SchoolInbox
from .untis import Untis, UntisConfig, FixedSchedule, WithFallback
from .telegram import Telegram, parse_chat_ids
from .breaks import Breaks
from .news import News
from .tg_voice import TgVoice
from .translate import Translator
from .fastpath import make_fast_factory
from .quiet import make_mute_factory
from .voice import Voice
from .voiceid import OnnxEmbedder, VoiceID, ensure_model as ensure_voiceid_model
from .wake import WakeWord, ensure_models
from .wake_vosk import MultiWake, StopWord, VoskWake, ensure_model
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


async def stop_factory(model_dir: Path) -> Callable[[], object] | None:
    """«Стоп» посреди ответа: тот же Vosk, что и для слова пробуждения, со своим списком слов."""
    try:
        path = await asyncio.to_thread(ensure_model, model_dir)
    except Exception as e:
        logging.getLogger("jarvis").warning("«стоп» голосом выключен: %s", e)
        return None
    return lambda: StopWord(path)


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
        tasks_days = int(read_env_file(Path(".env")).get("JARVIS_TASKS_MAX_AGE_DAYS", os.environ.get("JARVIS_TASKS_MAX_AGE_DAYS", "30")) or 30)
        google = Google(settings.google_client_id, settings.google_client_secret,
                        settings.google_refresh_token, settings.google_tz, tasks_max_age_days=tasks_days)
        logging.getLogger("jarvis").info("Google: календарь, почта, задачи подключены")
    brain = Brain(Path(settings.data_dir) / "brain.db", ha=ha, google=google)
    if settings.gemini_api_key:
        voice = Voice(hub, audio, settings, brain=brain,
                      mute_factory=await make_mute_factory(Path(settings.data_dir) / "models"),
                      fast_factory=await asyncio.to_thread(make_fast_factory, Path(settings.data_dir) / "models"),
                      wake_factory=await wake_factory(Path(settings.data_dir) / "models", settings.wake_words),
                      stop_factory=await stop_factory(Path(settings.data_dir) / "models"))
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
    env = {**read_env_file(Path(".env")), **os.environ}
    news = News.from_env(brain, lambda: brain.agents.generate if brain.agents else None, env) if voice else None
    if voice and settings.routines:
        mcfg = MorningConfig.from_env(env)
        morning = hub.morning = brain.morning = Morning(
            mcfg, voice.announce, untis=WithFallback(Untis(UntisConfig.from_env(env)), FixedSchedule(mcfg.school_start)), transit=Transit(TransitConfig.from_env(env)),
            bridge=bridge, brain=brain, ha=ha)
        umbrella = Umbrella(UmbrellaConfig.from_env(env), voice.announce, night=lambda: routines.night())
        routines = hub.routines = Routines(
            voice.announce, settings=lambda: bridge.store.values, brain=brain, ha=ha, briefing_at=settings.briefing_at,
            soft_wake_s=mcfg.soft_wake_s if mcfg.soft_wake else None, wake_context=morning.wake_context,
            on_leave=umbrella.on_leave, briefing_extra=news.briefing_extra,
            phone_home=lambda: presence.phone_on_lan())     # presence создаётся ниже
        morning.routines = routines
        bridge.on_wake = routines.wake_signal
        routines.start()
        morning.start()
    if ha:
        WeatherFeed(hub, ha).start()      # значок погоды на экране сам, а не только по вопросу
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
                        idle_min=settings.presence_idle_min, room_away_min=settings.room_away_min,
                        ble_uuid=settings.ble_uuid)
    hub.on_message(presence.on_room)
    hub.on_connect(presence.on_connect)
    presence.hub = hub
    brain.presence = presence if presence.enabled else None
    # навыки: уведомления и команды в Telegram, задания навыкам уходят на ноутбук через мост ПК
    async def pc_send(msg) -> bool:
        return bridge.pc is not None and await bridge.pc.send(msg)

    requests = hub.skills = brain.skill_requests
    requests.pc_send = pc_send
    bridge.on_pc_event = requests.handle_pc
    bridge.on_pc_connect = requests.dispatch_waiting
    hub.claude = ClaudeStatus(lambda msg: _device_send(hub, msg), voice.announce if voice else None,
                              brain.pref, brain.set_pref)
    if tg:
        brain.on_notice = requests.notify = tg.send
        tg.skills = requests
    hub.presence = presence
    Offline(hub, brain, clap_on=settings.clap, clap_sens=settings.clap_sens)   # хлопки и план без связи
    if voice:
        voice.presence = presence
    if tg:
        tg.presence = presence
    brain.extensions = []
    extras = hub.extras = []
    extras.append(Screenshot(hub))
    GEMINI_ERRORS.bind(brain)
    health = Health(hub=hub, bridge=bridge, ha=ha, google=google)
    brain.extensions.append(health)
    extras.append(health)
    school_env = {**read_env_file(Path(".env")), **os.environ}
    inbox = SchoolInbox(brain.db)
    bridge.on_notif = inbox.record
    school = School(Untis(UntisConfig.from_env(school_env)), inbox, SchoolConfig.from_env(school_env),
                    announce=voice.announce if voice else None, brain=brain,
                    can_speak=lambda: not (voice.is_night() or voice.quiet.states) if voice else False)
    memo = Memorize(brain.db)
    brain.extensions += [school, memo]
    extras += [school, memo]
    school.start()
    translator = brain.translator = Translator(brain.german)
    brain.extensions.append(translator)
    extras.append(translator)
    breaks = None
    if news:
        brain.extensions.append(news)
        extras.append(news)
    if voice:
        breaks = Breaks.from_env(voice.announce, bridge=bridge, brain=brain, env=env,
                                 muted=lambda: (d := hub.get()) is not None and voice.quiet.active(d.id))
        brain.extensions.append(breaks)
        extras.append(breaks)
        breaks.start()
    tgvoice = None
    if voice and tg:
        tgvoice = tg.tgvoice = TgVoice.from_env(voice, tg.send, env)
        extras.append(tgvoice)
    if settings.voiceid:
        voiceid = VoiceID(brain, hub=hub, voice=voice, audio=audio, presence=presence if presence.enabled else None,
                          notify=tg.send if tg else None, night=bridge.night, threshold=settings.voiceid_threshold)
        brain.extensions.append(voiceid)
        extras.append(voiceid)
        if tg:
            tg.voiceid = voiceid

        async def load_voiceid() -> None:
            # модель качается в фоне: пока её нет, колонка отзывается как раньше
            try:
                path = await asyncio.to_thread(ensure_voiceid_model, Path(settings.data_dir) / "models")
                voiceid.embedder = await asyncio.to_thread(OnnxEmbedder, path)
                logging.getLogger("jarvis").info("модель голоса готова, режим: %s", voiceid.mode)
            except Exception as e:
                logging.getLogger("jarvis").warning("голос хозяина выключен: %s", e)
        voiceid_task = asyncio.create_task(load_voiceid())     # noqa: F841 (держим ссылку до конца main)
    habits = None
    if voice and google and settings.habits:
        habits = Habits(voice.announce, google=google, brain=brain, settings=lambda: bridge.store.values,
                        prob=settings.habits_prob, windows=parse_windows(settings.habits_windows),
                        keywords=parse_keywords(settings.habits_keywords))
        brain.extensions.append(habits)
        extras.append(habits)
        habits.start()
    digest = None
    if tg and settings.digest:
        digest = Digest(tg, brain, google=google, habits=habits, backup_dir=Path(settings.data_dir) / "backup",
                        at=settings.digest_at)
        bridge.on_slept = digest.record_sleep
        extras.append(digest)
        digest.start()
    if voice and settings.gemini_api_key:
        from .agents import ask_model

        async def learn_ask(prompt: str) -> str:
            return await asyncio.to_thread(ask_model, settings.gemini_api_key, settings.agent_model, prompt)
        learning = brain.learning = hub.learning = Learning(
            brain, ask=learn_ask, notify=(tg.send if tg else None), announce=voice.announce,
            presence=presence if presence.enabled else None, night=routines.night if routines else None)
        learning.start()
    else:
        learning = None
    hunches = None
    if ha and learning:
        hunches = brain.hunches = Hunches(
            brain, learning, ha, announce=voice.announce, notify=(tg.send if tg else None),
            presence=presence if presence.enabled else None, night=routines.night if routines else None,
            muted=lambda: any(voice.quiet.active(d) for d in list(hub.devices)), bridge=bridge)
        hunches.attach_ha()
        if lights:
            hunches.attach_lights(lights)
        brain.extensions.append(hunches)
        extras.append(hunches)
        hunches.start()
    if digest and learning:
        digest.learning_digest = learning.digest      # «чему я научился» в недельной сводке
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
            for module in (habits, digest, school, breaks, tgvoice, hunches):
                if module:
                    await module.stop()
            if learning:
                await learning.stop()
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
