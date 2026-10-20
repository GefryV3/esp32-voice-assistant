"""Голос хозяина на подменах: поддельная модель отдаёт детерминированные векторы по «номеру голоса»,
который зашит в громкость звука (голос k — меандр с амплитудой 1000·k)."""

import asyncio
import json
import logging
from types import SimpleNamespace

import numpy as np
import pytest
from websockets.asyncio.client import connect

from jarvis import protocol
from jarvis import voice as voice_mod
from jarvis import voiceid as vid
from jarvis.audio import Audio
from jarvis.brain import Brain
from jarvis.config import Settings
from jarvis.devices import DeviceHub
from jarvis.voice import Voice
from jarvis.voiceid import (OWNER, Match, VoiceID, VoiceStore, classify, cosine, decide, fbank, unit, voiced)

TOKEN = "secret-token"
DIM = 16


def basis(i):
    v = np.zeros(DIM, np.float32)
    v[i] = 1.0
    return v


def voice_vec(spk):
    """1 — хозяин, 2 — Аня, 3 — незнакомец; 5 — «хозяин простуженный» (сходство 0.45), 6 — совсем другой (0.2)."""
    if spk == 5:
        return unit(0.45 * basis(1) + np.sqrt(1 - 0.45 ** 2) * basis(5))
    if spk == 6:
        return unit(0.2 * basis(1) + np.sqrt(1 - 0.2 ** 2) * basis(6))
    return basis(spk)


def speech(spk, seconds=1.5):
    n = int(16_000 * seconds)
    x = np.where(np.arange(n) % 2 == 0, 1000 * spk, -1000 * spk).astype(np.int16)
    return x.tobytes()


def silence(seconds):
    return bytes(int(16_000 * seconds) * 2)


class FakeEmbedder:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def embed(self, pcm):
        self.calls += 1
        if self.fail:
            raise RuntimeError("модель упала")
        x = np.frombuffer(pcm, np.int16).astype(np.float32)
        return voice_vec(round(float(np.sqrt((x ** 2).mean())) / 1000))


class Clock:
    def __init__(self):
        self.t = 1_760_800_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path / "brain.db")


@pytest.fixture
def clock():
    return Clock()


def make(brain, clock, **kw):
    kw.setdefault("embedder", FakeEmbedder())
    return VoiceID(brain, clock=clock, **kw)


def enroll_direct(v, name, spk, n=6):
    v.store.replace(name, [voice_vec(spk) for _ in range(n)])


# ── признаки звука ──

def test_fbank_shape_and_tone_peak():
    n = 16_000
    x = (8000 * np.sin(2 * np.pi * 1000 * np.arange(n) / 16_000)).astype(np.int16)
    f = fbank(x.tobytes())
    assert f.shape == (1 + (n - 400) // 160, 80)
    assert abs(float(f.mean())) < 1e-4                         # среднее вычтено
    peak_bin = int(np.argmax(f.mean(0) + 0.0 * f.std(0)))
    assert 25 <= peak_bin <= 55                                # 1 кГц в середине мел-шкалы 80 полос


def test_fbank_rejects_too_short():
    with pytest.raises(ValueError):
        fbank(bytes(2000))


def test_voiced_finds_speech_and_tail():
    pcm = silence(1.0) + speech(1, 1.0) + silence(0.9)
    _, secs, tail = voiced(pcm)
    assert 0.9 <= secs <= 1.1
    assert 0.8 <= tail <= 1.0
    assert voiced(silence(2.0))[1] == 0.0


# ── хранилище и сравнение ──

def test_store_roundtrip_forget_and_centroid(brain):
    s = VoiceStore(brain.db)
    s.replace(OWNER, [basis(1), basis(1) * 2])
    s.replace("аня", [basis(2)])
    assert s.counts() == {OWNER: 2, "аня": 1}
    assert cosine(s.centroids()[OWNER], basis(1)) == pytest.approx(1.0)
    s.replace(OWNER, [basis(3)])                                # повторная запись заменяет
    assert s.counts()[OWNER] == 1
    assert s.forget("аня") == 1 and "аня" not in s.centroids()
    again = VoiceStore(brain.db)                                # переживает пересоздание (brain.db)
    assert again.counts() == {OWNER: 1}


def cents():
    return {OWNER: basis(1), "аня": basis(2)}


def test_classify_and_decide_threshold():
    m = classify(voice_vec(1), cents(), 0.5)
    assert m.name == OWNER and decide(m, 0.5, False)[0]
    m = classify(voice_vec(2), cents(), 0.5)
    ok, why = decide(m, 0.5, False)
    assert m.name == "аня" and not ok and "аня" in why
    m = classify(voice_vec(3), cents(), 0.5)
    ok, why = decide(m, 0.5, False)
    assert m.name is None and not ok and "чужой голос, сходство" in why
    # граница порога: 0.45 ниже 0.5, а при пороге 0.4 уже хозяин
    assert classify(voice_vec(5), cents(), 0.5).name is None
    assert classify(voice_vec(5), cents(), 0.4).name == OWNER


def test_uncertain_zone_policy_day_vs_night():
    m = classify(voice_vec(5), cents(), 0.5)                     # 0.45: в зоне неуверенности
    assert decide(m, 0.5, night=False)[0] is False               # днём молчим
    ok, why = decide(m, 0.5, night=True)                         # ночью лучше ответить
    assert ok and "ночь" in why
    far = classify(voice_vec(6), cents(), 0.5)                   # 0.2: даже ночью чужой
    assert decide(far, 0.5, night=True)[0] is False
    other = classify(voice_vec(2), cents(), 0.5)                 # записанный другой человек — не неуверенность
    assert decide(other, 0.5, night=True)[0] is False


# ── режимы и команды ──

async def test_default_off_and_gating_needs_samples(brain, clock):
    v = make(brain, clock)
    assert v.mode == "off" and not v.gating()
    enroll_direct(v, OWNER, 1)
    assert not v.gating()                                        # образцы есть, но режим off
    assert "только хозяина" not in (await v.command("voice status"))
    await v.command("voice on")
    assert v.mode == "owner" and v.gating()


async def test_voice_on_requires_samples_and_model(brain, clock):
    v = make(brain, clock)
    assert "образцов хозяина нет" in await v.command("voice on")
    assert v.mode == "off"
    enroll_direct(v, OWNER, 1)
    v.embedder = None
    assert "модель" in await v.command("voice on")
    assert v.mode == "off" and not v.gating()


async def test_threshold_command_validates_and_persists(brain, clock):
    v = make(brain, clock, threshold=0.5)
    assert v.threshold == 0.5
    assert "0.62" in await v.command("voice threshold 0,62")
    assert v.threshold == 0.62
    assert "от 0.2 до 0.9" in await v.command("voice threshold 3")
    assert "число" in await v.command("voice threshold abc")
    assert make(brain, clock).threshold == 0.62                  # pref в brain.db


async def test_any_override_expires(brain, clock):
    v = make(brain, clock)
    enroll_direct(v, OWNER, 1)
    await v.command("voice on")
    assert v.gating()
    assert "10 мин" in await v.command("voice any 10")
    assert not v.gating()                                        # отвечаем всем
    clock.t += 9 * 60
    assert not v.gating()
    clock.t += 2 * 60
    assert v.gating()                                            # время вышло: снова хозяин
    await v.command("voice any 30")
    await v.command("voice any 0")
    assert v.gating()
    await v.command("voice any 30")
    await v.command("voice on")                                  # on снимает переопределение
    assert v.gating()
    assert "отвечаю всем 480" in await v.command("voice any 99999")      # не больше 8 часов


async def test_off_and_forget_owner_disable(brain, clock):
    v = make(brain, clock)
    enroll_direct(v, OWNER, 1)
    enroll_direct(v, "аня", 2)
    await v.command("voice on")
    assert "удалён" in await v.command("voice forget аня")
    assert v.store.counts() == {OWNER: 6}
    await v.command("voice forget я")
    assert v.mode == "off" and v.store.counts() == {}            # без отпечатка хозяина режим снимается
    assert await v.command("voice idle") is None                 # чужие команды voice не перехватываем
    assert await v.command("tg привет") is None


# ── цепочка: слово → проверка голоса → разговор ──

class FakeLive:
    def __init__(self):
        self.audio = []
        self.queue = asyncio.Queue()

    def __call__(self, system):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def send_audio(self, pcm, rate=16000):
        self.audio.append(pcm)

    async def audio_stream_end(self):
        pass

    async def send_text(self, text):
        pass

    async def send_tool_results(self, results):
        pass

    async def events(self):
        while True:
            ev = await self.queue.get()
            if ev is None:
                return
            yield ev


class FakeWake:
    def process(self, pcm):
        return 0.99 if pcm.startswith(b"WAKE") else 0.01

    def reset(self):
        pass


@pytest.fixture
async def rig(brain, clock, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.5)
    hub = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    await hub.start()
    live = FakeLive()
    voice = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
                  wake_factory=FakeWake, live_factory=live)
    night = SimpleNamespace(v=False)
    v = VoiceID(brain, embedder=FakeEmbedder(), hub=hub, voice=voice, clock=clock, night=lambda: night.v)
    enroll_direct(v, OWNER, 1)
    enroll_direct(v, "аня", 2)
    await v.command("voice on")
    ws = await connect(f"ws://127.0.0.1:{hub.port}/device")
    await ws.send(json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": "jarvis-hub",
                              "fw": "0.3.0", "caps": ["display", "mic", "speaker"]}))
    await ws.recv()
    await asyncio.sleep(0.05)
    dev = hub.get()
    voice._detectors[dev.id] = FakeWake()

    def say(pcm, chunk=3200):
        for i in range(0, len(pcm), chunk):
            voice._on_audio(dev, pcm[i:i + chunk])

    async def settle():
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not v._pending and not v._tasks:
                break
        await asyncio.sleep(0.05)

    yield SimpleNamespace(hub=hub, voice=voice, v=v, dev=dev, live=live, say=say, settle=settle, night=night)
    await ws.close()
    await hub.stop()


WAKE = b"WAKE" + bytes(3196)


async def test_owner_voice_opens_conversation_and_audio_catches_up(rig):
    rig.say(WAKE)
    assert rig.dev.id not in rig.voice._talks                    # пока проверяем, разговор не открыт
    rig.say(speech(1, 1.5))
    await rig.settle()
    assert rig.dev.id in rig.voice._talks
    assert rig.live.audio, "звук, пока шла проверка, должен дойти до Gemini"
    assert "хозяина" in rig.v.last_decision


async def test_stranger_is_ignored_and_logged(rig, caplog):
    with caplog.at_level(logging.INFO, logger="jarvis.voiceid"):
        rig.say(WAKE)
        rig.say(speech(3, 1.5))
        await rig.settle()
    assert rig.dev.id not in rig.voice._talks
    assert any("чужой голос, сходство" in r.message for r in caplog.records)
    assert rig.v.last[0] == "незнакомый"


async def test_enrolled_other_person_is_named_but_not_answered(rig):
    rig.say(WAKE)
    rig.say(speech(2, 1.5))
    await rig.settle()
    assert rig.dev.id not in rig.voice._talks
    assert rig.v.last[0] == "аня"
    assert "аня" in rig.v.who()["in_room_by_voice"] and rig.v.who()["last_speaker"] == "аня"


async def test_short_command_decides_after_phrase_ends(rig):
    rig.say(WAKE)
    rig.say(speech(1, 0.6))                                      # «Мага, свет»: речи мало, но фраза кончилась
    rig.say(silence(0.8))
    await rig.settle()
    assert rig.dev.id in rig.voice._talks


async def test_no_speech_after_wake_does_nothing(rig):
    rig.say(WAKE)
    rig.say(silence(4.5))
    await rig.settle()
    assert rig.dev.id not in rig.v._pending and rig.dev.id not in rig.voice._talks
    assert rig.v.embedder.calls == 0


async def test_night_uncertain_answers_day_does_not(rig):
    rig.say(WAKE)
    rig.say(speech(5, 1.5))
    await rig.settle()
    assert rig.dev.id not in rig.voice._talks
    rig.voice._quiet_until.clear()
    rig.night.v = True
    rig.say(WAKE)
    rig.say(speech(5, 1.5))
    await rig.settle()
    assert rig.dev.id in rig.voice._talks
    assert "ночь" in rig.v.last_decision


async def test_model_failure_answers_instead_of_silence(rig):
    rig.v.embedder.fail = True
    rig.say(WAKE)
    rig.say(speech(3, 1.5))
    await rig.settle()
    assert rig.dev.id in rig.voice._talks


async def test_any_override_answers_stranger_without_check(rig):
    await rig.v.command("voice any 15")
    rig.say(WAKE)
    assert rig.dev.id in rig.voice._talks                        # проверки нет: разговор открыт сразу
    assert rig.v.embedder.calls == 0


async def test_mode_off_changes_nothing(rig):
    await rig.v.command("voice off")
    rig.say(WAKE)
    assert rig.dev.id in rig.voice._talks and not rig.v._pending


# ── кто в комнате ──

async def test_who_tool(brain, clock):
    v = make(brain, clock)
    assert v.who()["ok"] is False                                # голосов нет
    enroll_direct(v, OWNER, 1)
    enroll_direct(v, "аня", 2)
    assert v.who()["in_room_by_voice"] == []
    v._remember(Match("аня", 0.8, ("аня", 0.8)))
    clock.t += 120
    v._remember(Match(OWNER, 0.9, None))
    out = await v.call("who_is_here", {})
    assert out["in_room_by_voice"] == ["аня", OWNER] or sorted(out["in_room_by_voice"]) == sorted(["аня", OWNER])
    assert out["last_speaker"] == OWNER
    clock.t += 20 * 60                                           # через 20 минут никого «в комнате» нет
    assert v.who()["in_room_by_voice"] == []
    assert "who_is_here" in v.tool_names and v.TOOLS[0]["name"] == "who_is_here"


# ── запись образцов ──

class FakeDev:
    id = "jarvis-hub"

    def __init__(self):
        self.sent = []
        self.hello = SimpleNamespace(caps=("mic", "speaker", "display"))

    async def send(self, msg):
        self.sent.append(msg)


class FakeAudio:
    def __init__(self, script):
        self.script = list(script)       # что «услышала» колонка на каждую запись: PCM
        self.records = 0
        self.plays = 0

    async def record(self, dev, seconds):
        self.records += 1
        return self.script.pop(0) if self.script else silence(seconds)

    async def play(self, dev, pcm, rate=16000):
        self.plays += 1


def enroller(brain, clock, script, notify=None):
    dev = FakeDev()
    hub = SimpleNamespace(get=lambda: dev, on_audio=lambda cb: None)
    v = make(brain, clock, hub=hub, audio=FakeAudio(script), notify=notify)
    v.sleep = lambda s: asyncio.sleep(0)
    return v, dev


async def test_enroll_owner_saves_ten_samples_and_asks_to_turn_on(brain, clock):
    v, dev = enroller(brain, clock, [speech(1, 2.0)] * 10)
    text = await v._enroll(dev, OWNER)
    assert "10 образцов" in text and "voice on" in text
    assert v.store.counts() == {OWNER: 10}
    assert v.enrolling is None
    caps = [m["text"] for m in dev.sent if m["type"] == "caption" and m["kind"] == "reply"]
    assert len(caps) == 10 and caps[0].startswith("1/10")
    assert v.audio.plays == 10


async def test_enroll_retries_silent_phrase_then_skips(brain, clock):
    script = [silence(5), speech(1, 2.0)] + [speech(1, 2.0)] * 9      # первая фраза со второй попытки
    v, dev = enroller(brain, clock, script)
    assert "10 образцов" in await v._enroll(dev, OWNER)
    assert v.audio.records == 11


async def test_enroll_too_few_samples_saves_nothing(brain, clock):
    v, dev = enroller(brain, clock, [speech(1, 2.0)] * 3)            # дальше тишина
    text = await v._enroll(dev, OWNER)
    assert "ничего не сохранено" in text
    assert v.store.counts() == {}


async def test_enroll_other_person_has_consent_phrase_and_own_name(brain, clock):
    v, dev = enroller(brain, clock, [speech(2, 2.0)] * 10)
    text = await v._enroll(dev, "аня")
    assert "«аня»" in text and "voice on" not in text
    assert v.store.counts() == {"аня": 10}
    first = next(m["text"] for m in dev.sent if m["type"] == "caption" and m["kind"] == "reply")
    assert "согласен" in first and "аня" in first


def test_all_enroll_captions_fit_the_screen():
    for phrase in (*vid.PHRASES, vid.CONSENT.format(name="я" * vid.MAX_NAME)):
        protocol.caption("reply", vid._clip(f"10/10 скажи: {phrase}"))
    assert len(vid.PHRASES) == 10


async def test_enroll_command_runs_in_background_and_notifies(brain, clock):
    sent = []

    async def notify(text):
        sent.append(text)
    v, dev = enroller(brain, clock, [speech(1, 2.0)] * 10, notify=notify)
    out = await v.command("voice enroll")
    assert "10" in out and "векторы" in out
    for _ in range(100):
        await asyncio.sleep(0.01)
        if sent:
            break
    assert sent and "записан" in sent[0]
    assert "хозяин: 10" in await v.command("voice status")
    # занятую колонку второй раз не записываем
    v.enrolling = "кто-то"
    assert "занята" in await v.command("voice enroll аня")


async def test_enroll_needs_model_and_device(brain, clock):
    v = make(brain, clock, embedder=None)
    assert "модель" in await v.command("voice enroll")
    v = make(brain, clock)
    assert "не подключена" in await v.command("voice enroll")


# ── охрана ──

class Presence:
    def __init__(self, here):
        self._here = here

    def here(self):
        return self._here


def guard_rig(brain, clock, here=False, on=True):
    sent = []

    async def notify(text):
        sent.append(text)
    pres = Presence(here)
    v = make(brain, clock, presence=pres, notify=notify)
    if on:
        brain.set_pref("guard", "on")
    return v, pres, sent


async def feed_speech(v, clock, seconds, spk=3):
    dev = SimpleNamespace(id="jarvis-hub")
    for _ in range(int(seconds / 0.1)):
        clock.t += 0.1
        v._guard_audio(dev, speech(spk, 0.1))
    for _ in range(20):
        await asyncio.sleep(0.01)


async def test_guard_off_by_default(brain, clock):
    v, _, sent = guard_rig(brain, clock, on=False)
    assert not v.guard_on
    await feed_speech(v, clock, 6)
    assert sent == []


async def test_guard_notifies_once_with_unknown_voice_and_no_content(brain, clock):
    v, _, sent = guard_rig(brain, clock)
    enroll_direct(v, OWNER, 1)
    await feed_speech(v, clock, 6)
    assert len(sent) == 1
    assert "речь" in sent[0] and "незнакомый" in sent[0] and "Записи и расшифровки нет" in sent[0]
    await feed_speech(v, clock, 6)                               # антиспам: в пределах 10 минут второго нет
    assert len(sent) == 1
    clock.t += 11 * 60
    await feed_speech(v, clock, 6)
    assert len(sent) == 2


async def test_guard_names_enrolled_person_and_owner(brain, clock):
    v, _, sent = guard_rig(brain, clock)
    enroll_direct(v, OWNER, 1)
    enroll_direct(v, "аня", 2)
    await feed_speech(v, clock, 6, spk=2)
    assert "«аня»" in sent[0]
    clock.t += 11 * 60
    await feed_speech(v, clock, 6, spk=1)
    assert "хозяина" in sent[1]


async def test_guard_silent_when_owner_here_or_speech_short(brain, clock):
    v, pres, sent = guard_rig(brain, clock, here=True)
    await feed_speech(v, clock, 8)
    assert sent == []
    pres._here = False
    await feed_speech(v, clock, 2)                               # меньше 3 с речи
    assert sent == []


async def test_guard_without_prints_still_reports_without_identity(brain, clock):
    v, _, sent = guard_rig(brain, clock)
    await feed_speech(v, clock, 5)
    assert len(sent) == 1 and "не определён" in sent[0]


async def test_guard_ignores_noise_below_speech_level(brain, clock):
    v, _, sent = guard_rig(brain, clock)
    dev = SimpleNamespace(id="jarvis-hub")
    for _ in range(100):
        clock.t += 0.1
        v._guard_audio(dev, silence(0.1))
    await asyncio.sleep(0.05)
    assert sent == []


async def test_guard_command_toggles_pref(brain, clock):
    v = make(brain, clock)
    assert "off" in await v.command("guard")
    assert "включена" in await v.command("guard on") and v.guard_on
    assert "выключена" in await v.command("/guard off") and not v.guard_on
