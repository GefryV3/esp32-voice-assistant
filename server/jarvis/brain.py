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
from .google import TOOLS as GOOGLE_TOOLS, Google
from .agents import TOOLS as AGENT_TOOLS, Agents
from .ha import TOOLS as HOME_TOOLS, HomeAssistant
from .lights import TOOLS as LIGHT_TOOLS
from .games import TOOLS as GAME_TOOLS, Games
from .german import TOOLS as GERMAN_TOOLS, German
from .morning import TOOL_NAMES as MORNING_TOOL_NAMES

log = logging.getLogger(__name__)

MAX_TIMER_S = 24 * 3600
POLL_S = 5.0
RETRY_S = 20.0     # колонка занята разговором — попробуем напомнить чуть позже

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY, text TEXT NOT NULL, added TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, due TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, source TEXT NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

VOICES = ('Zephyr Puck Charon Kore Fenrir Leda Orus Aoede Callirrhoe Autonoe Enceladus Iapetus Umbriel Algieba Despina Erinome Algenib Rasalgethi Laomedeia Achernar Alnilam Schedar Gacrux Pulcherrima Achird Zubenelgenubi Vindemiatrix Sadachbia Sadaltager Sulafat').split()     # встроенные голоса Gemini


TOOLS: list[dict[str, Any]] = [
    {"name": "set_voice", "description": "Сменить свой голос по просьбе пользователя («смени голос», "
     "«говори живее/мягче/бодрее»). Голоса: Puck (бодрый), Fenrir (азартный), Zephyr (яркий), Orus "
     "(уверенный), Charon (спокойный), Kore (чёткий), Leda (молодой), Aoede (лёгкий), Enceladus "
     "(тихий), Algieba (плавный), Sulafat (тёплый) и другие. Новый голос с следующего разговора.",
     "parameters": {"type": "OBJECT", "properties": {"name": {"type": "STRING"}}, "required": ["name"]}},
    {"name": "set_affective", "description": "Включить или выключить эмоциональный режим голоса: "
     "модель слышит интонацию пользователя и отвечает в тон. enabled: on или off. Действует со "
     "следующего разговора; если после включения связь с Gemini пропала, выключи.",
     "parameters": {"type": "OBJECT", "properties": {"enabled": {"type": "STRING"}}, "required": ["enabled"]}},
    {"name": "set_presence", "description": "Пользователь сказал, что ушёл или вернулся. mode: "
     "away — «я ушёл», «меня нет» (колонка перестаёт отзываться на слово, напоминания и будильник "
     "работают); here — «я на месте»; auto — «определяй сам». Вернуть here голосом нельзя, пока "
     "away: колонка молчит, это можно сделать из Telegram или консоли.",
     "parameters": {"type": "OBJECT", "properties": {"mode": {"type": "STRING"}}, "required": ["mode"]}},
    {"name": "remember", "description": "Запомнить факт о пользователе или его просьбу надолго "
     "(имя, привычки, предпочтения, люди, планы). Один короткий факт на вызов.",
     "parameters": {"type": "OBJECT", "properties": {"fact": {"type": "STRING"}}, "required": ["fact"]}},
    {"name": "end_conversation", "description": "Вызови, когда по смыслу разговор закончен: "
     "пользователь поблагодарил, попрощался, сказал «всё», «отбой», «хватит», «ничего не надо». "
     "Сначала скажи короткое прощание словами, потом вызови: колонка уйдёт в ожидание и не будет "
     "ждать продолжения. Не вызывай, если пользователь явно собирается продолжать.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "mute_listening", "description": "Режим тишины «не слушай» на время: колонка совсем "
     "перестаёт слушать и отвечать на слово, вернуть можно только словами «Мага, вернись» или по "
     "истечении времени. minutes: на сколько минут (1…480), по умолчанию 60. Сначала скажи короткое "
     "подтверждение, потом вызови.",
     "parameters": {"type": "OBJECT", "properties": {"minutes": {"type": "NUMBER"}}}},
    {"name": "current_time", "description": "Точное текущее время и дата.",
     "parameters": {"type": "OBJECT", "properties": {}}},
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
    def __init__(self, path: Path | str, now: Callable[[], datetime] = datetime.now,
                 ha: HomeAssistant | None = None, agents: Agents | None = None, google: Google | None = None):
        self.now = now
        self.google = google
        self.ha = ha
        self.agents = agents
        self.presence = None     # Presence, ставит __main__
        self.lights = None       # Lights (jarvis.lights), ставит __main__
        self.mute_hook: Callable[[Any], Awaitable[dict]] | None = None   # Voice: режим тишины
        self.morning = None      # Morning (будильник по расписанию, автобус), ставит __main__
        self.extensions: list[Any] = []   # модули со своими функциями Gemini: .TOOLS, .tool_names, async .call(name, args)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.german = German(self.db, lambda: self.now(), self.pref, self.set_pref, lambda: self.google is not None)
        self.games = Games(self.db, lambda: self.now(), self.german)
        self._task: asyncio.Task | None = None
        self.on_message: Callable[[], None] | None = None   # зовётся после каждой записи в общую историю
        self.plan_changed: Callable[[], None] | None = None  # напоминания/таймеры поменялись: обновить план колонки
        self.timers: list[tuple[float, str]] = []            # (Unix-время срабатывания, подпись) — для плана колонки

    # ── общая история разговоров (голос и Telegram) ──
    #
    # Выбор: одна таблица на оба канала, а не по истории на канал. Реплики пишем сразу по ходу
    # (пара «вопрос — ответ» не теряется при обрыве). В подсказки обеих сторон попадает короткий
    # блок «Недавний разговор» (jarvis.voice.history_block), а не настоящие ходы сессии: Gemini Live
    # нельзя задним числом подложить историю, а блок в подсказке работает одинаково везде.

    def log_message(self, source: str, role: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.db.execute("INSERT INTO messages (ts, source, role, text) VALUES (?, ?, ?, ?)",
                        (self.now().isoformat(timespec="seconds"), source, role, text))
        self.db.commit()
        if self.on_message is not None:
            self.on_message()

    def recent_messages(self, limit: int = 12, max_age_h: float = 24) -> list[tuple[datetime, str, str, str]]:
        """Последние реплики, старые первыми: (время, источник, роль, текст). Старше max_age_h не берём."""
        since = (self.now() - timedelta(hours=max_age_h)).isoformat(timespec="seconds")
        rows = self.db.execute(
            "SELECT ts, source, role, text FROM messages WHERE ts >= ? ORDER BY id DESC LIMIT ?",
            (since, limit)).fetchall()
        return [(datetime.fromisoformat(ts), src, role, text) for ts, src, role, text in reversed(rows)]

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
        self._plan_changed()
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
        self._plan_changed()

    def cancel(self, needle: str) -> int:
        n = self.db.execute("UPDATE reminders SET done = 1 WHERE done = 0 AND "
                            "instr(lower(text), lower(?)) > 0", (needle,)).rowcount
        self.db.commit()
        self._plan_changed()
        return n

    # ── план для колонки без связи (docs/protocol-next.md, plan) ──

    def _plan_changed(self) -> None:
        if self.plan_changed is not None:
            self.plan_changed()

    def plan_rows(self) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
        """Ближайшие напоминания и таймеры как (Unix-время, подпись ≤31 байта), по возрастанию."""
        now = self.now().replace(tzinfo=None)
        stamp = now.timestamp()
        self.timers = [(at, label) for at, label in self.timers if at > stamp]

        def fit(text: str) -> str:
            return text.strip().encode()[:31].decode(errors="ignore")

        alarms = sorted((int(d.timestamp()), fit(t)) for _, t, d in self.pending() if d > now)
        timers = sorted((int(at), fit(label or "Таймер")) for at, label in self.timers)
        return alarms, timers

    # ── инструменты Gemini ──

    def pref(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM prefs WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_pref(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO prefs (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        self.db.commit()

    def tools(self) -> list[dict[str, Any]]:
        extra = [t for ext in self.extensions for t in ext.TOOLS]
        return (TOOLS + GERMAN_TOOLS + GAME_TOOLS + (HOME_TOOLS if self.ha else []) + (AGENT_TOOLS if self.agents else [])
                + (GOOGLE_TOOLS if self.google else []) + (LIGHT_TOOLS if self.lights else [])
                + (self.morning.tools() if self.morning else []) + extra)

    async def call(self, name: str, args: dict[str, Any], send: Callable[[dict], Awaitable[None]]) -> dict:
        """Выполнить инструмент; результат — короткий словарь для Gemini. Ошибки не роняют разговор."""
        try:
            return await self._call(name, args, send)
        except Exception as e:
            log.warning("инструмент %s(%s): %r", name, args, e)
            return {"ok": False, "error": f"не вышло: {e}"}

    async def _call(self, name: str, args: dict[str, Any], send: Callable[[dict], Awaitable[None]]) -> dict:
        now = self.now().replace(tzinfo=None)
        if name in ("goodnight", "music_light") and self.lights:
            return await self.lights.call(name, args)
        for ext in self.extensions:
            if name in ext.tool_names:
                return await ext.call(name, args)
        if name == "save_idea" and self.google:
            return await self.google.call(name, args)
        if name.startswith("home_") and self.ha:
            return await self.ha.call(name, args, send)
        if name in MORNING_TOOL_NAMES and self.morning:
            return await self.morning.call(name, args)
        if name.startswith("google_") and self.google:
            return await self.google.call(name, args)
        if name in ("start_task", "list_tasks", "cancel_task") and self.agents:
            return await self.agents.call(name, args)
        if name in self.german.names:
            return await self.german.call(name, args)
        if name in self.games.names:
            return await self.games.call(name, args, send)
        if name == "set_voice":
            wanted = str(args.get("name", "")).strip().casefold()
            match = next((v for v in VOICES if v.casefold() == wanted), None)
            if match is None:
                return {"ok": False, "error": "такого голоса нет", "available": VOICES}
            self.set_pref("voice", match)
            return {"ok": True, "voice": match, "note": "новый голос со следующего разговора"}
        if name == "set_affective":
            on = str(args.get("enabled", "")).strip().casefold() in ("on", "true", "1", "да", "вкл")
            self.set_pref("affective", "1" if on else "0")
            return {"ok": True, "affective": on, "note": "со следующего разговора"}
        if name == "set_presence":
            if self.presence is None:
                return {"ok": False, "error": "проверка присутствия выключена"}
            mode = {"away": "away", "ушёл": "away", "here": "here", "на месте": "here", "auto": "auto"}.get(
                str(args.get("mode", "")).strip().casefold(), "")
            if not mode:
                return {"ok": False, "error": "mode: away, here или auto"}
            self.presence.set_override(mode)
            return {"ok": True, "mode": mode, "note": "away: колонка отзовётся снова после presence here из Telegram"}
        if name == "mute_listening":
            if self.mute_hook is None:
                return {"ok": False, "error": "голоса нет, режим тишины недоступен"}
            return await self.mute_hook(args.get("minutes"))
        if name == "current_time":
            return {"time": f"{now:%H:%M}", "date": f"{now:%d.%m.%Y}", "weekday": now.strftime("%A")}
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
            self.timers = [(now.timestamp() + sec, str(args.get("label", "")))] if sec > 0 else []
            self._plan_changed()
            await send(protocol.timer(max(sec, 0), str(args.get("label", ""))))
            return {"ok": True, "seconds": max(sec, 0)}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    def end_conversation(self) -> None:
        """Разговор кончился: режим немецкого и игра живут только на один разговор."""
        self.german.reset()
        self.games.reset()

    @property
    def long_talk(self) -> bool:
        """Идёт немецкий или игра: паузы между репликами длиннее (человек думает над ответом)."""
        return self.german.active or self.games.active

    async def command(self, line: str) -> str | None:
        cmd, _, rest = line.strip().partition(" ")
        if cmd.lower() == "de":
            return await self.german.command(rest)
        if cmd.lower() == "game":
            return await self.games.command(rest)
        return None

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
