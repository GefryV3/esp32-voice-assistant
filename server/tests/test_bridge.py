import asyncio
import json
from datetime import datetime

import pytest
from websockets.asyncio.client import connect

from jarvis.bridge import Bridge, SettingsStore, in_window
from jarvis.config import Settings
from jarvis.devices import DeviceHub

TOKEN = "secret-token"
DAY = datetime(2026, 10, 18, 15, 0)   # днём — не ночь
NIGHT = datetime(2026, 10, 18, 1, 30)


@pytest.fixture
async def env(tmp_path):
    hub = DeviceHub(Settings(device_token=TOKEN, host="127.0.0.1", port=0), hello_timeout=0.3)
    now = {"t": DAY}
    bridge = Bridge(hub, Settings(device_token=TOKEN), SettingsStore(tmp_path / "settings.json"),
                    clock=lambda: 1_760_800_000.0, local_now=lambda: now["t"])
    await hub.start()
    yield hub, bridge, now
    await bridge.stop()
    await hub.stop()


def hello(device, caps):
    return json.dumps({"type": "hello", "proto": 1, "token": TOKEN, "device": device, "fw": "0.1.0", "caps": caps})


async def wait_for(cond, timeout=1.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def recv_type(ws, type_, timeout=1.0):
    async with asyncio.timeout(timeout):
        while True:
            msg = json.loads(await ws.recv())
            if msg["type"] == type_:
                return msg


def test_settings_are_cleaned_and_saved(tmp_path):
    store = SettingsStore(tmp_path / "s.json")
    store.update({"sleep_after_min": 999, "sleep_clock": "left", "night_start": "25:00",
                  "mirror": "yes", "unknown": 1, "sarcasm": 30})
    assert store.values["sleep_after_min"] == 60        # зажато в диапазон
    assert store.values["sleep_clock"] == "top"         # кривое — не принято
    assert store.values["night_start"] == "23:30"
    assert store.values["mirror"] is True
    assert "unknown" not in store.values
    assert SettingsStore(tmp_path / "s.json").values["sarcasm"] == 30   # пережило перезапуск


@pytest.mark.parametrize("now, start, end, inside", [
    (30, "23:30", "07:00", True),     # 00:30 — через полночь
    (23 * 60 + 40, "23:30", "07:00", True),
    (12 * 60, "23:30", "07:00", False),
    (10 * 60, "09:00", "18:00", True),
])
def test_in_window(now, start, end, inside):
    assert in_window(now, start, end) is inside


async def test_phone_gets_settings_and_notification_reaches_speaker(env):
    hub, bridge, _ = env
    base = f"ws://127.0.0.1:{hub.port}"
    async with connect(base + "/device") as speaker, connect(base + "/phone") as phone, connect(base + "/pc") as pc:
        await speaker.send(hello("jarvis-hub", ["display"]))
        assert (await recv_type(speaker, "config"))["sleep_after"] == 180
        await phone.send(hello("phone", ["phone"]))
        settings = await recv_type(phone, "settings")
        assert settings["values"]["mirror"] is True
        await pc.send(hello("pc", ["pc"]))
        await recv_type(pc, "welcome")
        await wait_for(lambda: bridge.pc is not None)

        # я за компьютером, телефон погашен — уведомление идёт на колонку
        await pc.send(json.dumps({"type": "pc", "idle": 5, "battery": 80, "charging": True}))
        await phone.send(json.dumps({"type": "state", "screen": False, "battery": 70, "charging": False}))
        await phone.send(json.dumps({"type": "notif", "key": "k1", "app_name": "Telegram", "title": "Мама",
                                     "text": "Ужин готов", "can_reply": True}))
        card = await recv_type(speaker, "notify")
        assert (card["kind"], card["title"], card["app"]) == ("msg", "Мама", "Telegram")

        # «ответь» — уходит телефону на последнее уведомление с кнопкой ответа
        out = await bridge.command("reply буду через 10 минут")
        assert "Telegram" in out
        assert (await recv_type(phone, "reply"))["key"] == "k1"

        # ссылка с телефона открывается на компьютере
        await phone.send(json.dumps({"type": "share", "text": "https://example.org"}))
        assert (await recv_type(pc, "open_url"))["url"] == "https://example.org"

        # настройки из приложения доходят до колонки
        await phone.send(json.dumps({"type": "settings", "values": {"sleep_after_min": 10, "brightness": 50}}))
        assert (await recv_type(speaker, "config"))["sleep_after"] == 600
        assert (await recv_type(speaker, "brightness"))["level"] == 128


async def test_mirror_rules(env):
    _, bridge, now = env
    bridge.phone_state = {"screen": False}
    assert bridge.should_mirror() is False          # компьютер не на связи, а нужно «только за компьютером»
    bridge.store.update({"mirror_only_pc": False})
    assert bridge.should_mirror() is True
    bridge.phone_state["screen"] = True
    assert bridge.should_mirror() is False          # телефон в руках
    bridge.phone_state["screen"] = False
    now["t"] = NIGHT
    assert bridge.should_mirror() is False          # ночью — тишина


def test_bedtime_summary(env):
    _, bridge, _ = env
    alarm = datetime(2026, 10, 19, 7, 30).timestamp() * 1000
    bridge.phone_state = {"next_alarm": alarm, "battery": 40, "charging": True}
    bridge.bt = [{"name": "Nothing Ear (a)", "level": 55}]
    title, text = bridge.bedtime_summary()
    assert title == "Перед сном"
    assert text == "будильник 07:30 · телефон 40 %, заряд · наушники 55 %"


async def test_night_remark_only_late_and_once_per_night(env):
    _, bridge, now = env
    sent = []

    async def speaker(msg):
        sent.append(msg)
    bridge.speaker = speaker
    bridge.phone_state = {"next_alarm": (1_760_800_000.0 + 5 * 3600) * 1000}
    for t in (datetime(2026, 10, 18, 23, 45), datetime(2026, 10, 19, 0, 40)):   # ночь, но ещё не поздно
        now["t"] = t
        await bridge._maybe_remark()
    assert sent == []
    now["t"] = datetime(2026, 10, 19, 3, 0)                   # реально сидит в 3 ночи
    await bridge._maybe_remark()
    assert len(sent) == 1 and sent[0]["kind"] == "remark"
    now["t"] = datetime(2026, 10, 19, 3, 40)                   # через сорок минут — не повторяем
    await bridge._maybe_remark()
    assert len(sent) == 1
    now["t"] = datetime(2026, 10, 20, 2, 0)                    # следующая ночь — можно снова
    await bridge._maybe_remark()
    assert len(sent) == 2
