import asyncio
import json
from datetime import datetime, timedelta

import pytest
from websockets.asyncio.client import connect

from jarvis import protocol
from jarvis.brain import Brain
from jarvis.config import Settings, load_settings
from jarvis.console import execute
from jarvis.devices import DeviceHub
from jarvis.offline import Offline

TOKEN = "secret-token"
NOW = datetime(2026, 10, 20, 9, 0)


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path / "b.db", now=lambda: NOW)


@pytest.fixture
async def hub(brain):
    h = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    h.offline_mod = Offline(h, brain)
    await h.start()
    yield h
    await h.stop()


async def speaker(hub, caps=("display", "mic", "speaker")):
    ws = await connect(f"ws://127.0.0.1:{hub.port}/device")
    await ws.send(json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": "jarvis-hub",
                              "fw": "0.13.0", "caps": list(caps)}))
    await ws.recv()   # welcome
    async with asyncio.timeout(1):
        while hub.get() is None:
            await asyncio.sleep(0.01)
    return ws


async def recv(ws):
    async with asyncio.timeout(2):
        return json.loads(await ws.recv())


# ── протокол ──

def test_config_clap_fields_only_when_given():
    assert protocol.config(clap=True, clap_sens=7) == {"type": "config", "clap": 1, "clap_sens": 7}
    assert protocol.config(clap=False) == {"type": "config", "clap": 0}
    assert protocol.config(sleep_after=60) == {"type": "config", "sleep_after": 60}
    with pytest.raises(ValueError):
        protocol.config(clap_sens=11)


def test_plan_limits():
    msg = protocol.plan([(1760940000, "Подъём")], [])
    assert msg == {"type": "plan", "alarms": [{"at": 1760940000, "label": "Подъём"}], "timers": []}
    many = protocol.plan([(i, "x") for i in range(20)], [])
    assert len(many["alarms"]) == protocol.PLAN_MAX
    with pytest.raises(ValueError):
        protocol.plan([(1, "я" * 20)], [])      # 40 байт


def test_settings_clap_env():
    s = load_settings(environ={"JARVIS_DEVICE_TOKEN": "t", "JARVIS_CLAP": "off", "JARVIS_CLAP_SENS": "8"})
    assert s.clap is False and s.clap_sens == 8
    assert load_settings(environ={"JARVIS_DEVICE_TOKEN": "t"}).clap is True


# ── план из Brain ──

async def test_plan_rows_reminders_and_timers(brain):
    async def send(msg):
        pass
    await brain.call("add_reminder", {"text": "позвонить маме", "minutes": 30}, send)
    await brain.call("add_reminder", {"text": "я" * 30, "minutes": 10}, send)
    await brain.call("set_timer", {"minutes": 5, "label": "чай"}, send)
    alarms, timers = brain.plan_rows()
    stamp = int(NOW.timestamp())
    assert [a for a, _ in alarms] == [stamp + 600, stamp + 1800]
    assert alarms[1][1] == "позвонить маме"
    assert len(alarms[0][1].encode()) <= 31          # подпись обрезана до 31 байта
    assert timers == [(stamp + 300, "чай")]
    await brain.call("set_timer", {"minutes": 0}, send)
    assert brain.plan_rows()[1] == []


async def test_plan_skips_past_and_done(brain):
    brain.add_reminder("прошло", NOW - timedelta(minutes=5))
    rid = brain.add_reminder("будет", NOW + timedelta(hours=1))
    assert [t for _, t in brain.plan_rows()[0]] == ["будет"]
    brain.finish(rid)
    assert brain.plan_rows()[0] == []


# ── сервер ↔ колонка ──

async def test_connect_sends_config_and_plan(hub, brain):
    brain.add_reminder("пить воду", NOW + timedelta(minutes=20))
    ws = await speaker(hub)
    cfg, plan = await recv(ws), await recv(ws)
    assert cfg == {"type": "config", "clap": 1, "clap_sens": 5}
    assert plan["type"] == "plan"
    assert plan["alarms"] == [{"at": int((NOW + timedelta(minutes=20)).timestamp()), "label": "пить воду"}]
    await ws.close()


async def test_no_mic_no_clap_config(hub):
    ws = await speaker(hub, caps=("display",))
    assert (await recv(ws))["type"] == "plan"
    await ws.close()


async def test_plan_pushed_on_change(hub, brain):
    ws = await speaker(hub)
    await recv(ws), await recv(ws)
    async def send(msg):
        pass
    await brain.call("add_reminder", {"text": "сходить", "minutes": 15}, send)
    plan = await recv(ws)
    assert plan["type"] == "plan" and plan["alarms"][0]["label"] == "сходить"
    await brain.call("cancel_reminder", {"text": "сходить"}, send)
    assert (await recv(ws))["alarms"] == []
    await ws.close()


async def test_clap_message_reaches_handlers(hub):
    got = []

    async def handler(dev, count):
        got.append((dev.id, count))
    hub.offline.on_clap.append(handler)
    ws = await speaker(hub)
    await ws.send(json.dumps({"type": "clap", "count": 2}))
    async with asyncio.timeout(1):
        while not got:
            await asyncio.sleep(0.01)
    assert got == [("jarvis-hub", 2)] and hub.offline.claps == 1
    await ws.close()


async def test_clap_console_and_persistence(hub, brain):
    ws = await speaker(hub)
    await recv(ws), await recv(ws)
    reply = await execute(hub, "clap off")
    assert "выкл" in reply
    assert await recv(ws) == {"type": "config", "clap": 0, "clap_sens": 5}
    await execute(hub, "clap on 8")
    assert await recv(ws) == {"type": "config", "clap": 1, "clap_sens": 8}
    assert brain.pref("clap_on") == "1" and brain.pref("clap_sens") == "8"
    assert "1…10" in await execute(hub, "clap on 99")
    assert "clap on|off" in await execute(hub, "clap")
    await execute(hub, "plan")
    assert (await recv(ws))["type"] == "plan"
    await ws.close()
    # после перезапуска настройки берутся из памяти
    again = Offline(DeviceHub(Settings(device_token=TOKEN)), brain, clap_on=True, clap_sens=5)
    assert again.clap_on and again.clap_sens == 8
