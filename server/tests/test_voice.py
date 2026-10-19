from datetime import datetime
import asyncio
import base64
import json
import wave
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from jarvis import voice as voice_mod
from jarvis.audio import Audio
from jarvis.config import Settings
from jarvis.devices import DeviceHub
from jarvis.gemini_live import Event, LiveError, LiveSession, parse, setup_message
from jarvis.voice import Voice, system_prompt, tail_bytes

TOKEN = "secret-token"
DATA = Path(__file__).parent / "data"
MODELS = Path(__file__).parents[1] / "data" / "models"


# ── подмены ──

class FakeLive:
    """Вместо Gemini: запоминает, что ему прислали, и отдаёт события из очереди."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.audio: list[bytes] = []
        self.texts: list[str] = []
        self.stream_ends = 0
        self.tool_results: list = []
        self.system = ""
        self.queue: asyncio.Queue = asyncio.Queue()

    def __call__(self, system: str):
        self.system = system
        return self

    async def __aenter__(self):
        if self.fail:
            raise LiveError("нет связи")
        return self

    async def __aexit__(self, *exc):
        pass

    async def send_audio(self, pcm, rate=16000):
        self.audio.append(pcm)

    async def audio_stream_end(self):
        self.stream_ends += 1

    async def send_text(self, text):
        self.texts.append(text)

    async def send_tool_results(self, results):
        self.tool_results.append(results)

    async def events(self):
        while True:
            ev = await self.queue.get()
            if ev is None:
                return
            yield ev


class FakeWake:
    """Срабатывает на кадр, который начинается с b'WAKE'."""

    def __init__(self):
        self.resets = 0

    def process(self, pcm):
        return 0.99 if pcm.startswith(b"WAKE") else 0.01

    def reset(self):
        self.resets += 1


@pytest.fixture
async def hub():
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    await h.start()
    yield h
    await h.stop()


@pytest.fixture
def live():
    return FakeLive()


@pytest.fixture
def voice(hub, live, tmp_path, monkeypatch):
    monkeypatch.setattr(voice_mod, "FOLLOW_UP_S", 0.3)
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.5)
    monkeypatch.setattr(voice_mod, "CAPTION_HOLD_S", 0.05)
    settings = Settings(device_token=TOKEN, gemini_api_key="k")
    return Voice(hub, Audio(hub, tmp_path), settings, wake_factory=FakeWake, live_factory=live)


async def speaker(hub, caps=("display", "mic", "speaker")):
    ws = await connect(f"ws://127.0.0.1:{hub.port}/device")
    await ws.send(json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": "jarvis-hub",
                              "fw": "0.3.0", "caps": list(caps)}))
    await ws.recv()
    return ws


class Log:
    """Всё, что пришло колонке, по порядку; сама отвечает played на audio_end/audio_stop."""

    def __init__(self, ws):
        self.ws = ws
        self.msgs: list = []
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        async for raw in self.ws:
            if isinstance(raw, bytes):
                self.msgs.append(("bin", len(raw)))
                continue
            m = json.loads(raw)
            self.msgs.append(m)
            if m["type"] in ("audio_end", "audio_stop"):
                await self.ws.send(json.dumps({"type": "played"}))

    def types(self):
        return [m if isinstance(m, tuple) else (m["type"], m.get("state") or m.get("kind") or "")
                for m in self.msgs]

    async def wait(self, pred, timeout=3.0):
        async with asyncio.timeout(timeout):
            while not any(pred(m) for m in self.msgs if isinstance(m, dict)):
                await asyncio.sleep(0.01)


def is_voice(state):
    return lambda m: m["type"] == "voice" and m["state"] == state


# ── разговор ──

async def test_connect_starts_listening_for_wake_word(hub, voice):
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "listen_start")
        assert "jarvis-hub" in voice.audio.keep_listening
    finally:
        await ws.close()


async def test_wake_word_conversation(hub, voice, live):
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "listen_start")
        await ws.send(b"\x00\x00" * 1600)            # тишина — ничего
        await ws.send(b"WAKE" + b"\x00" * 3196)      # слово
        await log.wait(is_voice("listening"))
        await ws.send(b"\x01\x00" * 1600)            # фраза идёт в Gemini
        async with asyncio.timeout(2):
            while not live.audio:
                await asyncio.sleep(0.01)
        await live.queue.put(Event("heard", text="сколько "))
        await live.queue.put(Event("heard", text="времени"))
        await log.wait(lambda m: m["type"] == "caption" and m["text"] == "сколько времени")
        await live.queue.put(Event("audio", data=b"\x02\x00" * 2400, rate=24000))
        await live.queue.put(Event("said", text="Без пяти полночь."))
        await live.queue.put(Event("audio", data=b"\x03\x00" * 2400, rate=24000))
        await live.queue.put(Event("turn_complete"))
        await log.wait(lambda m: m["type"] == "audio_end")
        await log.wait(is_voice("idle"))              # продолжения не было — разговор кончился
        t = log.types()
        assert ("caption", "said") in t
        i_speak = t.index(("voice", "speaking"))
        assert t.index(("audio_start", "")) > i_speak
        assert sum(n for kind, n in (x for x in t if x[0] == "bin")) == 2 * 4800
        assert live.stream_ends == 1                  # пока говорит, микрофон Gemini не слушает
        assert voice.reply_text == "Без пяти полночь."
        start = next(m for m in log.msgs if isinstance(m, dict) and m["type"] == "audio_start")
        assert start["rate"] == 24000
    finally:
        await ws.close()


async def test_mic_is_not_sent_while_speaking(hub, voice, live):
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "listen_start")
        await ws.send(b"WAKE" + b"\x00" * 3196)
        await log.wait(is_voice("listening"))
        await live.queue.put(Event("audio", data=b"\x02\x00" * 24000, rate=24000))   # 1 с ответа
        await log.wait(lambda m: m["type"] == "audio_start")
        sent = len(live.audio)
        await ws.send(b"\x07\x00" * 1600)            # эхо ответа в микрофоне
        await asyncio.sleep(0.1)
        assert len(live.audio) == sent
        await live.queue.put(Event("turn_complete"))
        await log.wait(is_voice("idle"))
    finally:
        await ws.close()


async def test_follow_up_without_wake_word(hub, voice, live, monkeypatch):
    monkeypatch.setattr(voice_mod, "FOLLOW_UP_S", 1.0)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "listen_start")
        await ws.send(b"WAKE" + b"\x00" * 3196)
        await log.wait(is_voice("listening"))
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("turn_complete"))
        async with asyncio.timeout(2):                 # после ответа снова слушает — продолжение
            while True:
                t = log.types()
                if ("audio_end", "") in t and ("voice", "listening") in t[t.index(("audio_end", "")):]:
                    break
                await asyncio.sleep(0.01)
        before = len(live.audio)
        await ws.send(b"\x05\x00" * 1600)
        async with asyncio.timeout(2):
            while len(live.audio) == before:
                await asyncio.sleep(0.01)
        await live.queue.put(Event("heard", text="а завтра?"))
        await log.wait(lambda m: m["type"] == "caption" and m["text"] == "а завтра?")
        await live.queue.put(None)                      # Gemini закрыл сессию
        await log.wait(is_voice("idle"))
    finally:
        await ws.close()


async def test_ask_command_speaks_reply(hub, voice, live):
    ws = await speaker(hub, caps=("display", "speaker"))
    log = Log(ws)
    try:
        async def answer():
            async with asyncio.timeout(2):
                while not live.texts:
                    await asyncio.sleep(0.01)
            await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
            await live.queue.put(Event("said", text="Пятьдесят шесть."))
            await live.queue.put(Event("turn_complete"))
        asyncio.create_task(answer())
        reply = await asyncio.wait_for(voice.command("ask сколько будет семь на восемь"), 3)
        assert live.texts == ["сколько будет семь на восемь"]
        assert reply == "текстом: сколько будет семь на восемь\nколонка: Пятьдесят шесть."
        assert await voice.command("last") == reply
        await log.wait(is_voice("idle"))
    finally:
        await ws.close()


async def test_gemini_down_shows_sorry(hub, tmp_path):
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              live_factory=FakeLive(fail=True))
    ws = await speaker(hub, caps=("display", "speaker"))
    log = Log(ws)
    try:
        await v.command("ask привет")
        await log.wait(lambda m: m["type"] == "emotion" and m["name"] == "sorry")
        await log.wait(is_voice("idle"))
    finally:
        await ws.close()


async def test_no_speech_after_wake_ends_conversation(hub, voice, live):
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "listen_start")
        await ws.send(b"WAKE" + b"\x00" * 3196)
        await log.wait(is_voice("listening"))
        await log.wait(is_voice("idle"), timeout=2)   # NO_SPEECH_S = 0.5 в тестах
    finally:
        await ws.close()


# ── мелочи ──

def test_tail_bytes_keeps_end():
    assert tail_bytes("коротко") == "коротко"
    long = "слово " * 40
    out = tail_bytes(long, 60)
    assert out.startswith("…") and len(out.encode()) <= 60 and long.rstrip().endswith(out[1:].rstrip())


def test_system_prompt_has_date_and_tone():
    from datetime import datetime
    p = system_prompt(datetime(2026, 10, 18, 23, 5), sarcasm=80)
    assert "воскресенье, 18 октября 2026, около 23:05" in p
    assert "едко" in p
    assert "едко" not in system_prompt(datetime(2026, 10, 18), sarcasm=10)


def test_parse_server_content():
    pcm = b"\x01\x02" * 10
    evs = parse({"serverContent": {
        "inputTranscription": {"text": "привет"},
        "modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000",
                                                "data": base64.b64encode(pcm).decode()}}]},
        "outputTranscription": {"text": "здравствуй"},
        "turnComplete": True}})
    assert [e.kind for e in evs] == ["heard", "audio", "said", "turn_complete"]
    assert evs[1].data == pcm and evs[1].rate == 24000
    assert parse({"goAway": {"timeLeft": "10s"}})[0].kind == "go_away"
    assert parse({"setupComplete": {}}) == []


def test_setup_message():
    s = setup_message("gemini-3.8-live", "Charon", "будь краток")["setup"]
    assert s["model"] == "models/gemini-3.8-live"
    assert s["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Charon"
    assert s["systemInstruction"]["parts"][0]["text"] == "будь краток"
    assert "inputAudioTranscription" in s and "outputAudioTranscription" in s


async def test_live_session_against_fake_gemini():
    got = []

    async def gemini(ws):
        got.append(("key", ws.request.headers.get("x-goog-api-key")))
        got.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"setupComplete": {}}))
        got.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"serverContent": {"outputTranscription": {"text": "ок"},
                                                    "turnComplete": True}}))
        await ws.close()

    async with serve(gemini, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        async with LiveSession("KEY", "m", "Charon", "sys", url=f"ws://127.0.0.1:{port}") as live:
            await live.send_audio(b"\x01\x00")
            evs = [e.kind async for e in live.events()]
    assert got[0] == ("key", "KEY")
    assert got[1]["setup"]["model"] == "models/m"
    assert base64.b64decode(got[2]["realtimeInput"]["audio"]["data"]) == b"\x01\x00"
    assert evs == ["said", "turn_complete"]


async def test_live_session_reports_rejected_setup():
    async def gemini(ws):
        await ws.recv()
        await ws.close(1008, "models/nope is not found")

    async with serve(gemini, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        with pytest.raises(LiveError, match="nope is not found"):
            async with LiveSession("KEY", "nope", "Charon", "sys", url=f"ws://127.0.0.1:{port}"):
                pass


# ── слово пробуждения на настоящих моделях ──

def read_pcm(path):
    with wave.open(str(path)) as w:
        return w.readframes(w.getnframes())


@pytest.mark.skipif(not (MODELS / "hey_jarvis_v0.1.onnx").is_file(), reason="нет моделей в data/models")
def test_wake_word_models():
    from jarvis.wake import WakeWord
    ww = WakeWord(MODELS)
    pcm = read_pcm(DATA / "hey_jarvis.wav")
    assert max(ww.process(pcm[i:i + 3200]) for i in range(0, len(pcm), 3200)) > 0.5
    ww.reset()
    pcm = read_pcm(DATA / "privet.wav")
    assert max(ww.process(pcm[i:i + 3200]) for i in range(0, len(pcm), 3200)) < 0.1


# ── инструменты ──

async def test_tool_call_runs_and_answers(hub, live, tmp_path, monkeypatch):
    from jarvis.brain import Brain
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.5)
    brain = Brain(tmp_path / "b.db")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=brain, live_factory=live)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        dev = hub.get()
        task = v.start(dev, "запомни, что я люблю чай")
        await live.queue.put(Event("tool_call", calls=(("c1", "remember", {"fact": "любит чай"}),)))
        async with asyncio.timeout(2):
            while not live.tool_results:
                await asyncio.sleep(0.01)
        assert live.tool_results[0] == [("c1", "remember", {"ok": True, "new": True})]
        assert brain.facts() == ["любит чай"]
        await live.queue.put(None)
        await task
    finally:
        await ws.close()


def test_system_prompt_lists_facts():
    from datetime import datetime
    text = system_prompt(datetime(2026, 10, 19, 9, 0), facts=["любит чай"], tools=True)
    assert "любит чай" in text and "remember" in text


def test_setup_message_silence_and_sensitivity():
    vad = setup_message("m", "v", "s", silence_ms=300)["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
    assert vad["silenceDurationMs"] == 300
    assert vad["endOfSpeechSensitivity"] == "END_SENSITIVITY_LOW"   # паузу посреди фразы не считаем концом
    default = setup_message("m", "v", "s")["setup"]["realtimeInputConfig"]["automaticActivityDetection"]
    assert default["silenceDurationMs"] < 700


async def test_gemini_session_is_always_ready_and_refreshed(hub, voice, live, monkeypatch):
    """Сессия Gemini открывается, как только колонка на связи, и обновляется, пока не пригодилась."""
    opened = []
    orig = live.__aenter__

    async def counting():
        opened.append(1)
        return await orig()
    live.__aenter__ = counting
    monkeypatch.setattr(voice_mod, "WARM_TTL_S", 0.15)
    ws = await speaker(hub)
    try:
        await asyncio.sleep(0.05)
        assert len(opened) == 1 and voice._warm is not None
        dev = hub.get()
        voice._detectors[dev.id].process = lambda pcm: 0.4   # «похоже», но ниже порога 0.5
        voice._on_audio(dev, b"x" * 2560)                    # готовая сессия есть — новую не открывать
        await asyncio.sleep(0.05)
        assert len(opened) == 1
        await asyncio.sleep(0.3)                             # устарела — заменена свежей
        assert len(opened) >= 2 and voice._warm is not None
    finally:
        await ws.close()


async def test_dead_warm_session_is_replaced(hub, voice, live):
    ws = await speaker(hub)
    try:
        await asyncio.sleep(0.05)
        assert voice._warm is not None
        live.alive = False                                   # соединение остыло и закрылось
        opened = []
        orig = live.__aenter__

        async def counting():
            opened.append(1)
            return await orig()
        live.__aenter__ = counting
        got = await voice.take_live()
        assert got is live and len(opened) == 1              # открыли новую вместо мёртвой
    finally:
        await ws.close()


async def test_end_conversation_by_context_skips_follow_up(hub, live, tmp_path, monkeypatch):
    from jarvis.brain import Brain
    monkeypatch.setattr(voice_mod, "FOLLOW_UP_S", 30.0)     # без end_conversation ждали бы 30 с
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 5.0)
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=Brain(tmp_path / "b.db"), live_factory=live)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        task = v.start(hub.get(), "спасибо, всё")
        await live.queue.put(Event("tool_call", calls=(("c1", "end_conversation", {}),)))
        await live.queue.put(Event("audio", data=b"\x02\x00" * 2400, rate=24000))
        await live.queue.put(Event("said", text="Пожалуйста."))
        await live.queue.put(Event("turn_complete"))
        async with asyncio.timeout(3):                       # не 30 секунд
            await task
        assert live.tool_results[0] == [("c1", "end_conversation", {"ok": True})]
        await log.wait(is_voice("idle"))
        assert ("voice", "listening") not in log.types()[log.types().index(("voice", "speaking")):]
    finally:
        await ws.close()


# ── перебивание ──

async def test_barge_in_stops_reply_and_drops_old_tail(hub, voice, live):
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "barge")
        assert next(m for m in log.msgs if isinstance(m, dict) and m["type"] == "barge") == {
            "type": "barge", "on": 1, "ratio_x10": 40}
        task = voice.start(hub.get(), "расскажи длинно")
        await live.queue.put(Event("audio", data=b"\x02\x00" * 24000, rate=24000))   # 1 с ответа
        await log.wait(lambda m: m["type"] == "audio_start")
        await ws.send(json.dumps({"type": "barge_in"}))
        await log.wait(lambda m: m["type"] == "audio_stop")
        await log.wait(is_voice("listening"))
        starts = [m for m in log.msgs if isinstance(m, dict) and m["type"] == "audio_start"]
        # остаток старого ответа не играем, пока Gemini не скажет, что перебит
        await live.queue.put(Event("audio", data=b"\x03\x00" * 4800, rate=24000))
        await live.queue.put(Event("said", text="старый хвост"))
        await live.queue.put(Event("interrupted"))
        await asyncio.sleep(0.15)
        assert len([m for m in log.msgs if isinstance(m, dict) and m["type"] == "audio_start"]) == len(starts)
        assert "старый хвост" not in voice.reply_text
        # новый ответ после перебивания играется
        await live.queue.put(Event("audio", data=b"\x04\x00" * 4800, rate=24000))
        async with asyncio.timeout(2):
            while len([m for m in log.msgs if isinstance(m, dict) and m["type"] == "audio_start"]) < len(starts) + 1:
                await asyncio.sleep(0.01)
        await live.queue.put(Event("turn_complete"))
        await live.queue.put(None)
        await task
    finally:
        await ws.close()


async def test_barge_in_ignored_when_not_speaking(hub, voice, live):
    ws = await speaker(hub)
    log = Log(ws)
    try:
        await log.wait(lambda m: m["type"] == "barge")
        await ws.send(json.dumps({"type": "barge_in"}))       # никто не говорит — просто игнор
        await asyncio.sleep(0.1)
        assert ("voice", "listening") not in log.types()
    finally:
        await ws.close()


async def test_start_task_ends_conversation_after_ack(hub, live, tmp_path, monkeypatch):
    from jarvis.agents import Agents
    from jarvis.brain import Brain
    monkeypatch.setattr(voice_mod, "FOLLOW_UP_S", 30.0)
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 5.0)
    ag = Agents("k", "m", lambda p: asyncio.sleep(0, result=True), generate=lambda g: asyncio.sleep(10))
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=Brain(tmp_path / "b.db", agents=ag), live_factory=live)
    ws = await speaker(hub)
    log = Log(ws)
    try:
        task = v.start(hub.get(), "поищи погоду на завтра")
        await live.queue.put(Event("tool_call", calls=(("c1", "start_task", {"goal": "погода завтра"}),)))
        await live.queue.put(Event("audio", data=b"\x02\x00" * 2400, rate=24000))
        await live.queue.put(Event("turn_complete"))
        async with asyncio.timeout(3):                        # без end_conversation ждали бы 30 с
            await task
        assert ("voice", "listening") not in log.types()[log.types().index(("voice", "speaking")):]
        await ag.stop()
    finally:
        await ws.close()


def test_system_prompt_history_block_is_short_and_marked():
    now = datetime(2026, 10, 19, 12, 0)
    msgs = [(datetime(2026, 10, 18, 22, 10), "голос", "user", "старое"),
            (datetime(2026, 10, 19, 11, 30), "telegram", "user", "x" * 500),
            (datetime(2026, 10, 19, 11, 31), "telegram", "assistant", "ответ")]
    p = system_prompt(now, history=msgs)
    assert "Недавний разговор" in p
    assert "- вчера 22:10, голос, пользователь: старое" in p
    assert "- 11:31, telegram, ты: ответ" in p
    assert "x" * 181 not in p and "…" in p
    assert "Недавний разговор" not in system_prompt(now)


async def _talk(hub, live, tmp_path, monkeypatch, start):
    from jarvis.brain import Brain
    monkeypatch.setattr(voice_mod, "NO_SPEECH_S", 0.3)
    brain = Brain(tmp_path / "b.db")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=brain, live_factory=live)
    ws = await speaker(hub)
    Log(ws)
    try:
        await live.queue.put(Event("audio", data=b"\x02\x00" * 240, rate=24000))
        await live.queue.put(Event("said", text="Ответ."))
        await live.queue.put(Event("turn_complete"))
        await live.queue.put(None)
        await start(v, hub.get())
        async with asyncio.timeout(3):
            while v._talks:
                await asyncio.sleep(0.01)
    finally:
        await ws.close()
    return [(m[1], m[2], m[3]) for m in brain.recent_messages()]


async def test_voice_text_conversation_is_logged(hub, live, tmp_path, monkeypatch):
    async def start(v, dev):
        v.start(dev, "сколько будет два плюс два")
    assert await _talk(hub, live, tmp_path, monkeypatch, start) == [
        ("голос", "user", "сколько будет два плюс два"), ("голос", "assistant", "Ответ.")]


async def test_service_prompt_not_logged_as_user(hub, live, tmp_path, monkeypatch):
    async def start(v, dev):
        assert await v.remind("позвонить маме")
    assert await _talk(hub, live, tmp_path, monkeypatch, start) == [("голос", "assistant", "Ответ.")]


async def test_telegram_message_refreshes_warm_session(hub, voice, live, tmp_path):
    from jarvis.brain import Brain
    brain = Brain(tmp_path / "b.db")
    v = Voice(hub, Audio(hub, tmp_path), Settings(device_token=TOKEN, gemini_api_key="k"),
              brain=brain, wake_factory=FakeWake, live_factory=live)
    ws = await speaker(hub)
    try:
        async with asyncio.timeout(2):
            while v._warm is None:
                await asyncio.sleep(0.01)
        assert "Недавний разговор" not in live.system
        old = v._warm
        brain.log_message("telegram", "user", "купить молоко")
        assert v._warm is not old
        async with asyncio.timeout(2):
            while "купить молоко" not in live.system:
                await asyncio.sleep(0.01)
    finally:
        await ws.close()
