import copy
from datetime import datetime, timedelta, timezone

import pytest

from jarvis.brain import Brain
from jarvis.lights import Lights, clean_palette
from jarvis.lightstate import LightState

AREA = "spalnia"
WLED = {"light.podsvetka_stola": "192.168.178.119", "light.palka": "192.168.178.88"}
COLORS = {"192.168.178.119": [[255, 0, 80], [0, 0, 0], [0, 0, 0]], "192.168.178.88": [[0, 200, 40], [0, 0, 0], [0, 0, 0]]}
DESK, PALKA = "192.168.178.119", "192.168.178.88"


class FakeHA:
    """Минимальный HA: состояния, реестры, call_service. `stubborn` игнорирует transition-выключение."""

    def __init__(self):
        self.states = {}
        self.stubborn = set()
        self.calls = []
        self.script_on = False
        self.last_triggered = None
        reg, devs = [], []
        for eid, host in WLED.items():
            did = eid.replace("light.", "d_")
            devs.append({"id": did, "area_id": AREA, "manufacturer": "WLED", "configuration_url": f"http://{host}"})
            reg.append({"entity_id": eid, "device_id": did, "platform": "wled"})
            self.states[eid] = {"entity_id": eid, "state": "on", "attributes": {"friendly_name": eid, "brightness": 200}}
        reg.append({"entity_id": "light.dimmer", "area_id": AREA, "platform": "esphome"})
        self.states["light.dimmer"] = {"entity_id": "light.dimmer", "state": "on",
                                       "attributes": {"friendly_name": "Лианы", "brightness": 120}}
        reg.append({"entity_id": "light.hall", "area_id": "hall", "platform": "esphome"})
        self.states["light.hall"] = {"entity_id": "light.hall", "state": "on", "attributes": {"friendly_name": "Холл"}}
        self.states["light.spalnia"] = {"entity_id": "light.spalnia", "state": "on", "attributes": {
            "friendly_name": "Спальня", "entity_id": [*WLED, "light.dimmer"]}}
        self.reg, self.devs = reg, devs

    async def request(self, type_, **f):
        if type_ == "get_states":
            out = copy.deepcopy(list(self.states.values()))
            out.append({"entity_id": "script.sunrise", "state": "on" if self.script_on else "off", "attributes": {}})
            out.append({"entity_id": "automation.rassvet", "state": "on", "attributes": {
                "id": "1756302078808", "last_triggered": self.last_triggered}})
            return out
        if type_ == "config/entity_registry/list":
            return self.reg
        if type_ == "config/device_registry/list":
            return self.devs
        if type_ == "call_service":
            self.calls.append(f)
            for eid in f["target"]["entity_id"]:
                st = self.states[eid]
                if f["service"] == "turn_off":
                    if eid in self.stubborn and "transition" in f["service_data"]:
                        continue
                    st["state"] = "off"
                else:
                    st["state"] = "on"
                    if "brightness" in f["service_data"]:
                        st["attributes"]["brightness"] = f["service_data"]["brightness"]
            return None
        raise AssertionError(type_)


class FakeWled:
    def __init__(self, ha):
        self.ha = ha
        self.st = {h: {"on": True, "bri": 200, "seg": [{"id": 0, "col": c, "fx": 9, "sx": 100, "ix": 50, "pal": 3}]}
                   for h, c in COLORS.items()}
        self.posts = []

    async def __call__(self, method, url, body=None):
        host = url.split("/")[2]
        if url.endswith("/json/effects"):
            return ["Solid", "Blink", "Palette"]
        if url.endswith("/json/palettes"):
            return ["Default", "r", "a", "b", "c", "* Colors Only"]
        if method == "GET":
            return copy.deepcopy(self.st[host])
        self.posts.append((host, body))
        st = self.st[host]
        for k in ("on", "bri"):
            if k in body:
                st[k] = body[k]
        for seg in body.get("seg", []):
            st["seg"][seg["id"]].update(seg)
        eid = next(e for e, h in WLED.items() if h == host)
        self.ha.states[eid]["state"] = "on" if st["on"] else "off"
        return {"success": True}


class Dev:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class Hub:
    def __init__(self):
        self.dev = Dev()
        self.handlers = []

    def get(self):
        return self.dev

    def on_message(self, h):
        self.handlers.append(h)


class Voice:
    def __init__(self):
        self.prompts = []
        self._talks = {}

    async def announce(self, prompt):
        self.prompts.append(prompt)
        return True


class Bridge:
    def __init__(self, night=False, alarm=0):
        self._night = night
        self.phone_state = {"next_alarm": alarm, "charging": True, "battery": 80}

    def night(self):
        return self._night


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    ha, clock = FakeHA(), Clock()
    http = FakeWled(ha)
    state = LightState(ha, path=tmp_path / "ls.json", http=http, clock=clock)
    hub, voice = Hub(), Voice()

    async def sleep(s):
        clock.t += s

    brain = Brain(tmp_path / "b.db", ha=ha)
    bridge = Bridge(alarm=int(datetime(2026, 10, 21, 7, 10).timestamp() * 1000))
    lights = Lights(hub, ha, state, bridge=bridge, voice=voice, brain=brain, sleep=sleep, clock=clock)
    brain.lights = lights
    return ha, http, state, hub, voice, lights, bridge, clock, brain


async def test_lamps_found_without_groups_and_other_rooms(env):
    ha, _, state, *_ = env
    lamps = await state.lamps()
    assert sorted(l.entity_id for l in lamps) == ["light.dimmer", "light.palka", "light.podsvetka_stola"]
    assert {l.host for l in lamps if l.is_wled} == set(WLED.values())


async def test_goodnight_fades_everything_then_summary_and_screen(env):
    ha, http, state, hub, voice, lights, *_ = env
    ha.stubborn.add("light.dimmer")                       # HA-transition у диммера не сработал
    out = await lights.goodnight()
    assert out["ok"]
    await lights._goodnight
    assert all(ha.states[e]["state"] == "off" for e in ("light.podsvetka_stola", "light.palka", "light.dimmer"))
    assert ha.states["light.hall"]["state"] == "on"       # чужая комната не тронута
    off_calls = [c for c in ha.calls if c["service"] == "turn_off"]
    assert off_calls[0]["service_data"]["transition"] == 30
    assert any(h == DESK and b == {"on": False, "transition": 300} for h, b in http.posts)
    assert any("transition" not in c["service_data"] for c in off_calls)       # добили без плавности
    assert len(voice.prompts) == 1 and "07:10" in voice.prompts[0]
    kinds = [(m["type"], m.get("level", m.get("on"))) for m in hub.dev.sent]
    assert kinds[0] == ("brightness", 200) and ("night", True) in kinds and kinds[-1] == ("brightness", 255)
    assert state.views["light.palka"]["seg"][0]["col"][0] == [0, 200, 40]       # вид запомнен до гашения


async def test_goodnight_prompt_mentions_calendar_tool(env):
    *_, lights, bridge, clock, brain = env
    brain.google = object()
    assert "google_calendar_list" in lights.summary_prompt()
    bridge.phone_state["next_alarm"] = 0
    assert "не заведён" in lights.summary_prompt()


async def test_dawn_restores_colors_in_off_state(env):
    ha, http, state, *_, clock, brain = env
    assert (await state.tick()).startswith("captured 3")
    clock.t += 100
    # рассвет: автоматизация красит всё в белый и потом гасит
    ha.script_on = True
    for host in COLORS:
        http.st[host]["seg"][0].update(col=[[255, 255, 255], [0, 0, 0], [0, 0, 0]], fx=0, pal=0)
    assert await state.tick() == "dawn"
    for eid in ("light.podsvetka_stola", "light.palka", "light.dimmer"):
        ha.states[eid]["state"] = "off"
    for host in COLORS:
        http.st[host]["on"] = False
    clock.t += 100
    assert await state.tick() == "dawn"          # скрипт ещё идёт: белый не запомнен
    assert state.views["light.palka"]["seg"][0]["col"][0] == [0, 200, 40]
    ha.script_on = False
    assert await state.tick() == "restored"
    assert http.st[PALKA]["on"] is False
    assert http.st[PALKA]["seg"][0]["col"][0] == [0, 200, 40] and http.st[PALKA]["seg"][0]["fx"] == 9
    assert http.st[DESK]["seg"][0]["col"][0] == [255, 0, 80]
    assert ha.states["light.palka"]["state"] == "off"


async def test_dawn_detected_by_automation_last_triggered(env):
    ha, _, state, *_ = env
    ha.last_triggered = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    assert await state.tick() == "dawn"


async def test_views_survive_restart(env, tmp_path):
    ha, http, state, *_ = env
    await state.capture()
    again = LightState(ha, path=tmp_path / "ls.json", http=http)
    assert again.views["light.palka"]["seg"][0]["pal"] == 3


async def test_clap_two_turns_everything_off_then_on_with_colors(env):
    ha, http, state, hub, voice, lights, bridge, clock, brain = env
    assert "игнор" in await lights.clap(1) and "игнор" in await lights.clap(3)
    assert (await lights.clap(2)).startswith("погасил")
    assert all(ha.states[e]["state"] == "off" for e in ("light.podsvetka_stola", "light.palka", "light.dimmer"))
    lights._last_clap = clock.t
    assert await lights.clap(2) == "слишком часто"
    clock.t += 10
    for host in COLORS:                                   # лампу перекрасили, пока она была выключена
        http.st[host]["seg"][0].update(col=[[1, 1, 1]] * 3)
    ha.states["light.dimmer"]["attributes"]["brightness"] = 5
    assert (await lights.clap(2)).startswith("включил")
    assert ha.states["light.palka"]["state"] == "on"
    assert http.st[PALKA]["seg"][0]["col"][0] == [0, 200, 40]
    assert ha.states["light.dimmer"]["attributes"]["brightness"] == 120


async def test_clap_at_night_is_warm_and_dim_and_not_remembered(env):
    ha, http, state, hub, voice, lights, bridge, clock, brain = env
    await lights.clap(2)
    clock.t += 10
    bridge._night = True
    ha.calls.clear()
    assert "ночь" in await lights.clap(2)
    on = ha.calls[-1]
    assert on["service"] == "turn_on" and on["service_data"]["color_temp_kelvin"] == 2200
    assert on["service_data"]["brightness_pct"] == 15
    assert state.held                                      # приглушённый вид не станет «любимым»
    before = dict(state.views)
    await state.capture()
    assert state.views == before


async def test_clap_message_from_speaker(env):
    ha, http, state, hub, voice, lights, *_ = env
    await hub.handlers[0](hub.dev, {"type": "clap", "count": 2})
    assert ha.states["light.palka"]["state"] == "off"


def test_clean_palette():
    assert clean_palette(["#ff0000", "00ff00", "bad", "#ff0000", "#000000", "#101010", "#0000ff"]) == [
        [255, 0, 0], [0, 255, 0], [160, 160, 160]]
    assert clean_palette(None) == [] and clean_palette("x") == []


async def test_music_light_paints_desk_only_and_restores(env):
    ha, http, state, hub, voice, lights, bridge, clock, brain = env
    await lights.music_light("on")
    assert brain.pref("music_light") == "1"
    await lights.on_media({"type": "media", "state": "playing", "palette": ["#ff0000", "#0000ff"]})
    desk = [b for h, b in http.posts if h == DESK][-1]
    assert desk["on"] and desk["seg"][0]["fx"] == 2 and desk["seg"][0]["pal"] == 5      # Palette / Colors Only
    assert desk["seg"][0]["col"] == [[255, 0, 0], [0, 0, 255], [255, 0, 0]] and desk["transition"] == 20
    assert not [h for h, _ in http.posts if h == PALKA]                                  # Палку не трогаем
    await lights.on_media({"type": "media", "state": "playing", "palette": ["#00ff00"]})   # смена трека
    assert http.st[DESK]["seg"][0]["col"][0] == [0, 255, 0]
    await lights.on_media({"type": "media", "state": "paused"})
    assert lights._music_stop is not None
    await lights._music_stop
    seg = http.st[DESK]["seg"][0]
    assert seg["col"][0] == [255, 0, 80] and seg["fx"] == 9 and http.st[DESK]["on"] is True


async def test_music_light_restores_off_lamp_and_respects_night_and_switch(env):
    ha, http, state, hub, voice, lights, bridge, clock, brain = env
    await lights.on_media({"state": "playing", "palette": ["#ff0000"]})
    assert not http.posts                                  # выключено голосом — не красим
    await lights.music_light("on")
    bridge._night = True
    await lights.on_media({"state": "playing", "palette": ["#ff0000"]})
    assert not http.posts                                  # ночью не включаем
    bridge._night = False
    await state.capture(force=True)
    http.st[DESK]["on"] = False
    ha.states["light.podsvetka_stola"]["state"] = "off"
    await lights.on_media({"state": "playing", "palette": ["#ff0000"]})
    assert http.st[DESK]["on"] is True
    out = await lights.music_light("off")
    assert out["music_light"] is False
    assert http.st[DESK]["on"] is False and http.st[DESK]["seg"][0]["col"][0] == [255, 0, 80]


async def test_brain_and_console_routing(env):
    ha, http, state, hub, voice, lights, bridge, clock, brain = env
    assert {"goodnight", "music_light"} <= {t["name"] for t in brain.tools()}

    async def send(m):
        pass
    assert (await brain.call("music_light", {"action": "status"}, send))["ok"]
    assert (await brain.call("goodnight", {}, send))["ok"]
    await lights._goodnight
    assert await lights.command("hello") is None
    assert "light.palka" in await lights.command("lights")
    assert (await lights.command("music_light on")).startswith("{'ok': True")
