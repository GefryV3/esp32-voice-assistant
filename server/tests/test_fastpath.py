"""Быстрый путь (#38) и шёпот (#42): разбор фраз, поддельный Vosk, разговор без Gemini, признак шёпота."""

import asyncio
import json
import math
import random
from array import array
from datetime import datetime

import pytest

from jarvis import voice as voice_mod
from jarvis.audio import Audio
from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.devices import DeviceHub
from jarvis.fastpath import FastListener, parse, parse_number, vocabulary
from jarvis.gemini_live import Event
from jarvis.voice import Voice
from jarvis.whisper import WhisperConfig, WhisperDetector, frame_stats, whisper_volume
from test_lights import env as lights_env  # noqa: F401  (фикстура с настоящими Lights)
from test_voice import TOKEN, FakeLive, Log, is_voice, speaker


# ── разбор фраз ──

@pytest.mark.parametrize("text,kind,arg", [
    ("включи свет", "light_on", None), ("мага включи свет", "light_on", None),
    ("[unk] выключи свет", "light_off", None), ("Выключи свет пожалуйста", "light_off", None),
    ("всё выключи", "all_off", None), ("потемнее", "dim", None), ("сделай поярче", "bright", None),
    ("таймер на пять минут", "timer", 300), ("поставь таймер на пятнадцать минут", "timer", 900),
    ("таймер на полчаса", "timer", 1800), ("таймер на час", "timer", 3600),
    ("таймер на двадцать пять минут", "timer", 1500), ("таймер на тридцать секунд", "timer", 30),
    ("таймер на две минуты", "timer", 120), ("таймер на сто двадцать минут", "timer", 7200),
    ("отмени таймер", "timer_cancel", None), ("стоп", "stop", None), ("замолчи", "stop", None),
    ("который час", "time", None), ("сколько до будильника", "alarm", None),
    ("отвечай на команды вслух", "voice_on", None), ("отвечай на команды молча", "voice_off", None),
])
def test_parse_known_commands(text, kind, arg):
    cmd = parse(text)
    assert cmd is not None and cmd.kind == kind and cmd.arg == arg


@pytest.mark.parametrize("text", [
    "", "[unk]", "включи свет [unk]", "включи [unk] свет", "включи свет в коридоре",
    "таймер на", "таймер на пять", "таймер на минут", "таймер на пять пять минут", "таймер на ноль минут",
    "таймер на тысячу минут", "стоп включи свет", "какая погода", "выключи свет и включи музыку",
    "таймер на сто двадцать пять тридцать минут", "включи", "мага",
])
def test_parse_rejects_anything_unclear(text):
    assert parse(text) is None


def test_numbers_words():
    assert [parse_number(t.split()) for t in ("пять", "одиннадцать", "двадцать один", "сто", "сто пять",
                                                "девяносто девять", "двадцать двадцать", "пять двадцать")] \
        == [5, 11, 21, 100, 105, 99, None, None]


def test_vocabulary_covers_every_command_word():
    vocab = set(vocabulary())
    assert "[unk]" in vocab and {"таймер", "пять", "полчаса", "свет", "стоп", "будильника"} <= vocab
    assert all(w in vocab for w in "таймер на пятнадцать минут".split())


# ── поддельный распознаватель ──

class FakeRec:
    """Вместо Kaldi: каждая «фраза» кончается на кадре, где в очереди лежит её текст."""

    def __init__(self, finals, conf=1.0):
        self.finals, self.conf, self.last = list(finals), conf, ""

    def AcceptWaveform(self, pcm):
        self.last = self.finals.pop(0) if self.finals else ""
        return bool(self.last)

    def Result(self):
        words = [{"word": w, "conf": self.conf} for w in self.last.split()]
        return json.dumps({"text": self.last, "result": words})


def test_listener_fires_only_on_finished_confident_command():
    ls = FastListener(lambda: FakeRec(["", "включи свет", "что-то длинное"]), min_conf=0.7)
    assert [ls.feed(b"\0") for _ in range(3)] == [None, parse("включи свет"), None]
    low = FastListener(lambda: FakeRec(["включи свет"], conf=0.5))
    assert low.feed(b"\0") is None                                  # неуверенно → Gemini


# ── разговор ──

@pytest.fixture
async def hub():
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    await h.start()
    yield h
    await h.stop()


class StubLights:
    def __init__(self, result=None):
        self.calls, self.result = [], result or {"ok": True, "note": "Свет выключен"}

    async def room(self, action):
        self.calls.append(action)
        return self.result


def make_voice(hub, tmp_path, monkeypatch, finals, live=None, brain=True, now=datetime(2026, 10, 20, 14, 5)):
    monkeypatch.setattr(voice_mod, "FOLLOW_UP_S", 0.3)
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.6)
    live = live or FakeLive()
    br = Brain(tmp_path / "b.db") if brain else None
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"), brain=br,
              live_factory=live, fast_factory=lambda: FastListener(lambda: FakeRec(finals)), now=lambda: now)
    return v, live, br


async def talk(v, hub):
    """Начать разговор по слову и вернуть его (как будто колонка сказала «мага»)."""
    task = v.start(hub.get())
    await asyncio.sleep(0.05)
    return v._talks[hub.get().id], task


async def test_light_command_runs_locally_and_closes_gemini_silently(hub, tmp_path, monkeypatch):
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, ["", "выключи свет"])
    hub.lights = StubLights()
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        t.feed(b"\0" * 640)
        await asyncio.wait_for(task, 3)
        await log.wait(is_voice("idle"))
        assert hub.lights.calls == ["off"]
        assert live.texts == [] and v.fast.history[-1][1] == "light_off"   # Gemini текстом ничего не просили
        kinds = [m["type"] for m in log.msgs if isinstance(m, dict)]
        assert "emotion" in kinds and "caption" in kinds                    # тон и мимика, без голоса
        assert not any(isinstance(m, dict) and m["type"] == "voice" and m["state"] == "speaking" for m in log.msgs)
    finally:
        await ws.close()


async def test_gemini_already_answering_wins_over_fastpath(hub, tmp_path, monkeypatch):
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, ["", "выключи свет"])
    hub.lights = StubLights()
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("turn_complete"))
        await asyncio.sleep(0.1)
        t.feed(b"\0" * 640)
        t.feed(b"\0" * 640)
        await live.queue.put(None)
        await asyncio.wait_for(task, 3)
        assert hub.lights.calls == []                   # Gemini уже говорил — локально не выполняем
    finally:
        await ws.close()


async def test_unknown_phrase_goes_to_gemini(hub, tmp_path, monkeypatch):
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, ["какая завтра погода"])
    hub.lights = StubLights()
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("turn_complete"))
        await live.queue.put(None)
        await asyncio.wait_for(task, 3)
        assert hub.lights.calls == [] and not v.fast.history
        assert live.audio                              # звук ушёл в Gemini
    finally:
        await ws.close()


async def test_timer_sets_timer_and_confirms_by_voice(hub, tmp_path, monkeypatch):
    v, live, brain = make_voice(hub, tmp_path, monkeypatch, ["таймер на пять минут"])
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        await asyncio.wait_for(task, 3)
        await log.wait(lambda m: m["type"] == "timer")
        timers = [m for m in log.msgs if isinstance(m, dict) and m["type"] == "timer"]
        assert [m["sec"] for m in timers] == [300] and len(brain.timers) == 1
        await asyncio.sleep(0.2)
        assert any("Готово" in x or "таймер" in x for x in live.texts)     # голосовая подсказка по тексту
    finally:
        await ws.close()


async def test_time_is_caption_only_by_default_and_spoken_in_short_mode(hub, tmp_path, monkeypatch):
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, ["который час", "который час"])
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        await asyncio.wait_for(task, 3)
        caps = [m for m in log.msgs if isinstance(m, dict) and m["type"] == "caption" and m["kind"] == "reply"]
        assert caps and caps[0]["text"] == "14:05" and live.texts == []
        assert await v.command("fast voice short") == "ответы на быстрые команды: short"
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        await asyncio.wait_for(task, 3)
        await asyncio.sleep(0.2)
        assert any("14 часов 05 минут" in x for x in live.texts)
    finally:
        await ws.close()


async def test_light_error_is_spoken(hub, tmp_path, monkeypatch):
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, ["включи свет"])
    hub.lights = StubLights({"ok": False, "error": "не нашёл ни одной лампы"})
    ws = await speaker(hub)
    try:
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        await asyncio.wait_for(task, 3)
        await asyncio.sleep(0.2)
        assert any("не нашёл ни одной лампы" in x for x in live.texts)
    finally:
        await ws.close()


async def test_fastpath_switches_by_console_and_voice(hub, tmp_path, monkeypatch):
    v, live, brain = make_voice(hub, tmp_path, monkeypatch, ["отвечай на команды вслух"])
    assert v.fast.enabled() and v.fast.mode() == "off"
    assert "выкл" in (await v.command("fast off")).lower() or v.fast.enabled() is False
    assert not v.fast.enabled() and v.fast.listener() is None       # выключен → всё в Gemini
    await v.command("fast on")
    assert v.fast.enabled() and "быстрый путь: вкл" in await v.command("fast status")
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        t.feed(b"\0" * 640)
        await asyncio.wait_for(task, 3)
        assert v.fast.mode() == "short" and brain.pref("fastpath_voice") == "short"
    finally:
        await ws.close()


async def test_real_lights_room_actions(lights_env):
    ha, http, state, hub, voice, lights, *_ = lights_env
    assert (await lights.room("dim"))["ok"]
    assert any(c["service_data"].get("brightness_step_pct") == -25 for c in ha.calls)
    assert (await lights.room("off"))["note"] == "Свет выключен"
    assert all(ha.states[e]["state"] == "off" for e in ("light.palka", "light.dimmer", "light.podsvetka_stola"))
    assert (await lights.room("off"))["note"] == "Свет уже выключен"
    assert not (await lights.room("bright"))["ok"]                  # выключен — ярче нечего
    assert (await lights.room("on"))["ok"]


def test_alarm_countdown(hub=None):
    class B:
        phone_state = {"next_alarm": int(datetime(2026, 10, 21, 7, 10).timestamp() * 1000)}

    class H:
        bridge = B()

    class V:
        brain = None
        hub = H()
    from jarvis.fastpath import FastPath
    fp = FastPath(V(), None)
    assert fp._alarm(datetime(2026, 10, 20, 23, 40)).note == "До будильника 7 ч 30 мин"
    H.bridge.phone_state = {}
    assert fp._alarm(datetime(2026, 10, 20, 23, 40)).note == "Будильник не стоит"


# ── шёпот ──

def synth(kind: str, ms: int = 800, amp: float = 3000, rate: int = 16000) -> bytes:
    """voiced — гармоники 150 Гц (голос), noise — шум (шёпот)."""
    rnd = random.Random(1)
    n = rate * ms // 1000
    if kind == "voiced":
        s = (sum(math.sin(2 * math.pi * 150 * h * i / rate) / h for h in range(1, 8)) for i in range(n))
    else:
        s = (rnd.uniform(-1, 1) * 2 for _ in range(n))
    return array("h", (int(amp * x) for x in s)).tobytes()


def test_frame_stats_voiced_vs_noise():
    v = array("h"); v.frombytes(synth("voiced", 40)[:1280])
    w = array("h"); w.frombytes(synth("noise", 40)[:1280])
    assert frame_stats(v)[1] > 0.5 and frame_stats(w)[1] < 0.5


def test_whisper_is_quiet_noise_not_quiet_voice_or_loud_noise():
    def verdict(kind, amp):
        d = WhisperDetector()
        return d.feed(synth(kind, 1000, amp))
    assert verdict("noise", 250) is True          # тихий шум без тона — шёпот
    assert verdict("voiced", 250) is False        # тихо, но с основным тоном — просто тихий голос
    assert verdict("voiced", 4000) is False       # обычная речь
    assert verdict("noise", 6000) is False        # громкий шум — не шёпот
    assert WhisperDetector().feed(b"\0" * 32000) is None      # тишина — решения нет


def test_whisper_volume_is_lower_than_night():
    assert whisper_volume(60, 27) == 15 and whisper_volume(10, 8) == 5 and whisper_volume(4, 4) == 4
    assert whisper_volume(0, 0) == 0


async def test_whisper_config_console():
    cfg = WhisperConfig()
    assert not cfg.enabled() and cfg.detector() is None      # без настройки по живой записи выключено
    await cfg.command("whisper on")
    assert cfg.enabled() and cfg.detector() is not None
    assert "off" in await cfg.command("whisper off") and cfg.detector() is None
    await cfg.command("whisper on")
    assert "-40" in await cfg.command("whisper -40") and cfg.level_db() == -40
    assert "от -70" in await cfg.command("whisper 5")
    assert "порог уровня -40" in await cfg.command("whisper status")


async def test_whispering_conversation_ducks_volume_sends_note_and_restores(hub, tmp_path, monkeypatch):
    from jarvis.bridge import Bridge, SettingsStore
    store = SettingsStore(tmp_path / "settings.json")
    store.update({"volume": 60})
    Bridge(hub, Settings(device_token=TOKEN), store, local_now=lambda: datetime(2026, 10, 20, 15, 0))
    notes = []

    class NoteLive(FakeLive):
        async def send_note(self, text):
            notes.append(text)

    v, live, _ = make_voice(hub, tmp_path, monkeypatch, [], live=NoteLive(), brain=False)
    await v.whisper.command("whisper on")
    ws = await speaker(hub)
    log = Log(ws)
    try:
        t, task = await talk(v, hub)
        t.feed(synth("noise", 1000, 250))
        await asyncio.sleep(0.2)
        assert t.whispered and notes and "шёпотом" in notes[0]
        await live.queue.put(None)
        await asyncio.wait_for(task, 3)
        await asyncio.sleep(0.05)
        levels = [m["level"] for m in log.msgs if isinstance(m, dict) and m["type"] == "volume"]
        assert levels == [60, 15, 60]               # подключение, шёпот, вернули дневную
    finally:
        await ws.close()


async def test_normal_voice_does_not_whisper(hub, tmp_path, monkeypatch):
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, [], brain=False)
    await v.whisper.command("whisper on")
    ws = await speaker(hub)
    try:
        t, task = await talk(v, hub)
        t.feed(synth("voiced", 1000, 4000))
        await asyncio.sleep(0.1)
        assert not t.whispered
        await live.queue.put(None)
        await asyncio.wait_for(task, 3)
    finally:
        await ws.close()


async def test_whisper_is_off_until_enabled(hub, tmp_path, monkeypatch):
    # обычный голос из-за порогов, не проверенных вживую, принимался за шёпот: без `whisper on` детектора нет
    v, live, _ = make_voice(hub, tmp_path, monkeypatch, [], brain=False)
    ws = await speaker(hub)
    try:
        t, task = await talk(v, hub)
        t.feed(synth("noise", 1000, 250))
        await asyncio.sleep(0.1)
        assert t._whisper is None and not t.whispered
        await live.queue.put(None)
        await asyncio.wait_for(task, 3)
    finally:
        await ws.close()
