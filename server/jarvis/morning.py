"""Утро школьника: будильник по расписанию WebUntis, автобус, контекст для мягкого пробуждения.

* Вечером Мага сам предлагает время подъёма на завтра («подъём в 6:19, ставлю?») и по «да»
  вызывает set_wake_alarm: телефону уходит сообщение set_alarm (Android AlarmClock).
* Время подъёма = старт первого состоявшегося урока − дорога − сборы. Дорога: от отъезда
  автобуса (по умолчанию 7:19) до начала первого урока (по умолчанию 8:00). Если известно
  расписание автобуса (transit.py), берётся последний рейс не позже расчётного отъезда.
* Утром, пока человек собирается, следим за автобусом: опоздал или отменён — говорим.
* Инструменты Gemini: wake_plan, set_wake_alarm, next_bus.
Всё включается переменными .env (см. MorningConfig). Сеть и время подменяются в тестах.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .transit import LATE_MIN, Departure, Transit
from .untis import Lesson, Untis

log = logging.getLogger(__name__)

TICK_S = 60.0
RETRY_S = 20.0
BUS_POLL_S = 180.0
PREF_ASKED = "morning_asked_day"
PREF_ALARM = "morning_alarm"

MORNING_TOOLS: list[dict[str, Any]] = [
    {"name": "wake_plan", "description": "Во сколько вставать в ближайший учебный день: первый урок по расписанию "
     "WebUntis (с учётом отмен и замен), автобус и сборы. Вызови на вопросы вроде «во сколько мне завтра вставать».",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "set_wake_alarm", "description": "Поставить будильник на телефоне. time — ЧЧ:ММ. Вызывай только "
     "после согласия пользователя («да, ставь») или по его прямой просьбе.",
     "parameters": {"type": "OBJECT", "properties": {"time": {"type": "STRING", "description": "ЧЧ:ММ, например 06:40"}},
                    "required": ["time"]}},
    {"name": "next_bus", "description": "Ближайшие автобусы с реальным временем и опозданиями. Вызови на «когда мой "
     "автобус», «не опаздывает ли автобус».", "parameters": {"type": "OBJECT", "properties": {}}},
]
TOOL_NAMES = {t["name"] for t in MORNING_TOOLS}

ASK_PROMPT = (
    "Вечер. Предложи пользователю будильник на завтра, по-русски, коротко, в своей манере. Факты (не выдумывай "
    "ничего сверх них): {facts} Скажи примерно так: «Завтра подъём в {wake}, ставлю?». Если он согласен "
    "(«да», «давай», «ставь»), вызови set_wake_alarm с time=\"{wake}\" и подтверди одной фразой. Если называет другое "
    "время, вызови set_wake_alarm с ним. Если отказывается, ничего не ставь.")
BUS_LATE_PROMPT = (
    "Утро, пользователь собирается в школу. Предупреди его об автобусе одной-двумя короткими фразами и скажи, "
    "что можно выйти позже. Факты: {facts}")
BUS_CANCEL_PROMPT = (
    "Утро, пользователь собирается в школу. Автобус отменён: скажи ему об этом сразу и коротко, предложи "
    "следующий рейс, если он указан. Факты: {facts}")


def _on(env: dict[str, str], key: str, default: str = "on") -> bool:
    return env.get(key, default).strip().lower() not in ("0", "false", "no", "off")


def _hhmm(v: str, default: str) -> str:
    ok = len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60
    return v if ok else default


def _min(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


@dataclass(frozen=True)
class MorningConfig:
    wake_alarm: bool = True          # WAKE_ALARM: вечернее предложение будильника
    prep_min: int = 60               # WAKE_PREP_MIN: сборы
    bus_depart: str = "07:19"        # WAKE_BUS_DEPART: во сколько уезжает автобус
    school_start: str = "08:00"      # WAKE_SCHOOL_START: к какому уроку он едет (отсюда длина дороги)
    earliest: str = "05:00"          # WAKE_EARLIEST: раньше не будить
    ask_at: str = "21:30"            # WAKE_ASK_AT: вечером с этого часа
    ask_until: str = "23:00"         # WAKE_ASK_UNTIL: и не позже
    ha_entity: str = ""              # WAKE_HA_ENTITY: input_datetime в HA для «Рассвета к будильнику»
    soft_wake: bool = True           # SOFT_WAKE: мягкое пробуждение после будильника
    soft_wake_s: float = 30.0        # SOFT_WAKE_DELAY_S
    bus_warn: bool = True            # BUS_WARN: предупреждать об автобусе утром
    bus_watch_min: int = 40          # BUS_WATCH_MIN: за сколько минут до отъезда начинать следить

    @property
    def commute_min(self) -> int:
        return max(_min(self.school_start) - _min(self.bus_depart), 0)

    @classmethod
    def from_env(cls, env: dict[str, str]) -> MorningConfig:
        def num(key: str, default: float) -> float:
            try:
                return max(float(env.get(key, "").strip() or default), 0.0)
            except ValueError:
                return default
        d = cls()
        return cls(wake_alarm=_on(env, "WAKE_ALARM"), prep_min=int(num("WAKE_PREP_MIN", d.prep_min)),
                   bus_depart=_hhmm(env.get("WAKE_BUS_DEPART", "").strip(), d.bus_depart),
                   school_start=_hhmm(env.get("WAKE_SCHOOL_START", "").strip(), d.school_start),
                   earliest=_hhmm(env.get("WAKE_EARLIEST", "").strip(), d.earliest),
                   ask_at=_hhmm(env.get("WAKE_ASK_AT", "").strip(), d.ask_at),
                   ask_until=_hhmm(env.get("WAKE_ASK_UNTIL", "").strip(), d.ask_until),
                   ha_entity=env.get("WAKE_HA_ENTITY", "").strip(), soft_wake=_on(env, "SOFT_WAKE"),
                   soft_wake_s=num("SOFT_WAKE_DELAY_S", d.soft_wake_s), bus_warn=_on(env, "BUS_WARN"),
                   bus_watch_min=int(num("BUS_WATCH_MIN", d.bus_watch_min)))


@dataclass(frozen=True)
class WakePlan:
    day: date
    first: Lesson
    depart: datetime         # когда выходить на автобус
    wake: datetime
    bus: Departure | None = None
    cancelled: tuple[str, ...] = ()    # отменённые уроки перед первым состоявшимся

    def facts(self, prep_min: int) -> str:
        s = (f"первый урок завтра в {self.first.start:%H:%M} ({self.first.subject}"
             f"{', замена' if self.first.changed else ''}); ")
        if self.cancelled:
            s += f"до него уроки отменены: {', '.join(self.cancelled)}; "
        s += f"автобус в {self.depart:%H:%M}; на сборы {prep_min} минут; подъём в {self.wake:%H:%M}."
        return s


def make_plan(first: Lesson, cfg: MorningConfig, bus: Departure | None = None,
              cancelled: tuple[str, ...] = ()) -> WakePlan:
    depart = bus.planned if bus else first.start - timedelta(minutes=cfg.commute_min)
    wake = depart - timedelta(minutes=cfg.prep_min)
    wake = wake.replace(minute=wake.minute - wake.minute % 5, second=0, microsecond=0)   # вниз до 5 минут
    floor = wake.replace(hour=int(cfg.earliest[:2]), minute=int(cfg.earliest[3:]))
    return WakePlan(first.start.date(), first, depart, max(wake, floor), bus, cancelled)


class Morning:
    def __init__(self, cfg: MorningConfig, announce: Callable[[str], Awaitable[bool]], *,
                 untis: Untis | None = None, transit: Transit | None = None, bridge=None, brain=None, ha=None,
                 now: Callable[[], datetime] = datetime.now, clock: Callable[[], float] | None = None):
        self.cfg, self.announce = cfg, announce
        self.untis = untis if untis and untis.enabled else None
        self.transit = transit if transit and transit.enabled else None
        self.bridge, self.brain, self.ha = bridge, brain, ha
        self.now = now
        self._clock = clock or asyncio.get_running_loop().time
        self.routines = None                  # проставляет __main__: нужен для «человек уже встал»
        self._asked = brain.pref(PREF_ASKED) if brain else ""
        self._bus_key: tuple | None = None
        self._bus_day = ""
        self._tasks: list[asyncio.Task] = []
        self._warned: set[str] = set()

    def tools(self) -> list[dict[str, Any]]:
        out = []
        if self.untis:
            out += [t for t in MORNING_TOOLS if t["name"] in ("wake_plan", "set_wake_alarm")]
        if self.transit:
            out += [t for t in MORNING_TOOLS if t["name"] == "next_bus"]
        return out

    # ── план подъёма ──

    async def plan(self, day: date) -> WakePlan | None:
        """План на день или None: нет расписания, нет уроков, всё отменено, WebUntis не настроен."""
        if not self.untis:
            return None
        lessons = await self.untis.lessons(day)
        first = next((x for x in lessons if not x.cancelled), None)
        if first is None:
            return None
        skipped = tuple(x.subject for x in lessons if x.cancelled and x.start < first.start)
        bus = None
        if self.transit:
            try:
                bus = await self.transit.bus_before(first.start - timedelta(minutes=self.cfg.commute_min))
            except Exception as e:
                log.warning("утро: расписание автобуса не получено, считаю по умолчанию: %r", e)
        return make_plan(first, self.cfg, bus, skipped)

    async def set_alarm(self, hhmm: str) -> dict:
        hhmm = hhmm.strip().replace(".", ":")
        if len(hhmm) == 4 and hhmm[1] == ":":
            hhmm = "0" + hhmm
        if _hhmm(hhmm, "") == "":
            return {"ok": False, "error": "время нужно в виде ЧЧ:ММ"}
        phone = getattr(self.bridge, "phone", None)
        if phone is None:
            return {"ok": False, "error": "телефон не на связи, будильник не поставлен"}
        h, m = int(hhmm[:2]), int(hhmm[3:])
        if not await phone.send({"type": "set_alarm", "hour": h, "minute": m, "label": "Мага: подъём"}):
            return {"ok": False, "error": "телефон не ответил, будильник не поставлен"}
        stamp = f"{self.now():%Y-%m-%d} {hhmm}"
        if self.brain:
            self.brain.set_pref(PREF_ALARM, stamp)
        await self._push_ha(hhmm)
        log.info("утро: будильник %s отправлен на телефон", hhmm)
        return {"ok": True, "time": hhmm, "note": "отправлено на телефон; если телефон попросит, подтверди будильник на нём"}

    async def _push_ha(self, hhmm: str) -> None:
        """Время подъёма в input_datetime HA, чтобы «Рассвет к будильнику» шёл от него (если задан WAKE_HA_ENTITY)."""
        if not (self.cfg.ha_entity and self.ha):
            return
        try:
            await self.ha.request("call_service", domain="input_datetime", service="set_datetime",
                                  service_data={"time": f"{hhmm}:00"}, target={"entity_id": self.cfg.ha_entity})
        except Exception as e:
            log.warning("утро: HA не принял время подъёма: %r", e)

    def _phone_alarm_near(self, wake: datetime) -> bool:
        st = getattr(self.bridge, "phone_state", None) or {}
        at = st.get("next_alarm", 0) / 1000
        return bool(at) and abs(at - wake.timestamp()) < 5 * 60

    # ── вечернее предложение ──

    def _in_ask_window(self) -> bool:
        t = self.now()
        m = t.hour * 60 + t.minute
        return _min(self.cfg.ask_at) <= m < _min(self.cfg.ask_until)

    async def ask_step(self) -> float:
        if not (self.cfg.wake_alarm and self.untis) or not self._in_ask_window():
            return TICK_S
        day = self.now().strftime("%Y-%m-%d")
        if self._asked == day:
            return TICK_S
        plan = await self.plan(self.now().date() + timedelta(days=1))
        if plan is None or self._phone_alarm_near(plan.wake):
            self._mark_asked(day, "уроков нет или будильник уже стоит")
            return TICK_S
        if await self.announce(ASK_PROMPT.format(facts=plan.facts(self.cfg.prep_min), wake=f"{plan.wake:%H:%M}")):
            self._mark_asked(day, f"предложил подъём в {plan.wake:%H:%M}")
            return TICK_S
        return RETRY_S

    def _mark_asked(self, day: str, why: str) -> None:
        log.info("утро: %s", why)
        self._asked = day
        if self.brain:
            self.brain.set_pref(PREF_ASKED, day)

    # ── контекст мягкого пробуждения ──

    async def wake_context(self) -> str:
        """Факты для утреннего приветствия: первый урок сегодня и автобус. Пусто, если узнать не вышло."""
        parts = []
        try:
            plan = await self.plan(self.now().date())
            if plan:
                parts.append(f"первый урок сегодня в {plan.first.start:%H:%M} ({plan.first.subject}), "
                             f"автобус в {plan.depart:%H:%M}")
                if plan.cancelled:
                    parts.append(f"отменены: {', '.join(plan.cancelled)}")
            elif self.untis:
                parts.append("уроков сегодня нет")
        except Exception as e:
            log.warning("утро: расписание для приветствия не получено: %r", e)
        if self.transit:
            try:
                dep = await self.transit.bus_at(self._target_depart())
                if dep:
                    parts.append(f"автобус: {dep.describe()}")
            except Exception as e:
                log.warning("утро: автобус для приветствия не получен: %r", e)
        return "Известно из расписания: " + "; ".join(parts) + "." if parts else ""

    # ── автобус по утрам ──

    def _target_depart(self) -> datetime:
        t = self.now()
        return t.replace(hour=int(self.cfg.bus_depart[:2]), minute=int(self.cfg.bus_depart[3:]), second=0, microsecond=0)

    async def _today_depart(self) -> datetime | None:
        """Когда сегодня уезжает автобус; None — сегодня не учебный день."""
        today = self.now().date()
        if self.untis:
            plan = await self.plan(today)
            return plan.depart if plan else None
        return self._target_depart() if today.weekday() < 5 else None

    async def bus_step(self) -> float:
        if not (self.cfg.bus_warn and self.transit):
            return TICK_S * 5
        t = self.now()
        day = t.strftime("%Y-%m-%d")
        if day != self._bus_day:
            self._bus_day, self._bus_key = day, None
        depart = await self._today_depart()
        if depart is None or not depart - timedelta(minutes=self.cfg.bus_watch_min) <= t < depart:
            return TICK_S
        if self.routines is not None and not self.routines.awake():
            return TICK_S
        dep = await self.transit.bus_at(depart)
        if dep is None or not (dep.cancelled or dep.delay_min >= LATE_MIN):
            return BUS_POLL_S
        key = (dep.planned, dep.cancelled, dep.delay_min // 2)
        if key == self._bus_key:
            return BUS_POLL_S
        facts = dep.describe()
        prompt = BUS_CANCEL_PROMPT if dep.cancelled else BUS_LATE_PROMPT
        if dep.cancelled:
            later = [d for d in await self.transit.next_buses(3) if d.planned > dep.planned and not d.cancelled]
            if later:
                facts += f". Следующий: {later[0].describe()}"
        if await self.announce(prompt.format(facts=facts)):
            self._bus_key = key
            log.info("утро: предупредил про автобус (%s)", facts)
        return BUS_POLL_S if self._bus_key == key else RETRY_S

    # ── инструменты Gemini ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "set_wake_alarm":
            return await self.set_alarm(str(args.get("time", "")))
        if name == "wake_plan" and self.untis:
            day = self.now().date()
            for d in (day + timedelta(days=i) for i in range(1, 8)):    # ближайший день с уроками
                plan = await self.plan(d)
                if plan:
                    return {"ok": True, "day": plan.day.isoformat(), "wake": f"{plan.wake:%H:%M}",
                            "details": plan.facts(self.cfg.prep_min)}
            return {"ok": True, "note": "ближайшие дни без уроков"}
        if name == "next_bus" and self.transit:
            buses = await self.transit.next_buses(3)
            return {"ok": True, "buses": [b.describe() for b in buses] or [], "note": "" if buses else "рейсов в ближайшие два часа нет"}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── жизненный цикл ──

    async def _loop(self, step: Callable[[], Awaitable[float]], what: str) -> None:
        while True:
            try:
                delay = await step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if what not in self._warned:
                    self._warned.add(what)
                    log.warning("утро: %s не вышло: %r", what, e)
                delay = 300.0
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self.untis and self.cfg.wake_alarm:
            self._tasks.append(asyncio.create_task(self._loop(self.ask_step, "вечернее предложение")))
        if self.transit and self.cfg.bus_warn:
            self._tasks.append(asyncio.create_task(self._loop(self.bus_step, "слежение за автобусом")))
        if not self._tasks:
            log.info("утро: ни WebUntis, ни остановка не настроены, пакет спит")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── консоль ──

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "morning":
            return None
        arg = parts[1].lower() if len(parts) > 1 else "plan"
        try:
            if arg == "plan":
                if not self.untis:
                    return "WebUntis не настроен (UNTIS_ICAL_URL или UNTIS_SERVER/SCHOOL/USER/PASSWORD)"
                r = await self.call("wake_plan", {})
                return r.get("details") or r.get("note", "")
            if arg == "bus":
                if not self.transit:
                    return "остановка не задана (TRANSIT_STOP)"
                return "\n".join((await self.call("next_bus", {}))["buses"]) or "рейсов нет"
            if arg == "ask":
                plan = await self.plan(self.now().date() + timedelta(days=1))
                if plan is None:
                    return "на завтра уроков нет (или WebUntis не настроен)"
                ok = await self.announce(ASK_PROMPT.format(facts=plan.facts(self.cfg.prep_min), wake=f"{plan.wake:%H:%M}"))
                return "колонка заговорила" if ok else "колонки нет или она занята разговором"
            if arg == "set" and len(parts) > 2:
                return str(await self.set_alarm(parts[2]))
        except Exception as e:
            return f"не вышло: {e!r}"
        return "morning plan|bus|ask|set ЧЧ:ММ"
