"""Рутины: колонка сама заговаривает в нужный момент (утренний брифинг, возвращение домой).

Рутина = Voice.announce(подсказка): колонка начинает разговор, а Gemini в нём сам вызывает
инструменты (время, погода, календарь, задачи, умный дом, google_*), если они подключены.
Поэтому здесь только «когда» и «что сказать сделать», сами данные мы не собираем.
Колонка занята разговором (announce вернул False) — повторяем с паузой до часа.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .bridge import _minutes, _valid_time, in_window

log = logging.getLogger(__name__)

TICK_S = 30.0
RETRY_S = 20.0
GIVE_UP_S = 3600.0            # брифинг через час после срока уже не нужен
BRIEFING_LATE_H = 3           # сработать по времени можно не позже, чем через столько часов после него
HOME_POLL_S = 60.0
HOME_AWAY_MIN_S = 3600.0      # «долгое отсутствие»: не меньше часа вне дома
HOME_EVERY_S = 3 * 3600.0     # приветствовать не чаще раза в 3 часа
HOME_GIVE_UP_S = 600.0

BRIEFING_PROMPT = (
    "Утро, пользователь проснулся. Сам заговори с ним первым и дай короткий утренний брифинг: "
    "скажи, который час и какой сегодня день, погоду на сегодня (через home_weather или то, что "
    "есть), события календаря на сегодня и задачи, если такие инструменты есть, и напоминания, "
    "если они ещё не сработали. Только то, что реально получилось узнать, ничего не выдумывай. "
    "Коротко и живо, по-русски, в твоей обычной манере, без длинных списков.")
HOME_PROMPT = (
    "Пользователь только что вернулся домой после долгого отсутствия. Тепло и коротко встреть его, "
    "в твоей обычной манере, одной-двумя фразами. Если есть смысл, добавь одну полезную мелочь: "
    "ближайшее событие календаря или напоминание. Ничего не выдумывай и не устраивай допрос.")


class Routines:
    def __init__(self, announce: Callable[[str], Awaitable[bool]], *, settings: Callable[[], dict[str, Any]],
                 brain=None, ha=None, briefing_at: str = "08:00",
                 now: Callable[[], datetime] = datetime.now, clock: Callable[[], float] | None = None):
        """settings() → настройки моста (нужны night_start и night_end)."""
        self.announce = announce
        self.settings = settings
        self.brain = brain
        self.ha = ha
        self.briefing_at = briefing_at if _valid_time(briefing_at) else "08:00"
        self.now = now
        self._clock = clock or asyncio.get_running_loop().time
        self._woke_day = ""                   # день, когда выключили будильник
        self._due_since: float | None = None  # когда брифинг стал нужен (для отсчёта «до часа»)
        self._briefed = brain.pref("routine_briefing_day") if brain else ""
        self._away_since: dict[str, float] = {}
        self._was_home: dict[str, bool] = {}
        self._greeted_at: float | None = None
        self._tasks: list[asyncio.Task] = []

    # ── сигналы ──

    def wake_signal(self) -> None:
        """Будильник на телефоне выключен (зовёт мост): человек проснулся."""
        self._woke_day = self.now().strftime("%Y-%m-%d")
        log.info("рутины: будильник выключен, брифинг когда не ночь")

    def night(self) -> bool:
        local = self.now()
        v = self.settings()
        return in_window(local.hour * 60 + local.minute, v["night_start"], v["night_end"])

    # ── утренний брифинг ──

    def briefing_due(self) -> bool:
        """Пора ли говорить брифинг: раз в день, не ночью; по будильнику или по времени."""
        local = self.now()
        day = local.strftime("%Y-%m-%d")
        if self._briefed == day or self.night():
            return False
        minutes = local.hour * 60 + local.minute
        if minutes < _minutes(self.settings()["night_end"]):
            return False
        if self._woke_day == day:
            return True
        start = _minutes(self.briefing_at)
        return start <= minutes < start + BRIEFING_LATE_H * 60

    async def briefing_step(self) -> float:
        """Один шаг планировщика; возвращает паузу до следующего."""
        if not self.briefing_due():
            self._due_since = None
            return TICK_S
        now = self._clock()
        if self._due_since is None:
            self._due_since = now
        day = self.now().strftime("%Y-%m-%d")
        if now - self._due_since > GIVE_UP_S:
            self._finish_briefing(day, "не вышло в течение часа, отказываюсь")
            return TICK_S
        if await self.announce(BRIEFING_PROMPT):
            self._finish_briefing(day, "сказан")
            return TICK_S
        return RETRY_S

    def _finish_briefing(self, day: str, why: str) -> None:
        log.info("рутины: брифинг %s", why)
        self._briefed, self._due_since = day, None
        if self.brain:
            self.brain.set_pref("routine_briefing_day", day)

    # ── возвращение домой ──

    async def poll_home(self) -> bool:
        """Опросить person.* в HA; True — человек вернулся после долгого отсутствия."""
        if self.ha is None:
            return False
        states = await self.ha.request("get_states")
        now = self._clock()
        arrived = False
        for s in states:
            eid = s.get("entity_id", "")
            if not eid.startswith("person."):
                continue
            home = s.get("state") == "home"
            was = self._was_home.get(eid)
            if was is True and not home:
                self._away_since[eid] = now
            if was is False and home and now - self._away_since.get(eid, now) >= HOME_AWAY_MIN_S:
                arrived = True
            self._was_home[eid] = home
        return arrived

    async def greet(self, prompt: str = HOME_PROMPT) -> bool:
        """Поздороваться, если не ночь и не чаще раза в 3 часа; повторы до 10 минут."""
        deadline = self._clock() + HOME_GIVE_UP_S
        while self._clock() < deadline:
            if self.night():
                log.info("рутины: вернулся ночью, не здороваюсь")
                return False
            if self._greeted_at is not None and self._clock() - self._greeted_at < HOME_EVERY_S:
                log.info("рутины: уже здоровались недавно, пропускаю")
                return False
            if await self.announce(prompt):
                self._greeted_at = self._clock()
                log.info("рутины: поздоровался")
                return True
            await asyncio.sleep(RETRY_S)
        return False

    async def _home_loop(self) -> None:
        failed = False
        while True:
            try:
                if await self.poll_home():
                    self._tasks.append(asyncio.create_task(self.greet()))
                failed = False
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not failed:      # одна строка на срыв, а не каждую минуту
                    log.warning("рутины: HA не отвечает, приветствие ждёт: %r", e)
                failed = True
            await asyncio.sleep(HOME_POLL_S)

    async def _briefing_loop(self) -> None:
        while True:
            try:
                delay = await self.briefing_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("рутины: брифинг")
                delay = TICK_S
            await asyncio.sleep(delay)

    # ── жизненный цикл ──

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._briefing_loop()))
        if self.ha is not None:
            self._tasks.append(asyncio.create_task(self._home_loop()))
        else:
            log.info("рутины: Home Assistant не подключён — приветствие при возвращении домой пропущено")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── консоль ──

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "routine":
            return None
        which = parts[1].lower() if len(parts) > 1 else ""
        prompt = {"briefing": BRIEFING_PROMPT, "home": HOME_PROMPT}.get(which)
        if prompt is None:
            return "routine briefing|home — запустить рутину сразу, не дожидаясь срока"
        ok = await self.announce(prompt)
        return f"рутина {which}: " + ("колонка заговорила" if ok else "колонки нет или она занята разговором")
