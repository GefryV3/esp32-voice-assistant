from types import SimpleNamespace

import pytest

from jarvis import config
from jarvis.brain import Brain
from jarvis.presence import Presence

T0 = 1_760_800_000.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t

    def advance(self, minutes):
        self.t += minutes * 60


class FakeBridge:
    def __init__(self):
        self.pc = None
        self.phone = None
        self.phone_state = {}
        self.is_night = False

    def night(self):
        return self.is_night


class Ent(SimpleNamespace):
    @property
    def domain(self):
        return self.entity_id.partition(".")[0]


class FakeHA:
    def __init__(self, *states, fail=False):
        self.states, self.fail = list(states), fail

    async def entities(self, fresh=False):
        if self.fail:
            raise RuntimeError("нет связи")
        return [Ent(entity_id=e, state=s) for e, s in self.states]


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    bridge = FakeBridge()
    brain = Brain(tmp_path / "b.db")
    p = Presence("auto", bridge=bridge, brain=brain, clock=clock, idle_min=180)
    return p, bridge, brain, clock


def pc_online(bridge, idle):
    bridge.pc = SimpleNamespace(state={"idle": idle})


def phone_online(bridge, **state):
    bridge.phone = object()
    bridge.phone_state = state


def test_no_data_means_here(env):
    p, *_ = env
    assert p.here()


def test_pc_active_is_here_and_idle_pc_alone_is_not_enough(env):
    p, bridge, _, clock = env
    pc_online(bridge, 30)
    assert p.here() and p.reason == "компьютер активен"
    pc_online(bridge, 3600)
    clock.advance(1)
    assert p.here()                      # часа без признаков ещё мало (порог 180 мин)
    clock.advance(200)
    assert not p.here() and "нет признаков" in p.reason


def test_phone_in_hand_keeps_here(env):
    p, bridge, _, clock = env
    phone_online(bridge, screen=False, unlocked_at=0)
    clock.advance(200)
    assert not p.here()
    phone_online(bridge, screen=True)
    clock.advance(1)
    assert p.here()
    phone_online(bridge, screen=False, unlocked_at=int((clock() - 60) * 1000))
    clock.advance(0.1)
    assert p.here()


async def test_ha_away_wins_over_phone_but_not_over_pc(env):
    p, bridge, _, clock = env
    p.ha = FakeHA(("person.mag", "not_home"))
    await p.refresh()
    phone_online(bridge, screen=True)
    assert not p.here() and "HA" in p.reason
    pc_online(bridge, 5)
    clock.advance(0.5)
    assert p.here()


async def test_ha_home_and_device_tracker_fallback(env):
    p, *_ = env
    p.ha = FakeHA(("device_tracker.phone", "home"))
    await p.refresh()
    assert p._ha_home() is True
    p.ha = FakeHA(("person.a", "not_home"), ("person.b", "home"))
    await p.refresh()
    assert p._ha_home() is True          # хоть кто-то дома


async def test_ha_failure_means_unknown_and_stale_is_ignored(env):
    p, _, _, clock = env
    p.ha = FakeHA(("person.a", "not_home"))
    await p.refresh()
    assert not p.here()
    clock.advance(11)                    # ответ HA устарел
    assert p.here()
    p.ha = FakeHA(fail=True)
    await p.refresh()
    assert p._ha_home() is None and p.here()


async def test_night_at_bedside_stays_here(env):
    p, bridge, _, clock = env
    bridge.is_night = True
    phone_online(bridge, screen=False, unlocked_at=0)
    clock.advance(400)                   # давно ничего не трогали — но это сон
    assert p.here() and "ночь" in p.reason
    p.ha = FakeHA(("person.mag", "home"))
    await p.refresh()
    clock.advance(1)
    assert p.here()
    p.ha = FakeHA(("person.mag", "not_home"))
    await p.refresh()
    assert not p.here()                  # ночью, но не дома


def test_override_is_stored_and_wins(env, tmp_path):
    p, bridge, brain, clock = env
    pc_online(bridge, 1)
    p.set_override("away")
    assert not p.here() and "вручную" in p.reason
    assert Presence("auto", brain=Brain(tmp_path / "b.db"), clock=clock).override == "away"   # пережило перезапуск
    p.set_override("here")
    pc_online(bridge, 99999)
    clock.advance(500)
    assert p.here()
    p.set_override("auto")
    assert not p.here()
    with pytest.raises(ValueError):
        p.set_override("maybe")


def test_off_always_here(env):
    _, bridge, brain, clock = env
    off = Presence("off", bridge=bridge, brain=brain, clock=clock)
    brain.set_pref("presence", "away")
    assert off.here()


async def test_command(env):
    p, *_ = env
    assert await p.command("ask что-то") is None
    out = await p.command("presence away")
    assert "НЕТ на месте" in out and "away" in out
    assert "на месте" in await p.command("presence here")
    assert "auto|here|away" in await p.command("presence bogus")
    assert p.override == "here"


def test_log_throttle(env):
    p, _, _, clock = env
    assert p.should_log() and not p.should_log()
    clock.advance(1.1)
    assert p.should_log()


def test_cache_does_not_recompute_every_frame(env):
    p, *_ = env
    calls = []
    orig = p._decide
    p._decide = lambda: calls.append(1) or orig()
    for _ in range(100):
        p.here()
    assert len(calls) == 1


async def test_brain_set_presence(env):
    p, _, brain, _ = env

    async def send(m):
        pass

    assert (await brain.call("set_presence", {"mode": "away"}, send))["ok"] is False   # выключено
    brain.presence = p
    assert (await brain.call("set_presence", {"mode": "away"}, send))["mode"] == "away"
    assert p.override == "away"
    assert (await brain.call("set_presence", {"mode": "ушёл"}, send))["ok"] is True
    assert (await brain.call("set_presence", {"mode": "x"}, send))["ok"] is False
    assert any(t["name"] == "set_presence" for t in brain.tools())


def test_settings():
    s = config.load_settings(environ={"JARVIS_DEVICE_TOKEN": "t"}, env_file="nonexistent.env")
    assert (s.presence, s.presence_pc_min, s.presence_idle_min) == ("auto", 10, 180)
    s = config.load_settings(environ={"JARVIS_DEVICE_TOKEN": "t", "JARVIS_PRESENCE": "OFF",
                                      "JARVIS_PRESENCE_IDLE_MIN": "0"}, env_file="nonexistent.env")
    assert s.presence == "off" and s.presence_idle_min == 0
    with pytest.raises(config.ConfigError):
        config.load_settings(environ={"JARVIS_DEVICE_TOKEN": "t", "JARVIS_PRESENCE": "x"}, env_file="n.env")
    with pytest.raises(config.ConfigError):
        config.load_settings(environ={"JARVIS_DEVICE_TOKEN": "t", "JARVIS_PRESENCE_PC_MIN": "x"}, env_file="n.env")


# ── сигнал колонки room ──

async def room(p, here, src="ble", rssi=-60):
    await p.on_room(None, {"type": "room", "here": here, "rssi": rssi, "src": src})


async def test_room_here_beats_ha_away(env):
    p, _, _, clock = env
    p.ha = FakeHA(("person.mag", "not_home"))
    await p.refresh()
    assert not p.here()
    await room(p, True)
    assert p.here() and "ble" in p.reason


async def test_room_away_needs_delay(env):
    p, bridge, _, clock = env
    phone_online(bridge, screen=True)
    pc_online(bridge, 5)
    await room(p, False)
    assert p.here()                       # ещё не 5 минут
    clock.advance(4)
    await room(p, False)                  # повтор раз в 30 с не сбрасывает отсчёт
    assert p.here()
    clock.advance(1.1)
    await room(p, False)
    assert not p.here() and "колонка не видит" in p.reason   # сильнее активного компьютера


async def test_room_returns_clears_away(env):
    p, *_, clock = env
    await room(p, False)
    clock.advance(6)
    await room(p, False)
    assert not p.here()
    await room(p, True)
    assert p.here()
    await room(p, False)
    clock.advance(1)
    assert p.here()                       # отсчёт начался заново


async def test_room_stale_or_none_is_ignored(env):
    p, *_, clock = env
    await room(p, False)
    clock.advance(6)
    await room(p, False)
    assert not p.here()
    clock.advance(3)                      # колонка замолчала дольше 2 минут
    assert p.here()
    await room(p, False, src="none")
    clock.advance(10)
    await room(p, False, src="none")
    assert p.here()


async def test_manual_override_beats_room(env):
    p, *_, clock = env
    p.set_override("here")
    await room(p, False)
    clock.advance(6)
    await room(p, False)
    assert p.here() and "вручную" in p.reason
    p.set_override("away")
    await room(p, True)
    assert not p.here()


async def test_night_charging_phone_at_home_ignores_room_away(env):
    p, bridge, _, clock = env
    bridge.is_night = True
    phone_online(bridge, screen=False, charging=True)
    await room(p, False)
    clock.advance(6)
    await room(p, False)
    assert p.here() and "ночь" in p.reason
    p.ha = FakeHA(("person.mag", "not_home"))        # по HA нет дома — тогда отключаем
    await p.refresh()
    clock.advance(0.1)
    assert not p.here()
    p.ha = FakeHA(("person.mag", "home"))
    await p.refresh()
    phone_online(bridge, screen=False, charging=False)  # телефон не на зарядке — не оправдание
    clock.advance(0.1)
    assert not p.here()


async def test_room_and_ble_commands(env):
    p, *_ = env
    p.ble_uuid = "01234567-89ab-cdef-0123-456789abcdef"
    assert "нет данных" in await p.command("room")
    await room(p, True, rssi=-58)
    out = await p.command("room")
    assert "ble" in out and "-58" in out
    sent = []

    class Dev:
        async def send(self, msg):
            sent.append(msg)

    p.hub = SimpleNamespace(get=lambda: Dev())
    assert "отправлено" in await p.command("ble on")
    assert sent == [{"type": "ble_scan", "on": 1, "uuid": "0123456789abcdef0123456789abcdef"}] and p.ble_scan
    sent.clear()
    await p.on_connect(Dev())
    assert sent == [{"type": "ble_scan", "on": 1, "uuid": "0123456789abcdef0123456789abcdef"}]
    await p.command("ble off")
    sent.clear()
    await p.on_connect(Dev())
    assert sent == []


# ── диагностика памяти, настройка скана и калибровка BLE ──

class Dev:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


async def test_room_shows_heap(env):
    p, *_ = env
    await p.on_room(None, {"type": "room", "here": True, "rssi": -60, "src": "ble",
                           "heap_free": 71680, "heap_min": 51200})
    out = await p.command("room")
    assert "70 КБ" in out and "минимум 50 КБ" in out
    await p.on_room(None, {"type": "room", "here": True, "src": "ble", "heap_free": "много"})
    assert "КБ" not in await p.command("room")        # мусор в поле не ломает и не показывается


async def test_ble_tune_is_saved_and_sent(env):
    p, *_ = env
    p.ble_uuid = "0123456789abcdef0123456789abcdef"
    dev = Dev()
    p.hub = SimpleNamespace(get=lambda: dev)
    assert "по умолчанию" in await p.command("ble tune")
    assert "ключи" in await p.command("ble tune foo=1")
    await p.command("ble on")
    dev.sent.clear()
    out = await p.command("ble tune win_ms=60 every_s=3 night_every_s=20")
    assert "win_ms=60" in out and "отправлено" in out
    assert dev.sent == [{"type": "ble_scan", "on": 1, "uuid": p.ble_uuid,
                         "win_ms": 60, "every_s": 3, "night_every_s": 20}]
    dev.sent.clear()
    await p.on_connect(dev)
    assert dev.sent[0]["win_ms"] == 60
    await p.command("ble tune reset")
    dev.sent.clear()
    await p.on_connect(dev)
    assert dev.sent == [{"type": "ble_scan", "on": 1, "uuid": p.ble_uuid}]


async def test_ble_tune_rejects_out_of_range(env):
    p, *_ = env
    out = await p.command("ble tune win_ms=9999")
    assert out.startswith("ble tune:") and "win_ms" in out
    assert "по умолчанию" in await p.command("ble tune")      # неверное не сохранилось


async def test_room_calibrate_suggests_and_applies(env):
    p, *_ = env
    p.ble_uuid = "0123456789abcdef0123456789abcdef"
    dev = Dev()
    p.hub = SimpleNamespace(get=lambda: dev)
    rssi = [-60, -62, -61, -65, -59, -63, -70, -58, -64, -61]

    async def fake_sleep(sec):
        assert sec >= 30
        for r in rssi:
            await p.on_room(dev, {"type": "room", "here": True, "src": "ble", "raw": r, "heard": 1})
        await p.on_room(dev, {"type": "room", "here": True, "src": "ble"})   # цикл без пакета

    p.sleep = fake_sleep
    out = await p.command("room calibrate")
    assert dev.sent[0]["cal_s"] == 30 and dev.sent[0]["on"] == 1
    assert dev.sent[-1]["on"] == 0                      # BLE был выключен — выключили обратно
    assert "рядом > -69 дБм" in out and "ушёл < -76 дБм" in out and "из 11" in out
    dev.sent.clear()
    assert "применены" in await p.command("room calibrate apply")
    assert dev.sent == [{"type": "config", "ble_near": -69, "ble_far": -76}]
    dev.sent.clear()
    await p.on_connect(dev)                              # пороги переживают переподключение
    assert dev.sent == [{"type": "config", "ble_near": -69, "ble_far": -76}]


async def test_room_calibrate_too_few_packets(env):
    p, *_ = env
    p.ble_uuid = "0123456789abcdef0123456789abcdef"
    dev = Dev()
    p.hub = SimpleNamespace(get=lambda: dev)

    async def fake_sleep(sec):
        await p.on_room(dev, {"type": "room", "here": False, "src": "ble"})

    p.sleep = fake_sleep
    assert "мало" in await p.command("room calibrate")
    assert "сначала" in await p.command("room calibrate apply")


async def test_room_calibrate_needs_speaker_and_uuid(env):
    p, *_ = env
    assert "не подключена" in await p.command("room calibrate")
    p.hub = SimpleNamespace(get=lambda: Dev())
    assert "UUID" in await p.command("room calibrate")
