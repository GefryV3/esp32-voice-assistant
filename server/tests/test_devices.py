import asyncio
import json

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from jarvis import protocol
from jarvis.config import Settings
from jarvis.console import add_admin_route
from jarvis.devices import DeviceHub

TOKEN = "secret-token"


@pytest.fixture
async def hub():
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0),
                  hello_timeout=0.3, clock=lambda: 1_760_700_000.0)
    await h.start()
    yield h
    await h.stop()


def url(hub, path="/device"):
    return f"ws://127.0.0.1:{hub.port}{path}"


def hello(token=TOKEN, device="jarvis-hub"):
    return json.dumps({"type": "hello", "proto": 1, "token": token, "device": device,
                       "fw": "0.1.0", "caps": ["display"]})


async def wait_for(cond, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def test_handshake_registers_device_and_sends_time(hub):
    async with connect(url(hub)) as ws:
        await ws.send(hello())
        welcome = json.loads(await ws.recv())
        assert welcome["type"] == "welcome"
        assert welcome["time"] == 1_760_700_000
        assert welcome["tz"] == Settings(device_token="x").tz
        await wait_for(lambda: "jarvis-hub" in hub.devices)
    await wait_for(lambda: not hub.devices)


async def test_command_reaches_device(hub):
    async with connect(url(hub)) as ws:
        await ws.send(hello())
        await ws.recv()
        await wait_for(lambda: hub.get() is not None)
        await hub.get().send(protocol.dice(5))
        assert json.loads(await ws.recv()) == {"type": "game", "name": "dice", "result": 5}


async def test_status_is_stored_and_forwarded(hub):
    seen = []

    async def handler(dev, msg):
        seen.append((dev.id, msg["type"]))

    hub.on_message(handler)
    async with connect(url(hub)) as ws:
        await ws.send(hello())
        await ws.recv()
        await ws.send(json.dumps({"type": "status", "uptime": 12, "rssi": -60}))
        await wait_for(lambda: seen)
    assert seen == [("jarvis-hub", "status")]


@pytest.mark.parametrize("first, code", [
    (hello(token="wrong"), protocol.CLOSE_BAD_TOKEN),
    (json.dumps({"type": "status"}), protocol.CLOSE_BAD_HELLO),
    (None, protocol.CLOSE_BAD_HELLO),          # молчит дольше hello_timeout
])
async def test_bad_handshake_is_closed_with_code(hub, first, code):
    async with connect(url(hub)) as ws:
        if first is not None:
            await ws.send(first)
        with pytest.raises(ConnectionClosed) as e:
            await ws.recv()
        assert e.value.rcvd.code == code
    assert not hub.devices


async def test_wrong_path_is_rejected(hub):
    async with connect(url(hub, "/nope")) as ws:
        with pytest.raises(ConnectionClosed) as e:
            await ws.recv()
        assert e.value.rcvd.code == 1008


async def test_admin_route_sends_console_commands(hub):
    add_admin_route(hub, TOKEN)
    async with connect(url(hub)) as dev_ws:
        await dev_ws.send(hello())
        await dev_ws.recv()
        await wait_for(lambda: hub.get() is not None)
        async with connect(url(hub, "/admin")) as admin:
            await admin.send(TOKEN)
            await admin.send("dice 4")
            assert (await admin.recv()).startswith("→ jarvis-hub")
            assert json.loads(await dev_ws.recv()) == {"type": "game", "name": "dice", "result": 4}
            await admin.send("devices")
            assert "jarvis-hub" in await admin.recv()


async def test_admin_route_rejects_bad_token(hub):
    add_admin_route(hub, TOKEN)
    async with connect(url(hub, "/admin")) as admin:
        await admin.send("wrong")
        with pytest.raises(ConnectionClosed) as e:
            await admin.recv()
        assert e.value.rcvd.code == protocol.CLOSE_BAD_TOKEN


async def test_reconnect_replaces_old_connection(hub):
    async with connect(url(hub)) as old:
        await old.send(hello())
        await old.recv()
        async with connect(url(hub)) as new:
            await new.send(hello())
            await new.recv()
            with pytest.raises(ConnectionClosed) as e:
                await old.recv()
            assert e.value.rcvd.code == protocol.CLOSE_REPLACED
            await wait_for(lambda: len(hub.devices) == 1)
            await hub.get().send(protocol.coin(1))
            assert json.loads(await new.recv())["name"] == "coin"


async def test_odd_audio_frame_and_failing_handler_do_not_drop_the_speaker(hub):
    # np.frombuffer падал на нечётном кадре и рвал соединение с колонкой («отключилась jarvis-hub»)
    seen = []

    def audio(dev, pcm):
        seen.append(len(pcm))
        np.frombuffer(pcm, dtype=np.int16)           # как в wake.py
        if len(pcm) == 4:
            raise RuntimeError("обработчик упал")

    hub.on_audio(audio)
    async with connect(url(hub)) as ws:
        await ws.send(hello())
        await ws.recv()
        await ws.send(b"\x01\x02\x03")               # нечётный: хвост обрезается, обработчик получает 2 байта
        await ws.send(b"\x01\x02\x03\x04")           # обработчик бросает исключение
        await ws.send(json.dumps({"type": "status", "uptime": 5, "heap": 1, "psram": 1, "rssi": -50, "fps": 30}))
        await wait_for(lambda: hub.get() is not None and hub.get().status.get("uptime") == 5)
        assert seen == [2, 4]
        assert hub.get() is not None                 # колонка всё ещё на связи
