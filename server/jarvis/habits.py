"""Привычки с колкостью (идея #5): колонка иногда сама спрашивает, сделано ли дело.

Как рутины: «когда» решает этот модуль, а сам вопрос задаёт Gemini через Voice.announce(подсказка).
Раз в день бросаем кубик (вероятность HABITS_PROB, по умолчанию 0.5): спрашивать ли сегодня, и в какое
из окон (по умолчанию 18:00–21:30) — случайная минута. В назначенное время берём невыполненные
задачи Google Задач (google_tasks_list): сначала те, что совпали со словами привычек («тренировка»,
«бассейн»…), иначе любую; чем больше пропусков подряд, тем вероятнее выбор. Один вопрос в день.

Ответ записывает сам Gemini функцией habit_answer(task, done): «да» → задача отмечается
выполненной в Google (google_tasks_done), «нет» → в журнал идёт пропуск. Журнал — таблица
habit_log в brain.db; по ней считаются пропуски подряд и недельная сводка (digest.py). Когда
пропусков два и больше, в подсказку добавляется колкость в меру ползунка «Язвительность»
(bridge sarcasm 0…100): ниже 30 — без подколов вообще.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from .bridge import _minutes, _valid_time, in_window

log = logging.getLogger(__name__)

TICK_S = 60.0
RETRY_S = 30.0
GIVE_UP_S = 3600.0          # вопрос через час после назначенного уже не нужен
DEFAULT_WINDOWS = "18:00-21:30"
DEFAULT_KEYWORDS = "тренировк,бассейн,хомяк,немецк,зарядк,спорт,гантел,растяжк"
PLAN_KEY = "habits_plan"

SCHEMA = "CREATE TABLE IF NOT EXISTS habit_log (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, " \
         "task TEXT NOT NULL, done INTEGER NOT NULL)"

TOOLS: list[dict[str, Any]] = [
    {"name": "habit_answer", "description": "Ответ пользователя на твой вопрос «это сделано?» про привычку или "
     "задачу. done=true — сделал (задача отметится выполненной в Google), done=false — нет. Вызови один "
     "раз, сразу как понял ответ. task — название задачи ровно так, как она названа в вопросе.",
     "parameters": {"type": "OBJECT", "properties": {"task": {"type": "STRING"}, "done": {"type": "BOOLEAN"}},
                    "required": ["task", "done"]}},
]


def parse_windows(raw: str) -> list[tuple[str, str]]:
    """«18:00-21:30, 12:00-13:00» → [("18:00","21:30"), …]; кривое пропускаем."""
    out = []
    for part in raw.replace(";", ",").split(","):
        a, _, b = part.strip().partition("-")
        if _valid_time(a.strip()) and _valid_time(b.strip()) and _minutes(a.strip()) < _minutes(b.strip()):
            out.append((a.strip(), b.strip()))
    return out


def parse_keywords(raw: str) -> tuple[str, ...]:
    return tuple(k.strip().casefold() for k in raw.split(",") if k.strip())


def sharpness(sarcasm: int, misses: int) -> str:
    """Что сказать про пропуски: пусто, пока их меньше двух или ползунок почти на нуле."""
    if misses < 2 or sarcasm < 30:
        return ""
    if sarcasm < 70:
        return (f"Это уже {misses}-й раз подряд, когда дело не сделано. Одной короткой мягкой подколкой "
                "дай понять, что ты это заметил.")
    return (f"Это уже {misses}-й раз подряд, дело не сделано. Подколи его по-настоящему, остро и смешно, "
            "в одну фразу (над делом, не над человеком: без обидного).")


class Habits:
    def __init__(self, announce: Callable[[str], Awaitable[bool]], *, google, brain,
                 settings: Callable[[], dict[str, Any]], prob: float = 0.5,
                 windows: list[tuple[str, str]] | None = None, keywords: tuple[str, ...] | None = None,
                 now: Callable[[], datetime] = datetime.now, rng: random.Random | None = None,
                 clock: Callable[[], float] | None = None):
        self.announce, self.google, self.brain, self.settings = announce, google, brain, settings
        self.prob = min(max(prob, 0.0), 1.0)
        self.windows = windows if windows is not None else parse_windows(DEFAULT_WINDOWS)
        self.keywords = keywords if keywords is not None else parse_keywords(DEFAULT_KEYWORDS)
        self.now, self.rng = now, rng or random.Random()
        self._clock = clock or asyncio.get_running_loop().time
        self._due_since: float | None = None
        self._task: asyncio.Task | None = None
        brain.db.execute(SCHEMA)
        brain.db.commit()
        self.tool_names = frozenset(t["name"] for t in TOOLS)
        self.TOOLS = TOOLS

    # ── журнал ──

    def log_answer(self, task: str, done: bool) -> None:
        self.brain.db.execute("INSERT INTO habit_log (ts, task, done) VALUES (?, ?, ?)",
                              (self.now().isoformat(timespec="seconds"), task.strip(), int(done)))
        self.brain.db.commit()

    def misses(self, task: str) -> int:
        """Сколько раз подряд (последние ответы) задача была «нет»."""
        # регистр сравниваем в Python: lower() в SQLite не знает кириллицы
        want = task.strip().casefold()
        n = 0
        for name, done in self.brain.db.execute("SELECT task, done FROM habit_log ORDER BY id DESC LIMIT 300"):
            if name.casefold() != want:
                continue
            if done:
                break
            n += 1
        return n

    def week_stats(self, days: int = 7) -> dict[str, list[str]]:
        since = (self.now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = self.brain.db.execute("SELECT task, done FROM habit_log WHERE ts >= ? ORDER BY id", (since,))
        out: dict[str, list[str]] = {"done": [], "missed": []}
        for task, done in rows:
            out["done" if done else "missed"].append(task)
        return out

    # ── план дня ──

    def _plan(self) -> dict[str, Any]:
        day = self.now().strftime("%Y-%m-%d")
        try:
            plan = json.loads(self.brain.pref(PLAN_KEY, "{}"))
        except ValueError:
            plan = {}
        if plan.get("day") == day:
            return plan
        at = ""
        if self.windows and self.rng.random() < self.prob:
            start, end = self.rng.choice(self.windows)
            m = self.rng.randrange(_minutes(start), _minutes(end))
            at = f"{m // 60:02d}:{m % 60:02d}"
        plan = {"day": day, "at": at, "state": "wait" if at else "skip"}
        self._save(plan)
        log.info("привычки: на сегодня %s", f"вопрос в {at}" if at else "вопроса нет")
        return plan

    def _save(self, plan: dict[str, Any]) -> None:
        self.brain.set_pref(PLAN_KEY, json.dumps(plan))

    def night(self) -> bool:
        local, v = self.now(), self.settings()
        return in_window(local.hour * 60 + local.minute, v["night_start"], v["night_end"])

    # ── выбор и вопрос ──

    def pick(self, titles: list[str]) -> str | None:
        if not titles:
            return None
        habit = [t for t in titles if any(k in t.casefold() for k in self.keywords)]
        pool = habit or titles
        weights = [1 + self.misses(t) for t in pool]
        return self.rng.choices(pool, weights)[0]

    def prompt(self, task: str) -> str:
        misses = self.misses(task)
        jibe = sharpness(int(self.settings().get("sarcasm", 60)), misses)
        return (f"Вечер, спроси пользователя про одно дело из его списка задач: «{task}». Один короткий "
                "вопрос в своей манере, например «… сегодня была?» по смыслу задачи (не зачитывай список "
                "и не спрашивай про другие дела). Дождись ответа. Как понял: сделал → вызови "
                f"habit_answer(task=\"{task}\", done=true) и коротко порадуйся; не сделал → вызови "
                f"habit_answer(task=\"{task}\", done=false) и ответь одной фразой, без нотаций. "
                "Если он ответил невпопад или сказал «потом» — ничего не записывай. "
                + (jibe + " " if jibe else "") + "Больше ничего не спрашивай.")

    async def ask_step(self, force: bool = False) -> float:
        """Один шаг планировщика; возвращает паузу до следующего."""
        plan = self._plan()
        if force:
            plan = {**plan, "state": "wait", "at": "00:00"}
        if plan["state"] != "wait":
            self._due_since = None
            return TICK_S
        local = self.now()
        if not force and (local.strftime("%H:%M") < plan["at"] or self.night()):
            return TICK_S
        now = self._clock()
        if self._due_since is None:
            self._due_since = now
        if not force and now - self._due_since > GIVE_UP_S:
            return self._close(plan, "время вышло")
        try:
            titles = (await self.google.tasks_list()).get("tasks", [])
        except Exception as e:
            log.warning("привычки: задачи Google недоступны: %s", e)
            return RETRY_S
        task = self.pick(titles)
        if task is None:
            return self._close(plan, "невыполненных задач нет", force)
        if await self.announce(self.prompt(task)):
            log.info("привычки: спросил про «%s»", task)
            return self._close(plan, "спросил", force)
        return RETRY_S

    def _close(self, plan: dict[str, Any], why: str, force: bool = False) -> float:
        log.info("привычки: %s", why)
        self._due_since = None
        if not force:
            self._save({**plan, "state": "done"})
        return TICK_S

    async def _loop(self) -> None:
        while True:
            try:
                delay = await self.ask_step()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("привычки")
                delay = TICK_S
            await asyncio.sleep(delay)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── вход для Gemini и консоль ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name != "habit_answer":
            return {"ok": False, "error": f"нет такого инструмента: {name}"}
        task = str(args.get("task", "")).strip()
        done = args.get("done") in (True, "true", "True", 1)
        if not task:
            return {"ok": False, "error": "не сказано, про какое дело"}
        self.log_answer(task, done)
        if not done:
            return {"ok": True, "done": False, "misses_in_row": self.misses(task)}
        marked: list[str] = []
        try:
            marked = (await self.google.tasks_done(task)).get("done", [])
        except Exception as e:
            log.warning("привычки: задача не отметилась: %s", e)
        return {"ok": True, "done": True, "marked_in_google": marked}

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "habits":
            return None
        if len(parts) > 1 and parts[1].lower() == "ask":
            await self.ask_step(force=True)
            return "привычки: вопрос задан (если есть невыполненные задачи и колонка свободна)"
        plan = self._plan()
        return (f"привычки: сегодня {'вопрос в ' + plan['at'] if plan['at'] else 'вопроса нет'} "
                f"({plan['state']}); habits ask — спросить прямо сейчас")
