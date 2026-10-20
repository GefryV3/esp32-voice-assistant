"""Здоровье системы голосом: «Мага, как ты?» → функция health_report.

Собирает в один короткий словарь то, что Gemini перескажет: версии сервера и прошивки, кто на
связи (колонка, телефон, компьютер, Home Assistant, Google), сколько раз за сутки Gemini не
ответил, заряд батарей и сколько работает сервер. Всё, что не удалось узнать, помечаем
«не проверено», а не выдумываем.

Ошибки Gemini считает общий счётчик GEMINI_ERRORS: voice.py и telegram.py зовут
`GEMINI_ERRORS.add("голос")` там, где ловят сбой. Метки времени держим в памяти, а если
счётчику дали Brain (bind), ещё и в prefs, чтобы перезапуск не обнулял сутки.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import __version__

log = logging.getLogger(__name__)

DAY_S = 24 * 3600
KEEP = 500                # больше событий не храним
CHECK_TIMEOUT_S = 6.0
PREF_KEY = "gemini_errors"

TOOLS: list[dict[str, Any]] = [
    {"name": "health_report", "description": "Как ты? Состояние системы: версии сервера и прошивки, связь "
     "колонки, телефона, компьютера, Home Assistant и Google, ошибки Gemini за сутки, батареи, сколько "
     "сервер работает. Вызови на «как ты?», «как дела у системы», «всё ли работает», «что с тобой» и "
     "перескажи коротко и по-человечески, назвав только то, что сломано или важно.",
     "parameters": {"type": "OBJECT", "properties": {}}},
]


class ErrorLog:
    """Счётчик сбоев Gemini за последние сутки."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._events: deque[tuple[float, str]] = deque(maxlen=KEEP)
        self._brain: Any = None

    def bind(self, brain: Any) -> None:
        """Подключить хранилище и поднять сохранённые события."""
        self._brain = brain
        try:
            saved = json.loads(brain.pref(PREF_KEY, "[]"))
            for ts, kind in saved:
                self._events.append((float(ts), str(kind)))
        except (ValueError, TypeError):
            pass

    def add(self, kind: str = "gemini") -> None:
        self._events.append((self._clock(), kind))
        if self._brain is not None:
            try:
                self._brain.set_pref(PREF_KEY, json.dumps(list(self._events)))
            except Exception:
                log.debug("счётчик ошибок не сохранился", exc_info=True)

    def count(self, window_s: float = DAY_S) -> dict[str, int]:
        since = self._clock() - window_s
        out: dict[str, int] = {}
        for ts, kind in self._events:
            if ts >= since:
                out[kind] = out.get(kind, 0) + 1
        return out


GEMINI_ERRORS = ErrorLog()


def uptime_words(seconds: float) -> str:
    s = int(seconds)
    d, rest = divmod(s, 86400)
    h, rest = divmod(rest, 3600)
    m = rest // 60
    if d:
        return f"{d} д {h} ч"
    if h:
        return f"{h} ч {m} мин"
    return f"{m} мин"


def _level(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100 else None


class Health:
    TOOLS = TOOLS
    tool_names = frozenset(t["name"] for t in TOOLS)

    def __init__(self, *, hub=None, bridge=None, ha=None, google=None, errors: ErrorLog = GEMINI_ERRORS,
                 started: float | None = None, clock: Callable[[], float] = time.time,
                 now: Callable[[], datetime] = datetime.now, version: str = __version__):
        self.hub, self.bridge, self.ha, self.google, self.errors = hub, bridge, ha, google, errors
        self._clock, self.now, self.version = clock, now, version
        self.started = clock() if started is None else started

    async def _probe(self, coro) -> str:
        try:
            await asyncio.wait_for(coro, CHECK_TIMEOUT_S)
            return "на связи"
        except asyncio.TimeoutError:
            return "не отвечает (таймаут)"
        except Exception as e:
            return f"ошибка: {str(e)[:80]}"

    def _speaker(self) -> dict[str, Any]:
        dev = self.hub.get() if self.hub is not None else None
        if dev is None:
            return {"link": "не на связи"}
        return {"link": "на связи", "firmware": dev.hello.fw,
                "connected_min": int((self._clock() - dev.connected_at) // 60)}

    def _peers(self) -> dict[str, Any]:
        b = self.bridge
        if b is None:
            return {"phone": "мост выключен", "pc": "мост выключен"}
        out: dict[str, Any] = {"phone": "на связи" if b.phone is not None else "не на связи",
                               "pc": "на связи" if b.pc is not None else "не на связи"}
        return out

    def _batteries(self) -> list[str]:
        b = self.bridge
        out: list[str] = []
        if b is None:
            return out
        lvl = _level(b.phone_state.get("battery"))
        if lvl is not None:
            out.append(f"телефон {lvl} %" + (", заряжается" if b.phone_state.get("charging") else ""))
        if b.pc is not None:
            lvl = _level(b.pc.state.get("battery"))
            if lvl is not None:
                out.append(f"ноутбук {lvl} %" + (", заряжается" if b.pc.state.get("charging") else ""))
        for d in b.bt:
            lvl = _level(d.get("level"))
            if lvl is not None:
                out.append(f"{d.get('name', 'наушники')} {lvl} %")
        return out

    async def report(self) -> dict[str, Any]:
        errs = self.errors.count()
        out: dict[str, Any] = {
            "server_version": self.version,
            "uptime": uptime_words(self._clock() - self.started),
            "speaker": self._speaker(),
            **self._peers(),
            "gemini_errors_24h": sum(errs.values()),
            "batteries": self._batteries() or "данных нет",
        }
        if errs:
            out["gemini_errors_by_kind"] = errs
        if self.ha is None:
            out["home_assistant"] = "не настроен"
        else:
            out["home_assistant"] = await self._probe(self.ha.request("get_config"))
        if self.google is None:
            out["google"] = "не настроен"
        else:
            out["google"] = await self._probe(self.google.api("GET", "https://tasks.googleapis.com/tasks/v1/users/@me/lists",
                                                              {"maxResults": 1}))
        problems = [k for k in ("home_assistant", "google") if out[k] not in ("на связи", "не настроен")]
        if out["speaker"]["link"] != "на связи":
            problems.append("speaker")
        if out["gemini_errors_24h"] >= 5:
            problems.append("gemini")
        out["problems"] = problems
        return out

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "health_report":
            return await self.report()
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    async def command(self, line: str) -> str | None:
        """Консоль: `health` — тот же отчёт текстом."""
        if line.strip().lower() != "health":
            return None
        r = await self.report()
        return json.dumps(r, ensure_ascii=False, indent=1)
