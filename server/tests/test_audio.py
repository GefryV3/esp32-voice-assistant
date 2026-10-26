import asyncio
import io
import json
import wave
from array import array

import pytest
from websockets.asyncio.client import connect

from jarvis import audio as audio_mod
from jarvis import protocol
from jarvis.audio import Audio, levels, read_wav, tone, wav_bytes
from jarvis.config import Settings
from jarvis.console import add_admin_route
from jarvis.devices import DeviceHub

TOKEN = "secret-token"


@pytest.fixture
async def hub():
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    await h.start()
    yield h
    await h.stop()


@pytest.fixture
def audio(hub, tmp_path):
    return Audio(hub, tmp_path)


def url(hub, path="/device"):
    return f"ws://127.0.0.1:{hub.port}{path}"


async def speaker(hub, caps=("display", "mic", "speaker")):
    ws = await connect(url(hub))
    await ws.send(json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": "jarvis-hub",
                              "fw": "0.2.0", "caps": list(caps)}))
    await ws.recv()  # welcome
    async with asyncio.timeout(1):
        while hub.get() is None:
            await asyncio.sleep(0.01)
    return ws


async def recv_json(ws, type_):
    async with asyncio.timeout(2):
        while True:
            raw = await ws.recv()
            if isinstance(raw, str) and json.loads(raw)["type"] == type_:
                return json.loads(raw)


# ── PCM и WAV ──

def make_wav(samples, rate=16000, channels=1, width=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(samples)
    return buf.getvalue()


def test_wav_roundtrip():
    pcm = array("h", [0, 1000, -1000, 32767]).tobytes()
    assert read_wav(wav_bytes(pcm)) == (pcm, 16000)


def test_read_wav_downmixes_stereo():
    stereo = array("h", [1000, 3000, -2000, -4000]).tobytes()
    pcm, rate = read_wav(make_wav(stereo, rate=22050, channels=2))
    assert rate == 22050
    assert array("h", pcm).tolist() == [2000, -3000]


def test_read_wav_converts_8_and_24_bit():
    assert array("h", read_wav(make_wav(bytes([128, 255, 0]), width=1))[0]).tolist() == [0, 127 << 8, -128 << 8]
    s24 = (0x123456).to_bytes(3, "little") + (-0x123456 & 0xFFFFFF).to_bytes(3, "little")
    assert array("h", read_wav(make_wav(s24, width=3))[0]).tolist() == [0x1234, -0x1235]


def test_read_wav_rejects_odd_rate():
    with pytest.raises(ValueError):
        read_wav(make_wav(b"\0\0", rate=96000))


def test_tone_and_levels():
    pcm = tone(1000, 100, level=0.5)
    assert len(pcm) == 16000 * 2 // 10
    rms, peak, clipped = levels(pcm)
    assert -10 < rms < -8          # синус 0.5 ≈ −9 дБFS
    assert -6.5 < peak < -5.5
    assert clipped == 0
    assert levels(b"")[0] == float("-inf")


def test_protocol_audio_messages():
    assert protocol.listen_start(6) == {"type": "listen_start", "gain": 6}
    assert protocol.audio_start(24000)["rate"] == 24000
    assert protocol.volume(0)["level"] == 0
    for bad in (lambda: protocol.listen_start(31), lambda: protocol.audio_start(96000),
                lambda: protocol.volume(101)):
        with pytest.raises(ValueError):
            bad()


# ── запись и воспроизведение через колонку ──

async def test_record_collects_mic_frames(hub, audio):
    ws = await speaker(hub)
    try:
        task = asyncio.create_task(audio.record(hub.get(), 0.3))
        assert (await recv_json(ws, "listen_start"))["gain"] == 12
        await ws.send(b"\x01\x00" * 320)
        await ws.send(b"\x02\x00" * 320)
        await recv_json(ws, "listen_stop")
        assert await task == b"\x01\x00" * 320 + b"\x02\x00" * 320
    finally:
        await ws.close()


async def test_frames_outside_recording_are_ignored(hub, audio):
    ws = await speaker(hub)
    try:
        await ws.send(b"\x05\x00" * 320)   # до listen_start — никому не нужен
        await ws.send(json.dumps({"type": "status", "uptime": 1}))
        async with asyncio.timeout(1):     # статус пришёл — значит, и кадр до него разобран
            while not hub.get().status:
                await asyncio.sleep(0.01)
        task = asyncio.create_task(audio.record(hub.get(), 0.2))
        await recv_json(ws, "listen_start")
        await recv_json(ws, "listen_stop")
        assert await task == b""
    finally:
        await ws.close()


async def test_play_streams_pcm_and_waits_for_played(hub, audio):
    ws = await speaker(hub)
    try:
        pcm = tone(440, 250)
        task = asyncio.create_task(audio.play(hub.get(), pcm))
        assert (await recv_json(ws, "audio_start"))["rate"] == 16000
        got = bytearray()
        async with asyncio.timeout(2):
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    assert len(raw) <= 3200          # куски по 100 мс
                    got += raw
                elif json.loads(raw)["type"] == "audio_end":
                    break
        assert bytes(got) == pcm
        await asyncio.sleep(0.05)
        assert not task.done()                        # ждёт, пока колонка доиграет
        await ws.send(json.dumps({"type": "played"}))
        await asyncio.wait_for(task, 1)
    finally:
        await ws.close()


async def test_play_gives_up_without_played(hub, audio, monkeypatch):
    monkeypatch.setattr(audio_mod, "PLAYED_SLACK_S", 0.1)
    ws = await speaker(hub)
    try:
        await asyncio.wait_for(audio.play(hub.get(), tone(440, 20)), 2)
    finally:
        await ws.close()


async def test_admin_rec_returns_wav_then_text(hub, audio, tmp_path):
    add_admin_route(hub, TOKEN)
    ws = await speaker(hub)
    try:
        async with connect(url(hub, "/admin")) as admin:
            await admin.send(TOKEN)
            await admin.send("rec 0.5")
            await recv_json(ws, "listen_start")
            await ws.send(tone(500, 200))
            wav = await asyncio.wait_for(admin.recv(), 3)
            text = await admin.recv()
        assert isinstance(wav, bytes) and read_wav(wav)[0] == tone(500, 200)
        assert text.startswith("запись ") and "потеряно" in text   # пришло 0.2 с из 0.5
        assert len(list((tmp_path / "recordings").glob("*.wav"))) == 1
    finally:
        await ws.close()


async def test_admin_stream_forwards_pcm_to_speaker(hub, audio):
    add_admin_route(hub, TOKEN)
    ws = await speaker(hub)
    try:
        async with connect(url(hub, "/admin")) as admin:
            await admin.send(TOKEN)
            await admin.send("stream 24000 music")
            assert await admin.recv() == "ok"
            start = await recv_json(ws, "audio_start")
            assert start["rate"] == 24000 and start["music"] is True
            pcm = tone(500, 200, 24000)
            await admin.send(pcm)
            got = b""
            async with asyncio.timeout(2):
                while len(got) < len(pcm):
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        got += raw
            assert got == pcm
            await admin.send("stream_end")
            assert await admin.recv() == "ok"
            await recv_json(ws, "audio_end")
            await ws.send(json.dumps({"type": "played"}))
    finally:
        await ws.close()


async def test_admin_stream_rejects_bad_rate_and_missing_speaker(hub, audio):
    add_admin_route(hub, TOKEN)
    async with connect(url(hub, "/admin")) as admin:
        await admin.send(TOKEN)
        await admin.send("stream 100")
        assert (await admin.recv()).startswith("stream 8000")
        await admin.send("stream 24000")
        assert await admin.recv() == "no speaker to play on"


async def test_commands_check_caps(hub, audio):
    ws = await speaker(hub, caps=("display",))
    try:
        assert "прошивка без «mic»" in await audio.command("rec 1")
        assert "прошивка без «speaker»" in await audio.command("beep")
        assert await audio.command("dice 3") is None
        assert await audio.command("gain 40") == "gain 0…30 (дБ), сейчас 12"
        assert await audio.command("gain 20") == "усиление микрофона 20 дБ"
    finally:
        await ws.close()
