"""Перерывы по активности компьютера (идея #50).

Мост ПК (bridge.pc.state["idle"]) присылает, сколько секунд никто не трогал мышь и клавиатуру. Пока простой
не больше 5 минут, считаем, что человек работает непрерывно. Через N минут подряд (JARVIS_BREAK_MIN,
по умолчанию 120, 0 = совсем выключено) один раз за такой отрезок колонка коротко предлагает размяться или
попить воды (Voice.announce). Не ночью, не в режиме тишины, не чаще раза в 90 минут; колонка занята —
пробуем на следующем шаге. Простой дольше 5 минут обнуляет отсчёт.

Включается только по просьбе: голосом «напоминай про перерывы» (функция breaks_mode) или консолью
`break on|off`; без этого модуль ничего не говорит.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MIN = 120
RESET_IDLE_S = 300.0       # простой дольше — перерыв уже был, отсчёт заново
MIN_GAP_S = 90 * 60.0      # напоминать не чаще
TICK_S = 30.0
PREF = "breaks_on"
LAST_PREF = "breaks_last"

TOOLS: list[dict[str, Any]] = [
    {"name": "breaks_mode", "description": "Напоминания о перерывах за компьютером. enabled=true — «напоминай "
     "про перерывы», false — «не напоминай про перерывы». Когда включено, после долгой непрерывной работы "
     "колонка сама предложит размяться или попить воды.",
     "parameters": {"type": "OBJECT", "properties": {"enabled": {"type": "BOOLEAN"}}, "required": ["enabled"]}},
]

PROMPT = ("Пользователь уже около {minutes} минут подряд сидит за компьютером. Сам заговори и одной короткой "
          "фразой, в своей манере, предложи размяться или попить воды. Без нотаций и без списков.{soft}")


class Breaks:
    TOOLS = TOOLS
    tool_names = frozenset(t["name"] for t in TOOLS)

    def __init__(self, announce: Callable[[str], Awaitable[bool]], *, bridge: Any, brain: Any,
                 muted: Callable[[], bool] = lambda: False, minutes: float = DEFAULT_MIN,
                 clock: Callable[[], float] = time.time):
        """muted() → стоит режим тишины; bridge даёт pc.state и night(); clock — Unix-секунды."""
        self.announce, self.bridge, self.brain, self.muted = announce, bridge, brain, muted
        self.minutes = max(float(minutes), 0.0)
        self._clock = clock
        self._since: float | None = None      # когда началась непрерывная работа
        self._fired = False                   # в этом отрезке уже предлагали
        self._task: asyncio.Task | None = None

    @classmethod
    def from_env(cls, announce, *, bridge, brain, muted, env: dict[str, str]) -> Breaks:
        try:
            minutes = float(env.get("JARVIS_BREAK_MIN", "").strip() or DEFAULT_MIN)
        except ValueError:
            minutes = DEFAULT_MIN
        return cls(announce, bridge=bridge, brain=brain, muted=muted, minutes=minutes)

    def enabled(self) -> bool:
        return self.minutes > 0 and self.brain.pref(PREF) == "1"

    def idle(self) -> float | None:
        pc = self.bridge.pc
        return None if pc is None else pc.state.get("idle")

    def _last(self) -> float:
        try:
            return float(self.brain.pref(LAST_PREF, "0"))
        except ValueError:
            return 0.0

    def work_minutes(self) -> float:
        return 0.0 if self._since is None else (self._clock() - self._since) / 60

    async def step(self) -> None:
        """Один шаг: обновить отсчёт непрерывной работы и, если пора, предложить перерыв."""
        now = self._clock()
        idle = self.idle()
        if idle is None or idle > RESET_IDLE_S:
            self._since, self._fired = None, False
            return
        if self._since is None:
            self._since = now - idle           # работа началась, когда трогали в последний раз
            self._fired = False
        if not self.enabled() or self._fired or self.work_minutes() < self.minutes:
            return
        if now - self._last() < MIN_GAP_S or self.bridge.night() or self.muted():
            return
        if await self.announce(PROMPT.format(minutes=int(self.work_minutes()),
                                             soft="")):
            self._fired = True
            self.brain.set_pref(LAST_PREF, str(now))
            log.info("перерыв: предложил после %.0f мин", self.work_minutes())

    async def _loop(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("перерывы: %r", e)
            await asyncio.sleep(TICK_S)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def set_enabled(self, on: bool) -> None:
        self.brain.set_pref(PREF, "1" if on else "0")

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name != "breaks_mode":
            return {"ok": False, "error": f"нет такого инструмента: {name}"}
        if self.minutes <= 0:
            return {"ok": False, "error": "перерывы выключены в настройках сервера (JARVIS_BREAK_MIN=0)"}
        on = args.get("enabled")
        on = on if isinstance(on, bool) else str(on).strip().casefold() in ("1", "true", "on", "да", "вкл")
        self.set_enabled(on)
        return {"ok": True, "enabled": on, "after_minutes": int(self.minutes)}

    async def command(self, line: str) -> str | None:
        head, _, rest = line.strip().partition(" ")
        if head.lower() != "break":
            return None
        sub = rest.strip().lower()
        if sub in ("on", "off"):
            if self.minutes <= 0:
                return "JARVIS_BREAK_MIN=0: перерывы выключены совсем"
            self.set_enabled(sub == "on")
            return f"напоминания о перерывах {sub} (через {self.minutes:.0f} мин непрерывной работы)"
        idle = self.idle()
        return (f"перерывы: {'вкл' if self.enabled() else 'выкл'}, порог {self.minutes:.0f} мин, "
                f"работаю подряд {self.work_minutes():.0f} мин, простой {'нет данных' if idle is None else f'{idle:.0f} с'}. "
                "break on|off")
