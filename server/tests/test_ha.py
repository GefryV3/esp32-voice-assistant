import json

import pytest

from jarvis.brain import Brain
from jarvis.ha import HAError, HomeAssistant

STATES = [
    {"entity_id": "light.bed_left", "state": "off", "attributes": {"friendly_name": "Ночник слева"}},
    {"entity_id": "light.bed_right", "state": "on", "attributes": {"friendly_name": "Ночник справа"}},
    {"entity_id": "light.hall", "state": "off", "attributes": {"friendly_name": "Прихожая"}},
    {"entity_id": "sensor.temp", "state": "21.5", "attributes": {
        "friendly_name": "Температура", "unit_of_measurement": "°C"}},
    {"entity_id": "light.ghost", "state": "off", "attributes": {"friendly_name": "Призрак"}},
    {"entity_id": "automation.x", "state": "on", "attributes": {"friendly_name": "Авто"}},
]
REG = [
    {"entity_id": "light.bed_left", "area_id": "bed"}, {"entity_id": "light.bed_right", "device_id": "d1"},
    {"entity_id": "light.hall", "area_id": "hall"}, {"entity_id": "sensor.temp", "area_id": "bed"},
    {"entity_id": "light.ghost", "hidden_by": "user"},
]


class FakeWs:
    def __init__(self, token_ok=True):
        self.token_ok, self.sent, self.out = token_ok, [], ["auth_required"]
        self.closed = False

    async def recv(self):
        return json.dumps({"type": self.out.pop(0)}) if self.out and isinstance(self.out[0], str) \
            else json.dumps(self.out.pop(0))

    async def send(self, raw):
        m = json.loads(raw)
        self.sent.append(m)
        if m["type"] == "auth":
            self.out.append("auth_ok" if self.token_ok else "auth_invalid")
            return
        result = {"get_states": STATES, "config/entity_registry/list": REG,
                  "config/device_registry/list": [{"id": "d1", "area_id": "bed"}],
                  "config/area_registry/list": [{"area_id": "bed", "name": "Спальня"},
                                                {"area_id": "hall", "name": "Коридор"}],
                  "call_service": []}[m["type"]]
        self.out.append({"id": m["id"], "type": "result", "success": True, "result": result})

    async def close(self):
        self.closed = True


@pytest.fixture
def ws():
    return FakeWs()


@pytest.fixture
def ha(ws):
    async def connect(url, **kw):
        return ws
    return HomeAssistant("ws://x", "tok", connect=connect)


def services(ws):
    return [(m["service_data"], m["service"], m["target"]["entity_id"])
            for m in ws.sent if m["type"] == "call_service"]


async def test_room_turns_on_all_lights_of_room(ha, ws):
    out = await ha.call("home_set", {"name": "свет в спальне", "action": "on", "brightness_percent": 30})
    assert out["ok"] and sorted(out["devices"]) == ["Ночник слева", "Ночник справа"]
    assert {s[2] for s in services(ws)} == {"light.bed_left", "light.bed_right"}
    assert all(s[0] == {"brightness_pct": 30} and s[1] == "turn_on" for s in services(ws))


async def test_single_device_and_hidden_filtered(ha, ws):
    out = await ha.call("home_set", {"name": "прихожая", "action": "off"})
    assert out == {"ok": True, "devices": ["Прихожая"], "action": "off"}
    assert (await ha.call("home_set", {"name": "призрак", "action": "on"}))["ok"] is False


async def test_ambiguous_across_rooms(ha):
    out = await ha.call("home_set", {"name": "ночник", "action": "on"})
    assert out["ok"] is True      # оба ночника в одной комнате — это комната


async def test_state_and_list(ha):
    assert (await ha.call("home_state", {"name": "температура в спальне"}))["states"] == [
        "Температура (Спальня): 21.5 °C"]
    lights = (await ha.call("home_list", {"kind": "lights"}))["devices"]
    assert len(lights) == 3 and "Прихожая (Коридор): off" in lights


async def test_bad_token(ws):
    ws.token_ok = False

    async def connect(url, **kw):
        return ws
    bad = HomeAssistant("ws://x", "bad", connect=connect)
    out = await bad.call("home_list", {})
    assert out["ok"] is False and "токен" in out["error"]


async def test_brain_routes_home_tools(ha, tmp_path):
    brain = Brain(tmp_path / "b.db", ha=ha)
    assert {"home_set", "remember"} <= {t["name"] for t in brain.tools()}

    async def send(msg):
        pass
    assert (await brain.call("home_state", {"name": "прихожая"}, send))["ok"]
    assert "home_set" not in {t["name"] for t in Brain(tmp_path / "c.db").tools()}


async def test_color_and_effect(ha, ws):
    out = await ha.call("home_set", {"name": "прихожая", "action": "on", "color": "красный",
                                     "effect": "pulse", "transition_s": 2})
    assert out["ok"]
    data, service, _ = services(ws)[-1]
    assert service == "turn_on" and data["rgb_color"] == [255, 0, 0]
    assert data["effect"] == "pulse" and data["transition"] == 2 and data["brightness_pct"] == 100
    await ha.call("home_set", {"name": "прихожая", "action": "on", "color": "#00ff10"})
    assert services(ws)[-1][0]["rgb_color"] == [0, 255, 16]


async def test_generic_service_and_bad_json(ha, ws):
    out = await ha.call("home_service", {"name": "прихожая", "service": "light.toggle",
                                         "data": '{"transition": 1}'})
    assert out == {"ok": True, "devices": ["Прихожая"], "service": "toggle"}
    assert services(ws)[-1] == ({"transition": 1}, "toggle", "light.hall")
    assert (await ha.call("home_service", {"name": "прихожая", "service": "x", "data": "{"}))["ok"] is False


async def test_color_refused_for_dimmer_with_reason(ha, ws, monkeypatch):
    STATES[2]["attributes"]["supported_color_modes"] = ["brightness"]
    STATES[0]["attributes"]["supported_color_modes"] = ["rgb"]
    ha._cache = None
    out = await ha.call("home_set", {"name": "прихожая", "action": "on", "color": "синий"})
    assert out["ok"] is False and "brightness" in out["error"]
    assert not services(ws)
    brief = (await ha.call("home_list", {"kind": "lights"}))["devices"]
    assert any("Прихожая" in b and "только яркость" in b for b in brief)
    assert any("Ночник слева" in b and "умеет цвет" in b for b in brief)
    STATES[2]["attributes"].pop("supported_color_modes")
    STATES[0]["attributes"].pop("supported_color_modes")


async def test_exact_name_beats_longer_and_other_domains(ha, ws):
    STATES.extend([
        {"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Подсветка стола"}},
        {"entity_id": "number.desk_speed", "state": "5", "attributes": {"friendly_name": "Подсветка стола Скорость"}},
        {"entity_id": "scene.bed", "state": "unknown", "attributes": {"friendly_name": "Спальня"}},
        {"entity_id": "light.bed_group", "state": "on", "attributes": {"friendly_name": "Спальня"}},
    ])
    REG.extend([{"entity_id": e, "area_id": "bed"} for e in
                ("light.desk", "number.desk_speed", "scene.bed", "light.bed_group")])
    ha._cache = None
    try:
        out = await ha.call("home_set", {"name": "Подсветка стола", "action": "on", "color": "purple"})
        assert out == {"ok": True, "devices": ["Подсветка стола"], "action": "on"}
        assert services(ws)[-1][2] == "light.desk"
        out = await ha.call("home_set", {"name": "Спальня", "action": "on"})
        assert out["devices"] == ["Спальня"] and services(ws)[-1][2] == "light.bed_group"
    finally:
        del STATES[-4:]
        del REG[-4:]
