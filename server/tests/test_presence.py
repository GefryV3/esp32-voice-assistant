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
