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
from datetime import date, datetime, timedelta
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from . import protocol

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
DOMAIN_PRIO = {"light": 0, "switch": 1, "fan": 2, "cover": 3, "climate": 4, "media_player": 5,
               "lock": 6, "vacuum": 6, "scene": 8, "script": 8, "automation": 8}
COLOR_MODES = {"hs", "xy", "rgb", "rgbw", "rgbww"}
COLORS = {  # название → RGB; по-русски и по-английски
    "красный": (255, 0, 0), "red": (255, 0, 0), "оранжевый": (255, 120, 0), "orange": (255, 120, 0),
    "жёлтый": (255, 220, 0), "желтый": (255, 220, 0), "yellow": (255, 220, 0),
    "зелёный": (0, 220, 60), "зеленый": (0, 220, 60), "green": (0, 220, 60),
    "голубой": (0, 190, 255), "cyan": (0, 190, 255), "бирюзовый": (0, 220, 200),
    "синий": (0, 60, 255), "blue": (0, 60, 255), "фиолетовый": (140, 40, 255),
    "purple": (140, 40, 255), "розовый": (255, 60, 150), "pink": (255, 60, 150),
    "малиновый": (220, 20, 100), "белый": (255, 255, 255), "white": (255, 255, 255),
    # оттенки для светодиодов (насыщенные: тусклые на ленте выглядят грязно)
    "crimson": (220, 20, 60), "scarlet": (255, 36, 0), "maroon": (128, 0, 20), "burgundy": (128, 0, 32),
    "бордовый": (128, 0, 32), "вишневый": (180, 10, 50), "вишнёвый": (180, 10, 50), "алый": (255, 36, 0),
    "magenta": (255, 0, 255), "fuchsia": (255, 0, 200), "пурпурный": (160, 0, 160), "фуксия": (255, 0, 200),
    "violet": (160, 60, 255), "lilac": (190, 130, 255), "лиловый": (180, 100, 255), "сиреневый": (190, 130, 255),
    "lavender": (170, 140, 255), "лавандовый": (170, 140, 255), "indigo": (75, 0, 200), "индиго": (75, 0, 200),
    "navy": (0, 0, 160), "azure": (0, 150, 255), "лазурный": (0, 150, 255), "sky": (80, 180, 255),
    "turquoise": (0, 220, 200), "teal": (0, 160, 150), "aqua": (0, 255, 255), "аквамарин": (0, 255, 200),
    "mint": (80, 255, 170), "мятный": (80, 255, 170), "lime": (150, 255, 0), "салатовый": (127, 255, 0),
    "emerald": (0, 201, 87), "изумрудный": (0, 201, 87), "olive": (150, 160, 0), "оливковый": (150, 160, 0),
    "gold": (255, 200, 0), "золотой": (255, 200, 0), "amber": (255, 170, 0), "янтарный": (255, 170, 0),
    "coral": (255, 100, 80), "коралловый": (255, 100, 80), "salmon": (255, 110, 100), "peach": (255, 150, 100),
    "персиковый": (255, 150, 100), "brown": (150, 75, 0), "коричневый": (150, 75, 0), "gray": (128, 128, 128),
    "grey": (128, 128, 128), "серый": (128, 128, 128), "teal blue": (0, 130, 200),
}
_ADJ_ENDINGS = ("ый", "ий", "ой", "ая", "яя", "ое", "ее", "ые", "ие")
_DARK_PREFIXES = ("тёмно-", "темно-", "тёмно ", "темно ", "тёмный ", "темный ", "dark ", "dark-")
_LIGHT_PREFIXES = ("светло-", "светло ", "светлый ", "light ", "light-", "бледно-", "pale ")
_BRIGHT_PREFIXES = ("ярко-", "ярко ", "яркий ", "bright ", "bright-", "насыщенно-")

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
         "color": {"type": "STRING", "description": "Цвет света: название («красный», «малиновый», «blue») или "
                  "#RRGGBB. Для любого другого оттенка сам вычисли и передай #RRGGBB"},
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
    {"name": "home_calendar", "description": "События календаря на ближайшие дни из всех календарей "
     "дома: «что у меня сегодня / завтра / на неделе», «когда встреча». days — на сколько дней "
     "вперёд смотреть (1 = только сегодня, по умолчанию 2, максимум 31).",
     "parameters": {"type": "OBJECT", "properties": {"days": {"type": "NUMBER"}}}},
    {"name": "home_weather", "description": "Погода: сейчас (температура, условия, ветер, влажность) "
     "и прогноз на ближайшие дни. Зови на «какая погода», «брать ли зонт», «что на улице», "
     "«что завтра». Заодно обновляет значок погоды на экране колонки.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "home_state", "description": "Узнать состояние датчика или устройства: температура, "
     "влажность, кто дома, открыта ли дверь.",
     "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}},
]

WEATHER_RU = {
    "clear-night": "ясная ночь", "cloudy": "облачно", "exceptional": "необычная погода",
    "fog": "туман", "hail": "град", "lightning": "гроза", "lightning-rainy": "гроза с дождём",
    "partlycloudy": "переменная облачность", "pouring": "ливень", "rainy": "дождь",
    "snowy": "снег", "snowy-rainy": "снег с дождём", "sunny": "солнечно",
    "windy": "ветрено", "windy-variant": "облачно и ветрено",
}
# Условие HA → значок на экране колонки (0 ясно, 1 переменная облачность, 2 облачно, 3 дождь,
# 4 снег, 5 гроза, 6 туман, 7 ясная ночь; см. draw_weather в firmware/main/face/render.cpp).
# Град и ливень рисуем дождём, ветрено и «необычная» — просто облаком.
WEATHER_ICON = {
    "sunny": 0, "partlycloudy": 1, "cloudy": 2, "windy": 2, "windy-variant": 2, "exceptional": 2,
    "rainy": 3, "pouring": 3, "hail": 3, "snowy": 4, "snowy-rainy": 4, "lightning": 5,
    "lightning-rainy": 5, "fog": 6, "clear-night": 7,
}
WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MAX_EVENTS = 15


def day_words(d: date, today: date) -> str:
    delta = (d - today).days
    if delta in (0, 1, 2):
        return ("сегодня", "завтра", "послезавтра")[delta]
    return f"{d.day:02d}.{d.month:02d} ({WEEKDAYS[d.weekday()]})"


def event_when(start: str, today: date) -> tuple[str, str]:
    """Ключ сортировки и человеческое «когда» из start события (дата или дата-время ISO)."""
    if "T" not in start:
        return start, f"{day_words(date.fromisoformat(start), today)}, весь день"
    dt = datetime.fromisoformat(start)
    return dt.replace(tzinfo=None).isoformat(), f"{day_words(dt.date(), today)} в {dt:%H:%M}"


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


def _named_color(text: str) -> tuple[int, int, int] | None:
    """Название без приставок: учитывает ё/е и окончания («малиновая», «малиновое» → «малиновый»)."""
    for t in (text, text.replace("ё", "е")):
        if t in COLORS:
            return COLORS[t]
        for end in _ADJ_ENDINGS:
            if t.endswith(end):
                stem = t[:-len(end)]
                for ok in ("ый", "ий", "ой"):
                    for key in (stem + ok, stem + ok.replace("ё", "е")):
                        if key in COLORS:
                            return COLORS[key]
    return None


def parse_color(text: str) -> tuple[int, int, int] | None:
    """«crimson», «малиновая», «тёмно-синий», «светло-зелёный», «#ff8800» → RGB; незнакомое — None."""
    text = re.sub(r"\s+", " ", (text or "").strip().casefold())
    if m := re.fullmatch(r"#?([0-9a-f]{6})", text):
        return tuple(int(m[1][i:i + 2], 16) for i in (0, 2, 4))     # type: ignore[return-value]
    for prefixes, mix in ((_DARK_PREFIXES, None), (_LIGHT_PREFIXES, 255), (_BRIGHT_PREFIXES, -1)):
        for pre in prefixes:
            if text.startswith(pre):
                base = _named_color(text[len(pre):])
                if base is None:
                    return None
                if mix is None:       # тёмный: темнее, но не в чёрный (на ленте <25% не видно цвета)
                    return tuple(max(1, round(c * 0.55)) if c else 0 for c in base)   # type: ignore[return-value]
                if mix == 255:        # светлый: половина пути к белому
                    return tuple(round(c + (255 - c) * 0.5) for c in base)             # type: ignore[return-value]
                return base
    return _named_color(text)


def score(query: str, text: str) -> float:
    q = words(query)
    return len(q & words(text)) / len(q) if q else 0.0


class HomeAssistant:
    def __init__(self, url: str, token: str, *, connect: Callable = ws_connect, clock=time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        self.now = now
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
        """Устройства под запрос: точное имя важнее частичного, light важнее number/select,
        недоступные — в последнюю очередь; совпала комната, а не устройство — вся комната."""
        ents = [e for e in await self.entities() if e.domain in domains]
        alive = [e for e in ents if e.state != "unavailable"]
        ents = alive or ents
        norm = " ".join(sorted(words(query)))

        def key(e: Entity) -> tuple[float, bool]:
            return score(query, f"{e.name} {e.area}"), norm == " ".join(sorted(words(e.name)))

        ranked = sorted(((key(e), e) for e in ents), key=lambda x: (-x[0][0], not x[0][1]))
        if not ranked or ranked[0][0][0] < 0.5:
            return []
        top = [e for k, e in ranked if k == ranked[0][0]]
        best_prio = min(DOMAIN_PRIO.get(e.domain, 9) for e in top)
        top = [e for e in top if DOMAIN_PRIO.get(e.domain, 9) == best_prio]
        if len(top) > 1:
            shortest = min(len(e.name) for e in top)
            if not ranked[0][0][1]:      # не точное имя — «Подсветка стола» лучше, чем «… Скорость»
                top = [e for e in top if len(e.name) == shortest] if \
                    len({e.area for e in top}) > 1 or score(query, top[0].area) < 0.5 else top
        return top

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
        if color and parse_color(color) is None:
            return {"ok": False, "error": f"не знаю цвет «{color}»: передай его кодом #RRGGBB (любой оттенок) "
                                          "или назови привычным словом, лампы не менял"}
        found = await self.resolve(query, ("light",) if parse_color(color) else CONTROLLABLE)
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

    async def _response(self, domain: str, service: str, entity_ids: list[str],
                        data: dict[str, Any]) -> dict[str, Any]:
        """Сервис HA, который возвращает данные (return_response): {entity_id: ответ}."""
        result = await self.request("call_service", domain=domain, service=service,
                                    service_data=data, target={"entity_id": entity_ids},
                                    return_response=True)
        return (result or {}).get("response") or {}

    async def calendar(self, days: Any = 2) -> dict:
        """События ближайших `days` дней (с начала сегодняшнего дня) из всех календарей."""
        try:
            days = max(1, min(31, int(float(days))))
        except (TypeError, ValueError):
            days = 2
        cals = [e for e in await self.entities() if e.domain == "calendar" and e.state != "unavailable"]
        if not cals:
            return {"ok": False, "error": "в Home Assistant нет календарей"}
        now = self.now().replace(tzinfo=None)
        today = now.date()
        start = datetime.combine(today, datetime.min.time())
        resp = await self._response(
            "calendar", "get_events", [e.entity_id for e in cals],
            {"start_date_time": start.isoformat(sep=" "),
             "end_date_time": (start + timedelta(days=days)).isoformat(sep=" ")})
        found = []
        for e in cals:
            for ev in (resp.get(e.entity_id) or {}).get("events") or []:
                begin = str(ev.get("start") or "")
                if not begin:
                    continue
                end = str(ev.get("end") or "")
                if "T" in begin and "T" in end and datetime.fromisoformat(end).replace(tzinfo=None) < now:
                    continue        # сегодняшнее событие уже закончилось
                key, when = event_when(begin, today)
                text = f"{ev.get('summary') or 'без названия'} — {when}"
                if ev.get("location"):
                    text += f", {ev['location']}"
                found.append((key, text))
        events = [t for _, t in sorted(found)][:MAX_EVENTS]
        return {"ok": True, "days": days, "events": events, "note": "" if events else "событий нет"}

    async def weather(self, send: Callable[[dict], Any] | None = None) -> dict:
        """Погода из weather.*: сейчас и прогноз по дням. Значок и температуру шлёт на экран."""
        states = [s for s in await self.request("get_states")
                  if s["entity_id"].startswith("weather.") and s["state"] != "unavailable"]
        if not states:
            return {"ok": False, "error": "в Home Assistant нет погоды"}
        st = states[0]
        a = st.get("attributes", {})
        cond = st["state"]
        unit = a.get("temperature_unit", "°C")
        out: dict[str, Any] = {"ok": True, "now": {
            "condition": WEATHER_RU.get(cond, cond),
            "temperature": f"{round(float(a['temperature']))} {unit}" if a.get("temperature") is not None else None}}
        if a.get("humidity") is not None:
            out["now"]["humidity"] = f"{a['humidity']} %"
        if a.get("wind_speed") is not None:
            out["now"]["wind"] = f"{a['wind_speed']} {a.get('wind_speed_unit', 'км/ч')}"
        try:
            resp = await self._response("weather", "get_forecasts", [st["entity_id"]], {"type": "daily"})
            today = self.now().date()
            days = []
            for f in ((resp.get(st["entity_id"]) or {}).get("forecast") or [])[:5]:
                day = datetime.fromisoformat(str(f["datetime"])).date()
                line = f"{day_words(day, today)}: {WEATHER_RU.get(f.get('condition'), f.get('condition'))}"
                if f.get("temperature") is not None:
                    low = f.get("templow")
                    # целые градусы: с «17.8» Gemini при чтении вслух путает числа
                    line += (f", от {round(low)} до {round(f['temperature'])} {unit}" if low is not None
                             else f", {round(f['temperature'])} {unit}")
                if f.get("precipitation_probability") is not None:
                    line += f", осадки {f['precipitation_probability']} %"
                days.append(line)
            out["forecast"] = days
        except (HAError, ValueError, KeyError):
            out["forecast"] = []       # прогноз не получился — текущей погоды достаточно
        icon = WEATHER_ICON.get(cond)
        if send and icon is not None and a.get("temperature") is not None:
            await send(protocol.weather(icon, round(float(a["temperature"]))))
        return out

    async def call(self, name: str, args: dict[str, Any], send: Callable[[dict], Any] | None = None) -> dict:
        try:
            if name == "home_calendar":
                return await self.calendar(args.get("days", 2))
            if name == "home_weather":
                return await self.weather(send)
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
