"""Фоновые агенты: долгие задачи, которые идут, пока с колонкой можно говорить о другом.

Gemini зовёт start_task(goal): мы сразу отвечаем «взялся», а задача уходит в отдельный запрос
к Gemini с поиском в интернете (research, новости, сравнения, сводки). Готовый результат
колонка озвучивает сама, как только свободна. Задачи живут в памяти: перезапуск сервера их
обрывает (для минутных задач этого достаточно).
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_RUNNING = 3
TASK_TIMEOUT_S = 180.0
RETRY_S = 20.0           # колонка занята разговором — озвучим чуть позже
GIVE_UP_S = 3600.0       # через час результат уже не нужен
KEEP_DONE = 10

SYSTEM = ("Ты исследователь-помощник. Выполни задачу, при необходимости найди свежие факты в "
          "интернете. Ответ прозвучит вслух: по-русски, 3-6 коротких предложений простым "
          "разговорным языком, без списков, ссылок, сносок и разметки; числа словами не расписывай.")

TOOLS: list[dict[str, Any]] = [
    {"name": "start_task", "description": "Запустить долгую задачу в фоне (поиск в интернете, "
     "новости, сравнение, подборка, исследование), пока разговор продолжается. Результат "
     "озвучит сама колонка, когда будет готов. Для быстрых вопросов не нужна: отвечай сразу.",
     "parameters": {"type": "OBJECT", "properties": {
         "goal": {"type": "STRING", "description": "Что именно сделать, подробно и самодостаточно"},
         "title": {"type": "STRING", "description": "Название в 2-4 слова"}}, "required": ["goal"]}},
    {"name": "list_tasks", "description": "Какие фоновые задачи идут и что готово.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "cancel_task", "description": "Отменить фоновую задачу, в названии которой есть подстрока.",
     "parameters": {"type": "OBJECT", "properties": {"title": {"type": "STRING"}}, "required": ["title"]}},
]


@dataclass
class Task:
    id: int
    title: str
    goal: str
    status: str = "running"      # running, done, failed, cancelled
    result: str = ""
    spoken: bool = False
    runner: asyncio.Task | None = field(default=None, repr=False)


def generate_sync(api_key: str, model: str, goal: str) -> str:
    """Один запрос к Gemini с поиском Google; вызывается в потоке."""
    body = {"systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": goal}]}],
            "tools": [{"google_search": {}}]}
    req = urllib.request.Request(API_URL.format(model=model), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=TASK_TIMEOUT_S) as r:
        data = json.load(r)
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


class Agents:
    def __init__(self, api_key: str, model: str, announce: Callable[[str], Awaitable[bool]], *,
                 generate: Callable[[str], Awaitable[str]] | None = None, clock=None):
        """announce(prompt) → True, если колонка взялась озвучить (иначе повторим позже)."""
        self.announce = announce
        self.generate = generate or (lambda goal: asyncio.to_thread(generate_sync, api_key, model, goal))
        self.tasks: list[Task] = []
        self._next = 1
        self._loop_task: asyncio.Task | None = None
        self._wake = asyncio.Event()      # задача закончилась — пора озвучивать
        self._clock = clock or asyncio.get_running_loop().time

    # ── инструменты Gemini ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "start_task":
            return self.start(str(args.get("goal", "")), str(args.get("title", "")))
        if name == "list_tasks":
            return {"tasks": [{"title": t.title, "status": t.status} for t in self.tasks]}
        if name == "cancel_task":
            needle = str(args.get("title", "")).casefold()
            hit = [t for t in self.tasks if t.status == "running" and needle in t.title.casefold()]
            for t in hit:
                t.status = "cancelled"
                if t.runner:
                    t.runner.cancel()
            return {"ok": True, "cancelled": len(hit)}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    def start(self, goal: str, title: str = "") -> dict:
        goal = goal.strip()
        if not goal:
            return {"ok": False, "error": "не сказано, что делать"}
        if sum(t.status == "running" for t in self.tasks) >= MAX_RUNNING:
            return {"ok": False, "error": f"уже идёт {MAX_RUNNING} задачи, подожди"}
        task = Task(self._next, title.strip() or goal[:40], goal)
        self._next += 1
        self.tasks.append(task)
        task.runner = asyncio.create_task(self._work(task))
        self._trim()
        return {"ok": True, "note": "работает в фоне, результат озвучу сам, когда будет готов"}

    def _trim(self) -> None:
        done = [t for t in self.tasks if t.status != "running"]
        for t in done[:-KEEP_DONE]:
            self.tasks.remove(t)

    # ── выполнение и доставка ──

    async def _work(self, task: Task) -> None:
        try:
            async with asyncio.timeout(TASK_TIMEOUT_S):
                task.result = (await self.generate(task.goal)).strip()
            task.status = "done" if task.result else "failed"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("задача «%s»: %r", task.title, e)
            task.status = "failed"
        if task.status == "failed":
            task.result = ""
        log.info("задача «%s»: %s (%d симв.)", task.title, task.status, len(task.result))
        self._wake.set()

    def start_delivery(self) -> None:
        self._loop_task = asyncio.create_task(self._deliver_loop())

    async def stop(self) -> None:
        for t in self.tasks:
            if t.runner:
                t.runner.cancel()
        if self._loop_task:
            self._loop_task.cancel()
        await asyncio.gather(*(t.runner for t in self.tasks if t.runner),
                             *([self._loop_task] if self._loop_task else []), return_exceptions=True)

    def prompt_for(self, task: Task) -> str:
        if task.status == "failed":
            return (f"Фоновая задача «{task.title}» не получилась. Коротко скажи пользователю, "
                    "что не вышло, и предложи попробовать ещё раз.")
        return (f"Фоновая задача «{task.title}» готова. Результат:\n{task.result}\n"
                "Перескажи пользователю суть своими словами, коротко и разговорно.")

    async def _deliver_loop(self) -> None:
        waiting_since: dict[int, float] = {}
        while True:
            delay = 60.0
            self._wake.clear()
            for t in self.tasks:
                if t.status in ("done", "failed") and not t.spoken:
                    waiting_since.setdefault(t.id, self._clock())
                    if self._clock() - waiting_since[t.id] > GIVE_UP_S:
                        t.spoken = True
                    elif await self.announce(self.prompt_for(t)):
                        t.spoken = True
                    else:
                        delay = RETRY_S
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except TimeoutError:
                pass
