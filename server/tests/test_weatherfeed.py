import asyncio
import json

from websockets.asyncio.client import connect

from jarvis.config import Settings
from jarvis.devices import DeviceHub
from jarvis.weatherfeed import WeatherFeed

TOKEN = "secret-token"


class FakeHA:
    def __init__(self, ok=True, fail=False):
        self.ok, self.fail, self.calls = ok, fail, 0

    async def weather(self, send):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HA недоступен")
        if self.ok:
            await send({"type": "weather", "cond": 1, "temp": 12})
        return {"ok": self.ok}


async def speaker(hub, caps):
    ws = await connect(f"ws://127.0.0.1:{hub.port}/device")
    await ws.send(json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": "jarvis-hub",
                              "fw": "0.7.1", "caps": caps}))
    await ws.recv()
    return ws


async def hub_with(ha, refresh_s=900.0):
    hub = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    feed = WeatherFeed(hub, ha, refresh_s=refresh_s)
    await hub.start()
    return hub, feed


async def test_weather_goes_to_screen_on_connect():
    ha = FakeHA()
    hub, _ = await hub_with(ha)
    try:
        ws = await speaker(hub, ["display", "mic", "speaker"])
        msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert msg == {"type": "weather", "cond": 1, "temp": 12}
        await ws.close()
    finally:
        await hub.stop()


async def test_weather_refreshes_periodically():
    ha = FakeHA()
    hub, feed = await hub_with(ha, refresh_s=0.05)
    try:
        ws = await speaker(hub, ["display"])
        feed.start()
        await asyncio.sleep(0.3)
        assert ha.calls >= 3
        await ws.close()
    finally:
        await hub.stop()


async def test_no_display_or_ha_failure_is_harmless():
    for ha, caps in ((FakeHA(), ["mic"]), (FakeHA(fail=True), ["display"]), (FakeHA(ok=False), ["display"])):
        hub, feed = await hub_with(ha)
        try:
            ws = await speaker(hub, caps)
            await asyncio.sleep(0.1)
            assert await feed.push() is False
            await ws.close()
        finally:
            await hub.stop()
