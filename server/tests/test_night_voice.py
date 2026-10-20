"""Ночной голос, характер по времени суток, эмоциональный откат, тишина «не слушай», откат при 503."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from jarvis import voice as voice_mod
from jarvis.agents import Agents
from jarvis.audio import Audio
from jarvis.bridge import Bridge, SettingsStore
from jarvis.brain import Brain
from jarvis.character import effective_sarcasm
from jarvis.config import Settings, load_settings
from jarvis.devices import DeviceHub
from jarvis.gemini_fallback import is_overload
from jarvis.gemini_live import Event, LiveError, LiveSession
from jarvis.quiet import night_volume
from jarvis.voice import Voice, system_prompt
from test_voice import TOKEN, FakeLive, FakeWake, Log, is_voice, speaker


@pytest.fixture
async def hub():
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    await h.start()
    yield h
    await h.stop()


def make_bridge(hub, tmp_path, hour, volume=60):
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"volume": volume})
    return Bridge(hub, Settings(device_token=TOKEN), store, local_now=lambda: datetime(2026, 10, 20, hour, 10))


def volumes(log):
    return [m["level"] for m in log.msgs if isinstance(m, dict) and m["type"] == "volume"]


# ── 1. ночью тише ──

def test_night_volume_is_quieter_but_never_louder():
    assert night_volume(60) == 27 and night_volume(10) == 8 and night_volume(5) == 5 and night_volume(0) == 0


async def test_night_conversation_lowers_volume_and_restores_user_volume(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.3)
    make_bridge(hub, tmp_path, hour=2, volume=60)
    live = FakeLive()
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"), live_factory=live)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("turn_complete"))
        await live.queue.put(None)
        await v.start(hub.get(), "который час")
        await log.wait(is_voice("idle"))
        await asyncio.sleep(0.05)
        assert volumes(log) == [60, 27, 60]                 # 60 — при подключении, ночью 27, после разговора снова 60
        assert hub.bridge.store.values["volume"] == 60  # настройку не трогали
        assert "Сейчас ночь" in live.system
    finally:
        await ws.close()


async def test_day_conversation_keeps_volume(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.3)
    make_bridge(hub, tmp_path, hour=15)
    live = FakeLive()
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"), live_factory=live)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await live.queue.put(None)
        await v.start(hub.get(), "привет")
        await log.wait(is_voice("idle"))
        assert volumes(log) == [60] and "Сейчас ночь" not in live.system
    finally:
        await ws.close()


async def test_night_reminder_is_asked_quietly(hub, tmp_path):
    make_bridge(hub, tmp_path, hour=1)
    live = FakeLive()
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"), live_factory=live)
    ws = await speaker(hub)
    Log(ws)
    try:
        await live.queue.put(None)
        assert await v.remind("выпить воды")
        async with asyncio.timeout(2):
            while not live.texts:
                await asyncio.sleep(0.01)
        assert "вполголоса" in live.texts[0]
    finally:
        await ws.close()


# ── 3. характер по времени суток ──

def test_character_shifts_with_time_of_day():
    morning, evening, late = datetime(2026, 10, 20, 8), datetime(2026, 10, 20, 20), datetime(2026, 10, 20, 23, 30)
    assert effective_sarcasm(morning, 60) < 60 < effective_sarcasm(evening, 60)
    assert effective_sarcasm(late, 60) < 45                     # после 23:00 без подколов
    assert effective_sarcasm(evening, 20) == 20                 # ползунок «мягко» не трогаем
    assert "утро" in system_prompt(morning, 60) and "вечер" in system_prompt(evening, 60)
    assert "поздно" in system_prompt(late, 60)
    assert "едко" not in system_prompt(late, 60) and "едко" in system_prompt(evening, 60)
    assert "Сейчас ночь" in system_prompt(late, 60, night=True)


# ── 2. эмоциональный режим ──

def test_affective_is_off_by_default(tmp_path):
    # Gemini 3.8 Live убрал эмоциональный диалог (соединение закрывается с кодом 1007), поэтому по умолчанию выкл
    assert load_settings(tmp_path / "none", {"JARVIS_DEVICE_TOKEN": "t"}).affective is False
    assert load_settings(tmp_path / "none", {"JARVIS_DEVICE_TOKEN": "t", "JARVIS_AFFECTIVE": "1"}).affective is True


class RecordingLive(FakeLive):
    """Подмена: при affective=True не подключается; запоминает, с чем её открывали."""

    def __init__(self, voice_ref, fail_when=lambda aff, model: False, error="Gemini не принял настройки"):
        super().__init__()
        self.voice_ref, self.fail_when, self.error, self.opened = voice_ref, fail_when, error, []

    async def __aenter__(self):
        v = self.voice_ref[0]
        self.opened.append((v._use_affective, v._use_model))
        if self.fail_when(v._use_affective, v._use_model):
            raise LiveError(self.error)
        return self


async def test_affective_failure_falls_back_for_ten_minutes(hub, tmp_path, caplog):
    ref = []
    live = RecordingLive(ref, lambda aff, model: aff)
    now = [100.0]
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k", affective=True),
              live_factory=live, clock=lambda: now[0])
    ref.append(v)
    with caplog.at_level(logging.WARNING, logger="jarvis.voice"):
        got = await v.take_live()
    assert got is live and live.opened == [(True, "gemini-3.8-live"), (False, "gemini-3.8-live")]
    assert any("эмоциональный режим не подключился" in r.message for r in caplog.records)
    live.opened.clear()
    await v.take_live()                                       # 10 минут сразу обычный режим
    assert live.opened == [(False, "gemini-3.8-live")]
    now[0] += 601
    live.opened.clear()
    await v.take_live()
    assert live.opened[0][0] is True                          # время вышло — снова пробуем affective


async def test_affective_pref_off_is_respected(hub, tmp_path):
    ref = []
    live = RecordingLive(ref)
    brain = Brain(tmp_path / "b.db")
    brain.set_pref("affective", "0")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=brain, live_factory=live)
    ref.append(v)
    await v.take_live()
    assert live.opened == [(False, "gemini-3.8-live")]


class RejectAffectiveLive(RecordingLive):
    """Как Gemini 3.8 Live: с эмоциональным режимом настройки принимает, а на первом ответе закрывает 1007."""

    async def __aenter__(self):
        await super().__aenter__()
        self.affective = self.opened[-1][0]
        return self

    async def events(self):
        if self.affective:
            raise LiveError("Gemini закрыл соединение: 1007 Request contains an invalid argument.")
        async for ev in super().events():
            yield ev


async def test_affective_rejected_mid_session_turns_off_and_retries(hub, tmp_path):
    ref = []
    live = RejectAffectiveLive(ref)
    brain = Brain(tmp_path / "b.db")
    brain.set_pref("affective", "1")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=brain, live_factory=live)
    ref.append(v)
    ws = await speaker(hub, caps=("display", "speaker"))
    try:
        await v.command("ask привет")
        async with asyncio.timeout(3):
            while len(live.texts) < 2:
                await asyncio.sleep(0.01)
        # тот же вопрос ушёл второй раз, уже в обычную сессию, и режим выключен насовсем
        assert live.opened == [(True, "gemini-3.8-live"), (False, "gemini-3.8-live")]
        assert live.texts == ["привет", "привет"]
        assert brain.pref("affective") == "0" and v.affective is False
        await live.queue.put(None)
    finally:
        await ws.close()


# ── 5. откат при 503 ──

def test_is_overload_detects_503():
    assert is_overload(LiveError("Gemini ответил HTTP 503")) and is_overload("HTTP 503: busy")
    assert is_overload(LiveError("Gemini закрыл соединение: 1013 try again later"))
    assert not is_overload(LiveError("нет связи с Gemini"))


async def test_live_503_switches_to_fallback_model_and_tells_once(hub, tmp_path):
    ref = []
    live = RecordingLive(ref, lambda aff, model: model == "gemini-3.8-live", error="Gemini ответил HTTP 503")
    now = [100.0]
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k", affective=False,
                                                  gemini_fallback_model="fb-live"),
              live_factory=live, clock=lambda: now[0])
    ref.append(v)
    await v.take_live()
    assert live.opened == [(False, "gemini-3.8-live"), (False, "fb-live")]
    assert "перегружена" in live.system                        # голос скажет об этом сам
    live.opened.clear()
    live.system = ""
    await v.take_live()                                       # пока держится запасная — сразу на неё и без повтора фразы
    assert live.opened == [(False, "fb-live")] and "перегружена" not in live.system
    now[0] += 130                                             # через 2 минуты снова основная; сообщение — раз в 10 минут
    live.opened.clear()
    live.fail_when = lambda aff, model: model == "gemini-3.8-live"
    await v.take_live()
    assert live.opened == [(False, "gemini-3.8-live"), (False, "fb-live")]
    assert "перегружена" not in live.system


async def test_live_503_without_fallback_shows_overload_caption(hub, tmp_path):
    ref = []
    live = RecordingLive(ref, lambda aff, model: True, error="Gemini ответил HTTP 503")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k", affective=False,
                                                  gemini_fallback_model=""), live_factory=live)
    ref.append(v)
    ws = await speaker(hub, caps=("display", "speaker"))
    log = Log(ws)
    try:
        await v.command("ask привет")
        await log.wait(lambda m: m["type"] == "caption" and "перегружен" in m["text"])
    finally:
        await ws.close()


async def test_handshake_503_becomes_live_error():
    from websockets.asyncio.server import ServerConnection  # noqa: F401

    async def refuse(connection, request):
        return connection.respond(503, "busy")

    async with serve(lambda ws: None, "127.0.0.1", 0, process_request=refuse) as srv:
        port = srv.sockets[0].getsockname()[1]
        with pytest.raises(LiveError, match="503"):
            async with LiveSession("K", "m", "v", "s", url=f"ws://127.0.0.1:{port}"):
                pass


async def test_agents_retry_on_fallback_model_when_overloaded():
    said = []

    async def announce(prompt):
        said.append(prompt)
        return True

    ag = Agents("k", "m", announce, fallback_model="fb")
    calls = []

    async def fallback(goal):
        calls.append(goal)
        return "готово на запасной"

    async def primary(goal):
        raise RuntimeError("HTTP Error 503: Service Unavailable")

    ag.generate, ag.fallback = primary, fallback
    ag.start("найди погоду", "Погода")
    await asyncio.gather(*(t.runner for t in ag.tasks))
    t = ag.tasks[0]
    assert t.status == "done" and t.result == "готово на запасной" and calls == ["найди погоду"]
    assert "перегружена" in ag.prompt_for(t)
    ag.start("ещё", "Ещё")                                    # второй раз подряд — без повторной фразы
    await asyncio.gather(*(t.runner for t in ag.tasks))
    assert "перегружена" not in ag.prompt_for(ag.tasks[1])


async def test_agents_overload_without_fallback_reports_reason():
    ag = Agents("k", "m", lambda p: asyncio.sleep(0, result=True))

    async def primary(goal):
        raise RuntimeError("HTTP Error 503")

    ag.generate = primary
    ag.start("x", "X")
    await asyncio.gather(*(t.runner for t in ag.tasks))
    assert ag.tasks[0].status == "failed" and "перегружен" in ag.prompt_for(ag.tasks[0])


# ── 4. «не слушай» ──

class FakeListener:
    """Слово берёт из кадра: b'BACK…' = «вернись», b'MAGA…' = «мага»."""

    def process(self, pcm):
        return "вернись" if pcm.startswith(b"BACK") else "мага" if pcm.startswith(b"MAGA") else ""

    def reset(self):
        pass


def quiet_msgs(log):
    return [m["on"] for m in log.msgs if isinstance(m, dict) and m["type"] == "quiet"]


async def test_mute_listening_by_voice_then_resume_by_word(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 2.0)
    live = FakeLive()
    brain = Brain(tmp_path / "b.db")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"), brain=brain,
              wake_factory=FakeWake, live_factory=live, mute_factory=FakeListener)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        dev = hub.get()
        await log.wait(lambda m: m["type"] == "listen_start")
        await ws.send(b"WAKE" + b"\x00" * 3196)
        await log.wait(is_voice("listening"))
        await live.queue.put(Event("tool_call", calls=(("c1", "mute_listening", {"minutes": 60}),)))
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("turn_complete"))
        await log.wait(lambda m: m["type"] == "quiet")
        assert quiet_msgs(log) == [True]
        assert live.tool_results[0][0][2]["ok"] is True
        await log.wait(is_voice("idle"))                       # после подтверждения — без продолжения
        assert v.quiet.active(dev.id) and v.quiet.left(dev.id) > 3500
        sent = len(live.audio)
        # в тишине слово пробуждения и речь в Gemini не идут
        await ws.send(b"WAKE" + b"\x00" * 3196)
        await ws.send(b"\x05\x00" * 1600)
        await asyncio.sleep(0.15)
        assert dev.id not in v._talks and len(live.audio) == sent
        # «мага» — короткий ответ по тексту, микрофон закрыт
        await ws.send(b"MAGA" + b"\x00" * 3196)
        async with asyncio.timeout(2):
            while not live.texts:
                await asyncio.sleep(0.01)
        assert "Я молчу" in live.texts[-1]
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("turn_complete"))
        await asyncio.sleep(0.2)
        assert dev.id not in v._talks and len(live.audio) == sent        # one_shot: продолжения нет
        # «вернись» снимает тишину
        await ws.send(b"BACK" + b"\x00" * 3196)
        await log.wait(lambda m: m["type"] == "quiet" and m["on"] is False)
        assert not v.quiet.active(dev.id)
    finally:
        await ws.close()


async def test_mute_expires_by_timer(hub, tmp_path):
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              wake_factory=FakeWake, live_factory=FakeLive())
    ws = await speaker(hub)
    log = Log(ws)
    try:
        dev = hub.get()
        await v.quiet.start(dev, 0.1)
        await log.wait(lambda m: m["type"] == "quiet" and m["on"] is True)
        assert v.quiet.active(dev.id)
        await log.wait(lambda m: m["type"] == "quiet" and m["on"] is False)
        assert not v.quiet.active(dev.id)
    finally:
        await ws.close()


async def test_mute_console_and_tool_limits(hub, tmp_path):
    brain = Brain(tmp_path / "b.db")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"), brain=brain,
              live_factory=FakeLive())
    assert (await brain.call("mute_listening", {"minutes": 5}, None))["error"].startswith("колонки нет")
    ws = await speaker(hub)
    Log(ws)
    try:
        out = await brain.call("mute_listening", {"minutes": 99999}, None)
        assert out["ok"] and out["minutes"] == 480                       # потолок
        assert "тишина ещё" in await v.command("mute")
        assert await v.command("mute off") == "тишина снята"
        assert any(t["name"] == "mute_listening" for t in brain.tools())
    finally:
        await ws.close()


async def test_affective_invalid_argument_turns_it_off_for_good(hub, tmp_path):
    ref = []
    live = RecordingLive(ref, lambda aff, model: aff, error="Gemini закрыл соединение: 1007 Request contains an invalid argument.")
    brain = Brain(tmp_path / "b.db")
    now = [100.0]
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k", affective=True),
              brain=brain, live_factory=live, clock=lambda: now[0])
    ref.append(v)
    await v.take_live()
    assert v.affective is False and brain.pref("affective") == "0"
    now[0] += 5000                                           # и через часы больше не пробуем
    live.opened.clear()
    await v.take_live()
    assert live.opened == [(False, "gemini-3.8-live")]
