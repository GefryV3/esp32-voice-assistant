"""Мозг: память (факты) и напоминания в SQLite + инструменты, которые вызывает Gemini.

Gemini сам решает, когда «запомнить», «напомнить» или «поставить таймер», и зовёт функцию;
мы выполняем её и возвращаем короткий результат, который он проговаривает. Время везде
местное (то же, что `now()` голоса), без часовых поясов в базе.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import protocol

log = logging.getLogger(__name__)

MAX_TIMER_S = 24 * 3600
POLL_S = 5.0
RETRY_S = 20.0     # колонка занята разговором — попробуем напомнить чуть позже

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL, added TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, due TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "remember", "description": "Запомнить факт о пользователе или его просьбу надолго "
     "(имя, привычки, предпочтения, люди, планы). Один короткий факт на вызов.",
     "parameters": {"type": "OBJECT", "properties": {"fact": {"type": "STRING"}}, "required": ["fact"]}},
    {"name": "forget", "description": "Забыть факты, в тексте которых есть эта подстрока.",
     "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}, "required": ["text"]}},
    {"name": "add_reminder", "description": "Поставить напоминание. Укажи либо minutes (через сколько "
     "минут), либо at (местное время, формат YYYY-MM-DDTHH:MM).",
     "parameters": {"type": "OBJECT", "properties": {
         "text": {"type": "STRING", "description": "О чём напомнить, например «позвонить маме»"},
         "minutes": {"type": "NUMBER"}, "at": {"type": "STRING"}}, "required": ["text"]}},
    {"name": "list_reminders", "description": "Какие напоминания ещё не сработали.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "cancel_reminder", "description": "Отменить напоминание, в тексте которого есть подстрока.",
     "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}, "required": ["text"]}},
    {"name": "set_timer", "description": "Запустить таймер обратного отсчёта на экране колонки; "
     "minutes = 0 — снять таймер.",
     "parameters": {"type": "OBJECT", "properties": {
         "minutes": {"type": "NUMBER"}, "label": {"type": "STRING"}}, "required": ["minutes"]}},
]


def when_words(due: datetime, now: datetime) -> str:
    if due.date() == now.date():
        return f"сегодня в {due:%H:%M}"
    if due.date() == (now + timedelta(days=1)).date():
        return f"завтра в {due:%H:%M}"
    return f"{due:%d.%m} в {due:%H:%M}"


class Brain:
    def __init__(self, path: Path | str, now: Callable[[], datetime] = datetime.now):
        self.now = now
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._task: asyncio.Task | None = None

    # ── факты ──

    def facts(self) -> list[str]:
        return [r[0] for r in self.db.execute("SELECT text FROM facts ORDER BY id")]

    def remember(self, fact: str) -> bool:
        fact = fact.strip()
        if not fact or fact.lower() in (f.lower() for f in self.facts()):
            return False
        self.db.execute("INSERT INTO facts (text, added) VALUES (?, ?)",
                        (fact, self.now().isoformat(timespec="seconds")))
        self.db.commit()
        return True

    def forget(self, needle: str) -> int:
        n = self.db.execute("DELETE FROM facts WHERE instr(lower(text), lower(?)) > 0", (needle,)).rowcount
        self.db.commit()
        return n

    # ── напоминания ──

    def add_reminder(self, text: str, due: datetime) -> int:
        cur = self.db.execute("INSERT INTO reminders (text, due) VALUES (?, ?)",
                              (text.strip(), due.isoformat(timespec="minutes")))
        self.db.commit()
        return cur.lastrowid or 0

    def pending(self) -> list[tuple[int, str, datetime]]:
        rows = self.db.execute("SELECT id, text, due FROM reminders WHERE done = 0 ORDER BY due")
        return [(i, t, datetime.fromisoformat(d)) for i, t, d in rows]

    def due(self) -> list[tuple[int, str]]:
        now = self.now().replace(tzinfo=None)
        return [(i, t) for i, t, d in self.pending() if d <= now]

    def finish(self, rid: int) -> None:
        self.db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (rid,))
        self.db.commit()

    def cancel(self, needle: str) -> int:
        n = self.db.execute("UPDATE reminders SET done = 1 WHERE done = 0 AND "
                            "instr(lower(text), lower(?)) > 0", (needle,)).rowcount
        self.db.commit()
        return n

    # ── инструменты Gemini ──

    async def call(self, name: str, args: dict[str, Any], send: Callable[[dict], Awaitable[None]]) -> dict:
        """Выполнить инструмент; результат — короткий словарь для Gemini. Ошибки не роняют разговор."""
        try:
            return await self._call(name, args, send)
        except Exception as e:
            log.warning("инструмент %s(%s): %r", name, args, e)
            return {"ok": False, "error": f"не вышло: {e}"}

    async def _call(self, name: str, args: dict[str, Any], send: Callable[[dict], Awaitable[None]]) -> dict:
        now = self.now().replace(tzinfo=None)
        if name == "remember":
            return {"ok": True, "new": self.remember(str(args.get("fact", "")))}
        if name == "forget":
            return {"ok": True, "forgotten": self.forget(str(args.get("text", "")))}
        if name == "add_reminder":
            text = str(args.get("text", "")).strip()
            if not text:
                return {"ok": False, "error": "не сказано, о чём напомнить"}
            if args.get("minutes") is not None:
                due = now + timedelta(minutes=float(args["minutes"]))
            elif args.get("at"):
                due = datetime.fromisoformat(str(args["at"])).replace(tzinfo=None)
            else:
                return {"ok": False, "error": "не сказано, когда напомнить"}
            if due <= now:
                return {"ok": False, "error": "это время уже прошло"}
            self.add_reminder(text, due)
            await send(protocol.reminders(len(self.pending())))
            return {"ok": True, "when": when_words(due, now)}
        if name == "list_reminders":
            return {"reminders": [{"text": t, "when": when_words(d, now)} for _, t, d in self.pending()]}
        if name == "cancel_reminder":
            n = self.cancel(str(args.get("text", "")))
            await send(protocol.reminders(len(self.pending())))
            return {"ok": True, "cancelled": n}
        if name == "set_timer":
            sec = min(int(float(args.get("minutes", 0)) * 60), MAX_TIMER_S)
            await send(protocol.timer(max(sec, 0), str(args.get("label", ""))))
            return {"ok": True, "seconds": max(sec, 0)}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── планировщик: напомнить в срок ──

    def start(self, remind: Callable[[str], Awaitable[bool]]) -> None:
        """remind(text) → True, если колонка сказала напоминание (иначе повторим позже)."""
        self._task = asyncio.create_task(self._loop(remind))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self, remind: Callable[[str], Awaitable[bool]]) -> None:
        while True:
            delay = POLL_S
            for rid, text in self.due():
                if await remind(text):
                    self.finish(rid)
                else:
                    delay = RETRY_S
            await asyncio.sleep(delay)
