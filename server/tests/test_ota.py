import asyncio
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from jarvis.config import Settings
from jarvis.devices import DeviceHub
from jarvis.ota import APP_DESC, APP_MAGIC, Firmware, Ota

TOKEN = "secret-token"
BUILT = Path(__file__).parents[2] / "firmware" / "build" / "jarvis.bin"


def fake_image(version: str, size: int = 4096) -> bytes:
    data = bytearray(size)
    data[0] = 0xE9                                     # заголовок образа ESP
    data[APP_DESC:APP_DESC + 4] = APP_MAGIC.to_bytes(4, "little")
    v = version.encode()
    data[APP_DESC + 16:APP_DESC + 16 + len(v)] = v
    return bytes(data)


@pytest.fixture
def image(tmp_path):
    p = tmp_path / "jarvis.bin"
    p.write_bytes(fake_image("0.9.0"))
    return p


@pytest.fixture
async def hub(image):
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    Ota(h, Settings(device_token=TOKEN), str(image))
    await h.start()
    yield h
    await h.stop()


async def speaker(hub, fw="0.4.0"):
    ws = await connect(f"ws://127.0.0.1:{hub.port}/device")
    await ws.send(json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": "jarvis-hub",
                              "fw": fw, "caps": ["display"]}))
    await ws.recv()
    async with asyncio.timeout(1):
        while hub.get() is None or hub.get().hello.fw != fw:
            await asyncio.sleep(0.01)
    return ws


def test_firmware_version_from_image(image):
    fw = Firmware.load(image)
    assert fw.version == "0.9.0" and fw.size == 4096 and len(fw.sha256) == 64


def test_rejects_not_an_image(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"\0" * 200)
    with pytest.raises(ValueError):
        Firmware.load(p)


@pytest.mark.skipif(not BUILT.is_file(), reason="прошивка не собрана")
def test_reads_real_build():
    assert Firmware.load(BUILT).version.count(".") == 2


async def test_http_serves_image_only_with_token(hub, image):
    base = f"http://127.0.0.1:{hub.port}/firmware.bin"
    body = await asyncio.to_thread(lambda: urllib.request.urlopen(f"{base}?token={TOKEN}", timeout=5).read())
    assert body == image.read_bytes()
    with pytest.raises(urllib.error.HTTPError) as e:
        await asyncio.to_thread(lambda: urllib.request.urlopen(f"{base}?token=nope", timeout=5))
    assert e.value.code == 403


async def test_ota_info_and_same_version(hub):
    ws = await speaker(hub, fw="0.9.0")
    try:
        assert "на сервере 0.9.0" in await hub.ota.command("ota info")
        assert "уже на 0.9.0" in await hub.ota.command("ota")
    finally:
        await ws.close()


async def test_ota_full_cycle(hub):
    ws = await speaker(hub, fw="0.4.0")

    async def device():
        msg = json.loads(await ws.recv())
        assert msg["type"] == "ota" and msg["version"] == "0.9.0" and msg["url"].startswith("/firmware.bin?token=")
        for pct in (10, 50, 100):
            await ws.send(json.dumps({"type": "ota_state", "state": "progress", "pct": pct}))
        await ws.send(json.dumps({"type": "ota_state", "state": "done", "pct": 100}))
        await ws.close()                       # перезагрузка
        return await speaker(hub, fw="0.9.0")  # вернулась с новой версией

    task = asyncio.create_task(device())
    out = await asyncio.wait_for(hub.ota.command("ota"), 5)
    assert out == "jarvis-hub: обновлена 0.4.0 → 0.9.0"
    await (await task).close()


async def test_ota_error_is_reported(hub):
    ws = await speaker(hub, fw="0.4.0")

    async def device():
        await ws.recv()
        await ws.send(json.dumps({"type": "ota_state", "state": "error", "pct": 0, "error": "ESP_ERR_HTTP_CONNECT"}))

    asyncio.create_task(device())
    try:
        out = await asyncio.wait_for(hub.ota.command("ota"), 5)
        assert "ESP_ERR_HTTP_CONNECT" in out
    finally:
        await ws.close()


async def test_websocket_still_works_with_http_routes(hub):
    ws = await speaker(hub)
    await ws.close()
