"""Недельная сводка и резерв (идеи #29, #34): воскресным вечером в Telegram.

Одним сообщением: сколько было разговоров, что нового Мага запомнил, какие дела сделаны и
пропущены (ответы на вопросы привычек + выполненное в Google Задачах), как спал (если мост
считал) и, если подключена, строка от «самообучения» (`learning_digest`, её даёт другая сессия:
сюда достаточно присвоить `digest.learning_digest = функция`, обычная или async, возвращающая
текст или None). Следом тем же чатом уходит файл-резерв brain.db: копия через sqlite3 backup API
(живую базу копировать файлом нельзя: может разойтись с журналом), во временную папку, потом
sendDocument, потом свежая копия остаётся в data/backup, три последних.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .bridge import _minutes, _valid_time

log = logging.getLogger(__name__)

TICK_S = 60.0
WEEKDAY = 6                  # воскресенье
CONVERSATION_GAP_MIN = 20    # пауза между репликами больше этой — уже новый разговор
KEEP_BACKUPS = 3
LATE_H = 6                   # опоздать со сводкой можно не более чем на столько часов
MAX_FACTS = 8
PREF_KEY = "digest_week_sent"

SCHEMA = "CREATE TABLE IF NOT EXISTS sleep_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, hours REAL NOT NULL, lay TEXT NOT NULL)"


def hours_words(h: float) -> str:
    m = round(h * 60)
    return f"{m // 60} ч {m % 60:02d} мин"


def _night_key(hhmm: str) -> int:
    """Минуты от полудня: отбой в 01:30 позже, чем в 23:00."""
    m = _minutes(hhmm)
    return m + 1440 if m < 12 * 60 else m


class Digest:
    def __init__(self, telegram, brain, *, google=None, habits=None, backup_dir: Path | str = "data/backup",
                 at: str = "19:00", now: Callable[[], datetime] = datetime.now):
        self.tg, self.brain, self.google, self.habits = telegram, brain, google, habits
        self.backup_dir = Path(backup_dir)
        self.at = at if _valid_time(at) else "19:00"
        self.now = now
        self.learning_digest: Callable[[], Any] | None = None    # точка подключения (alive-learning)
        self._task: asyncio.Task | None = None
        brain.db.execute(SCHEMA)
        brain.db.commit()

    # ── данные ──

    def record_sleep(self, hours: float, lay: datetime) -> None:
        """Звать, когда мост посчитал сон (будильник выключен)."""
        self.brain.db.execute("INSERT INTO sleep_log (ts, hours, lay) VALUES (?, ?, ?)",
                              (self.now().isoformat(timespec="seconds"), hours, lay.isoformat(timespec="minutes")))
        self.brain.db.commit()

    def _since(self, days: int = 7) -> str:
        return (self.now() - timedelta(days=days)).isoformat(timespec="seconds")

    def conversations(self) -> tuple[int, int]:
        """(разговоров, реплик пользователя) за неделю. Разговор — серия реплик с паузами < 20 мин."""
        rows = self.brain.db.execute("SELECT ts FROM messages WHERE role = 'user' AND ts >= ? ORDER BY id",
                                     (self._since(),)).fetchall()
        n, prev = 0, None
        for (ts,) in rows:
            t = datetime.fromisoformat(ts)
            if prev is None or t - prev > timedelta(minutes=CONVERSATION_GAP_MIN):
                n += 1
            prev = t
        return n, len(rows)

    def new_facts(self) -> list[str]:
        return [r[0] for r in self.brain.db.execute("SELECT text FROM facts WHERE added >= ? ORDER BY id",
                                                    (self._since(),))]

    def sleep(self) -> list[tuple[float, str]]:
        return [(h, lay) for h, lay in self.brain.db.execute(
            "SELECT hours, lay FROM sleep_log WHERE ts >= ? ORDER BY id", (self._since(),))]

    async def google_tasks(self) -> tuple[int, int] | None:
        """(выполнено за неделю, ещё открыто) по Google Задачам; None — Google нет или не ответил."""
        if self.google is None:
            return None
        try:
            done = await self.google.api("GET", "https://tasks.googleapis.com/tasks/v1/lists/@default/tasks", {
                "showCompleted": "true", "showHidden": "true", "maxResults": 100,
                "completedMin": self._since() + "Z"})
            n_done = sum(1 for t in done.get("items", []) if t.get("status") == "completed")
            n_open = len((await self.google.tasks_list()).get("tasks", []))
            return n_done, n_open
        except Exception as e:
            log.warning("сводка: задачи Google недоступны: %s", e)
            return None

    # ── текст ──

    async def build(self) -> str:
        convs, replies = self.conversations()
        lines = [f"Неделя с Магой ({self.now():%d.%m})", ""]
        lines.append(f"Разговоров: {convs} (реплик от тебя: {replies})" if convs else "Разговоров на этой неделе не было.")
        facts = self.new_facts()
        if facts:
            lines.append("Запомнил нового: " + "; ".join(facts[:MAX_FACTS]) + (f" и ещё {len(facts) - MAX_FACTS}"
                                                                           if len(facts) > MAX_FACTS else ""))
        else:
            lines.append("Нового не запомнил.")
        if self.habits is not None:
            stats = self.habits.week_stats()
            if stats["done"] or stats["missed"]:
                lines.append(f"Привычки: сделано {len(stats['done'])}"
                             + (f" ({', '.join(sorted(set(stats['done'])))})" if stats["done"] else "")
                             + f", пропущено {len(stats['missed'])}"
                             + (f" ({', '.join(sorted(set(stats['missed'])))})" if stats["missed"] else ""))
        tasks = await self.google_tasks()
        if tasks is not None:
            lines.append(f"Задачи Google: выполнено за неделю {tasks[0]}, открыто сейчас {tasks[1]}")
        sleep = self.sleep()
        if sleep:
            avg = sum(h for h, _ in sleep) / len(sleep)
            late = max((lay[11:16] for _, lay in sleep), key=_night_key)
            lines.append(f"Сон: в среднем {hours_words(avg)} за {len(sleep)} ноч., самый поздний отбой {late}")
        extra = await self._learning()
        if extra:
            lines += ["", extra]
        lines += ["", "Копия памяти (brain.db) ниже файлом."]
        return "\n".join(lines)

    async def _learning(self) -> str:
        fn = self.learning_digest
        if fn is None:
            return ""
        try:
            out = fn()
            if inspect.isawaitable(out):
                out = await out
            return str(out).strip() if out else ""
        except Exception:
            log.exception("сводка: learning_digest упала")
            return ""

    # ── резерв ──

    def backup(self) -> Path:
        """Согласованная копия brain.db через backup API; старые копии подчищаются."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        dest = self.backup_dir / f"brain-{self.now():%Y-%m-%d}.db"
        out = sqlite3.connect(dest)
        try:
            self.brain.db.backup(out)
        finally:
            out.close()
        for old in sorted(self.backup_dir.glob("brain-*.db"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        return dest

    # ── отправка ──

    async def send_now(self) -> bool:
        text = await self.build()
        ok = await self.tg.send(text)
        try:
            path = await asyncio.to_thread(self.backup)
            data = await asyncio.to_thread(path.read_bytes)
            await self.tg.send_document(path.name, data, f"Резерв памяти, {self.now():%d.%m.%Y}")
        except Exception:
            log.exception("сводка: резерв не отправился")
            await self.tg.send("Резерв brain.db сделать не вышло, подробности в логе.")
        return ok

    def due(self) -> bool:
        local = self.now()
        if local.weekday() != WEEKDAY or self.brain.pref(PREF_KEY) == local.strftime("%Y-%m-%d"):
            return False
        minutes = local.hour * 60 + local.minute
        start = _minutes(self.at)
        return start <= minutes < start + LATE_H * 60

    async def step(self) -> None:
        if self.due() and await self.send_now():
            self.brain.set_pref(PREF_KEY, self.now().strftime("%Y-%m-%d"))

    async def _loop(self) -> None:
        while True:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("сводка")
            await asyncio.sleep(TICK_S)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if len(parts) < 1 or parts[0].lower() != "digest":
            return None
        if len(parts) > 1 and parts[1].lower() == "send":
            return "сводка отправлена в Telegram" if await self.send_now() else "сводка: Telegram не принял"
        return await self.build()
