"""Умный дом через WebSocket API Home Assistant: найти устройство по имени/комнате, включить,
выключить, прочитать датчик.

В аддоне HA сам даёт адрес ws://supervisor/core/websocket и токен SUPERVISOR_TOKEN
(`homeassistant_api: true` в config.yaml). При разработке — JARVIS_HA_URL и JARVIS_HA_TOKEN
(долгоживущий токен). Соединение открывается по требованию и переоткрывается само.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as ws_connect

log = logging.getLogger(__name__)

SUPERVISOR_URL = "ws://supervisor/core/websocket"
CACHE_S = 60.0
TIMEOUT_S = 10.0
CONTROLLABLE = ("light", "switch", "fan", "cover", "input_boolean", "scene", "script", "climate",
                "media_player", "lock", "vacuum", "humidifier", "water_heater", "siren", "remote",
                "button", "input_button", "number", "input_number", "select", "input_select",
                "automation", "alarm_control_panel", "valve", "lawn_mower", "text", "input_text")
READABLE = CONTROLLABLE + ("sensor", "binary_sensor", "person", "weather", "sun", "calendar",
                           "device_tracker", "camera", "update", "zone")
KIND_DOMAINS = {"lights": ("light",), "switches": ("switch", "fan", "input_boolean"),
                "sensors": ("sensor", "binary_sensor", "weather", "sun"),
                "people": ("person", "device_tracker"),
                "covers": ("cover", "valve"), "climate": ("climate", "humidifier", "water_heater"),
                "media": ("media_player",), "locks": ("lock", "alarm_control_panel"),
                "scenes": ("scene", "script", "automation")}
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}
COLORS = {  # название → RGB; по-русски и по-английски
    "красный": (255, 0, 0), "red": (255, 0, 0), "оранжевый": (255, 120, 0), "orange": (255, 120, 0),
    "жёлтый": (255, 220, 0), "желтый": (255, 220, 0), "yellow": (255, 220, 0),
    "зелёный": (0, 220, 60), "зеленый": (0, 220, 60), "green": (0, 220, 60),
    "голубой": (0, 190, 255), "cyan": (0, 190, 255), "бирюзовый": (0, 220, 200),
    "синий": (0, 60, 255), "blue": (0, 60, 255), "фиолетовый": (140, 40, 255),
    "purple": (140, 40, 255), "розовый": (255, 60, 150), "pink": (255, 60, 150),
    "малиновый": (220, 20, 100), "белый": (255, 255, 255), "white": (255, 255, 255),
}

TOOLS: list[dict[str, Any]] = [
    {"name": "home_list", "description": "Список устройств умного дома с комнатой и состоянием. "
     "kind: lights, switches, sensors, people, covers или all.",
     "parameters": {"type": "OBJECT", "properties": {"kind": {"type": "STRING"}}}},
    {"name": "home_set", "description": "Управлять устройством или всеми светильниками комнаты. "
     "name — имя устройства или комнаты («свет в спальне», «торшер»). action: on, off, toggle, "
     "open, close.",
     "parameters": {"type": "OBJECT", "properties": {
         "name": {"type": "STRING"}, "action": {"type": "STRING"},
         "brightness_percent": {"type": "NUMBER", "description": "Яркость света 1-100"},
         "color_temp_kelvin": {"type": "NUMBER", "description": "Теплота света, 2000 тёплый - 6500 холодный"},
         "color": {"type": "STRING", "description": "Цвет света: название («красный», «blue») или #RRGGBB"},
         "effect": {"type": "STRING", "description": "Эффект света, если лампа умеет"},
         "transition_s": {"type": "NUMBER", "description": "Плавно за столько секунд"}},
         "required": ["name", "action"]}},
    {"name": "home_service", "description": "Любое действие над любым устройством HA: климат "
     "(set_temperature, set_hvac_mode), плеер (media_pause, volume_set, play_media), замок "
     "(lock, unlock), пылесос (start, return_to_base), сцена, скрипт, number/select и т.д. "
     "name — имя устройства; service — имя сервиса без домена; data — JSON-строка с параметрами "
     "сервиса, например {\"temperature\": 22}.",
     "parameters": {"type": "OBJECT", "properties": {
         "name": {"type": "STRING"}, "service": {"type": "STRING"}, "data": {"type": "STRING"}},
         "required": ["name", "service"]}},
    {"name": "home_state", "description": "Узнать состояние датчика или устройства: температура, "
     "влажность, кто дома, открыта ли дверь.",
     "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}},
]


class HAError(Exception):
    pass


@dataclass
class Entity:
    entity_id: str
    name: str
    area: str
    state: str
    unit: str = ""
    modes: tuple[str, ...] = ()      # supported_color_modes света
    effects: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        return self.entity_id.partition(".")[0]

    def brief(self) -> str:
        where = f" ({self.area})" if self.area else ""
        extra = ""
        if self.domain == "light":
            extra = (", умеет цвет" if self.can_color else
                     ", только белый (теплота)" if "color_temp" in self.modes else
                     ", только яркость" if self.modes else "")
            if self.effects:
                extra += f", эффекты: {', '.join(self.effects[:6])}"
        return f"{self.name}{where}: {self.state}{(' ' + self.unit) if self.unit else ''}{extra}"

    @property
    def can_color(self) -> bool:
        return bool(set(self.modes) & COLOR_MODES)


def words(text: str) -> set[str]:
    """Слова без окончаний: «спальне» и «спальня» → «спаль»."""
    return {w[:5] for w in re.findall(r"\w+", text.casefold()) if len(w) > 1}


def parse_color(text: str) -> tuple[int, int, int] | None:
    text = (text or "").strip().casefold()
    if m := re.fullmatch(r"#?([0-9a-f]{6})", text):
        return tuple(int(m[1][i:i + 2], 16) for i in (0, 2, 4))     # type: ignore[return-value]
    return COLORS.get(text)


def score(query: str, text: str) -> float:
    q = words(query)
    return len(q & words(text)) / len(q) if q else 0.0


class HomeAssistant:
    def __init__(self, url: str, token: str, *, connect: Callable = ws_connect, clock=time.monotonic):
        self.url, self.token, self._connect, self._clock = url, token, connect, clock
        self._ws: Any = None
        self._id = 0
        self._lock = asyncio.Lock()
        self._cache: tuple[float, list[Entity]] | None = None

    # ── соединение ──

    async def _open(self) -> None:
        try:
            ws = await self._connect(self.url, max_size=None, open_timeout=TIMEOUT_S)
            hello = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
            if hello.get("type") != "auth_required":
                raise HAError(f"HA ответил неожиданно: {hello}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            reply = json.loads(await asyncio.wait_for(ws.recv(), TIMEOUT_S))
        except (OSError, TimeoutError) as e:
            raise HAError(f"нет связи с Home Assistant: {e!r}") from None
        if reply.get("type") != "auth_ok":
            await ws.close()
            raise HAError("Home Assistant не принял токен")
        self._ws = ws

    async def request(self, type_: str, **fields: Any) -> Any:
        async with self._lock:
            for attempt in (0, 1):
                try:
                    if self._ws is None:
                        await self._open()
                    self._id += 1
                    await self._ws.send(json.dumps({"id": self._id, "type": type_, **fields}))
                    while True:
                        msg = json.loads(await asyncio.wait_for(self._ws.recv(), TIMEOUT_S))
                        if msg.get("id") == self._id:
                            break
                except HAError:
                    raise
                except Exception as e:     # соединение умерло — одна попытка заново
                    self._ws = None
                    if attempt:
                        raise HAError(f"нет связи с Home Assistant: {e!r}") from None
                    continue
                if not msg.get("success"):
                    raise HAError((msg.get("error") or {}).get("message", "ошибка HA"))
                return msg.get("result")

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # ── устройства ──

    async def entities(self, fresh: bool = False) -> list[Entity]:
        if self._cache and not fresh and self._clock() - self._cache[0] < CACHE_S:
            return self._cache[1]
        states, reg, devices, areas = await asyncio.gather(
            self.request("get_states"), self.request("config/entity_registry/list"),
            self.request("config/device_registry/list"), self.request("config/area_registry/list"))
        area_name = {a["area_id"]: a["name"] for a in areas}
        device_area = {d["id"]: d.get("area_id") for d in devices}
        info = {e["entity_id"]: e for e in reg}
        out = []
        for s in states:
            eid = s["entity_id"]
            if eid.partition(".")[0] not in READABLE:
                continue
            r = info.get(eid, {})
            if r.get("hidden_by") or r.get("disabled_by"):
                continue
            area_id = r.get("area_id") or device_area.get(r.get("device_id"))
            attrs = s.get("attributes", {})
            out.append(Entity(eid, attrs.get("friendly_name") or eid, area_name.get(area_id, ""),
                              s["state"], attrs.get("unit_of_measurement", ""),
                              tuple(attrs.get("supported_color_modes") or ()),
                              tuple(attrs.get("effect_list") or ())))
        self._cache = (self._clock(), out)
        return out

    async def resolve(self, query: str, domains: tuple[str, ...]) -> list[Entity]:
        """Устройства, подходящие под запрос: комната целиком или лучшее совпадение по имени."""
        ents = [e for e in await self.entities() if e.domain in domains]
        by_name = sorted(((score(query, f"{e.name} {e.area}"), e) for e in ents), key=lambda x: -x[0])
        if not by_name or by_name[0][0] < 0.5:
            return []
        best = by_name[0][0]
        top = [e for s, e in by_name if s == best]
        rooms = {e.area for e in top}
        # «свет в спальне»: совпала комната, а не конкретное устройство
        if len(top) > 1 and len(rooms) == 1 and "" not in rooms and score(query, top[0].area) >= 0.5:
            return top
        return top[:1] if len(top) == 1 else top

    async def service(self, query: str, service: str, data: dict[str, Any] | None = None) -> dict:
        """Любой сервис HA над найденным устройством (домен берётся у устройства)."""
        found = await self.resolve(query, CONTROLLABLE)
        if not found:
            return {"ok": False, "error": f"не нашёл устройство «{query}»"}
        rooms = {e.area for e in found}
        if len(found) > 1 and not (len(rooms) == 1 and "" not in rooms):
            return {"ok": False, "error": "подходит несколько, уточни",
                    "candidates": [e.name for e in found[:5]]}
        service = service.rpartition(".")[2]
        done = []
        for e in found:
            await self.request("call_service", domain=e.domain, service=service,
                               service_data=data or {}, target={"entity_id": e.entity_id})
            done.append(e.name)
        self._cache = None
        return {"ok": True, "devices": done, "service": service}

    async def set(self, query: str, action: str, brightness: float | None = None,
                  kelvin: float | None = None, color: str = "", effect: str = "",
                  transition: float | None = None) -> dict:
        found = await self.resolve(query, CONTROLLABLE)
        if not found:
            return {"ok": False, "error": f"не нашёл устройство «{query}»"}
        rooms = {e.area for e in found}
        if len(found) > 1 and not (len(rooms) == 1 and "" not in rooms):
            return {"ok": False, "error": "подходит несколько, уточни",
                    "candidates": [e.name for e in found[:5]]}
        if color and parse_color(color):
            colorful = [e for e in found if e.domain == "light" and (e.can_color or not e.modes)]
            if not colorful:
                alt = [e.name for e in await self.entities() if e.domain == "light" and e.can_color
                       and e.area in {f.area for f in found} and e.state != "unavailable"]
                return {"ok": False, "error": "эта лампа не умеет менять цвет (по данным HA: "
                        + (", ".join(found[0].modes) or "режимы неизвестны") + ")",
                        "color_capable_in_same_room": alt[:5]}
            found = colorful
        if effect and all(e.effects and effect not in e.effects for e in found):
            return {"ok": False, "error": "такого эффекта нет",
                    "available_effects": sorted({x for e in found for x in e.effects})[:15]}
        done = []
        for e in found:
            domain, service, data = self._service(e, action, brightness, kelvin, color, effect, transition)
            await self.request("call_service", domain=domain, service=service,
                               service_data=data, target={"entity_id": e.entity_id})
            done.append(e.name)
        self._cache = None
        return {"ok": True, "devices": done, "action": action}

    @staticmethod
    def _service(e: Entity, action: str, brightness: float | None, kelvin: float | None,
                 color: str = "", effect: str = "", transition: float | None = None):
        action = action.casefold()
        if e.domain == "cover":
            return "cover", {"open": "open_cover", "on": "open_cover",
                             "close": "close_cover", "off": "close_cover"}.get(action, "toggle"), {}
        if e.domain in ("scene", "script", "automation", "button", "input_button"):
            return e.domain, {"button": "press", "input_button": "press"}.get(e.domain, "turn_on"), {}
        if e.domain == "lock":
            return "lock", "unlock" if action in ("on", "open", "unlock") else "lock", {}
        service = {"on": "turn_on", "off": "turn_off", "open": "turn_on", "close": "turn_off"}.get(action, "toggle")
        data: dict[str, Any] = {}
        if e.domain == "light" and service == "turn_on":
            if brightness is not None:
                data["brightness_pct"] = max(1, min(100, int(brightness)))
            if kelvin is not None:
                data["color_temp_kelvin"] = max(1500, min(9000, int(kelvin)))
            rgb = parse_color(color)
            if rgb:
                data["rgb_color"] = list(rgb)
                data.pop("color_temp_kelvin", None)
                data.setdefault("brightness_pct", 100)
            if effect:
                data["effect"] = effect
            if transition is not None:
                data["transition"] = max(0, min(300, float(transition)))
        return e.domain, service, data

    async def state(self, query: str) -> dict:
        found = await self.resolve(query, READABLE)
        if not found:
            return {"ok": False, "error": f"не нашёл «{query}»"}
        return {"ok": True, "states": [e.brief() for e in found[:5]]}

    async def listing(self, kind: str = "all") -> dict:
        domains = KIND_DOMAINS.get(kind.casefold(), READABLE)
        return {"devices": [e.brief() for e in await self.entities() if e.domain in domains][:40]}

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        try:
            if name == "home_list":
                return await self.listing(str(args.get("kind", "all")))
            if name == "home_set":
                return await self.set(str(args.get("name", "")), str(args.get("action", "toggle")),
                                      args.get("brightness_percent"), args.get("color_temp_kelvin"),
                                      str(args.get("color", "")), str(args.get("effect", "")),
                                      args.get("transition_s"))
            if name == "home_service":
                raw = args.get("data") or "{}"
                data = raw if isinstance(raw, dict) else json.loads(raw)
                return await self.service(str(args.get("name", "")), str(args.get("service", "")), data)
            if name == "home_state":
                return await self.state(str(args.get("name", "")))
        except ValueError as e:
            return {"ok": False, "error": f"неверные параметры: {e}"}
        except HAError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}
