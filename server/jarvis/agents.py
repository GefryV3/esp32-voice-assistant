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
import html
import re
import urllib.parse
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
    error: str = ""
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


SEARCH_URL = "https://html.duckduckgo.com/html/?"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_RESULT = re.compile(r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
                     r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', re.S)


def _text(raw: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).strip()


def web_search(query: str, count: int = 6) -> list[tuple[str, str, str]]:
    """Выдача DuckDuckGo без ключа и без квоты Google: (заголовок, адрес, отрывок)."""
    req = urllib.request.Request(SEARCH_URL + urllib.parse.urlencode({"q": query, "kl": "ru-ru"}),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as r:
        page = r.read().decode("utf-8", "replace")
    out = []
    for url, title, snippet in _RESULT.findall(page):
        url = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [url])[0]
        out.append((_text(title), url, _text(snippet)))
        if len(out) >= count:
            break
    return out


def brave_search(api_key: str, query: str, count: int = 6) -> list[tuple[str, str, str]]:
    """Brave Search API (бесплатно ~2000 запросов в месяц по ключу): надёжнее, чем разбор HTML."""
    req = urllib.request.Request(
        "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": query, "count": count, "country": "AT", "search_lang": "ru"}),
        headers={"Accept": "application/json", "X-Subscription-Token": api_key})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    return [(_text(x.get("title", "")), x.get("url", ""), _text(x.get("description", "")))
            for x in (data.get("web") or {}).get("results", [])[:count]]


def searxng_search(base_url: str, query: str, count: int = 6) -> list[tuple[str, str, str]]:
    """Свой SearXNG (аддон HA): собирает выдачу Google, Bing и других, без ключей и лимитов.
    В его settings.yml должен быть включён формат json (search.formats: [html, json])."""
    url = base_url.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json", "language": "ru"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=20) as r:
        data = json.load(r)
    return [(_text(x.get("title", "")), x.get("url", ""), _text(x.get("content", "")))
            for x in data.get("results", [])[:count]]


def make_search(kind: str, brave_key: str = "", searxng_url: str = ""):
    """Выбрать источник выдачи: brave / searxng / иначе DuckDuckGo (его блокирует капча)."""
    if kind == "brave" and brave_key:
        return lambda q: brave_search(brave_key, q)
    if kind == "searxng" and searxng_url:
        return lambda q: searxng_search(searxng_url, q)
    return web_search


def ask_model(api_key: str, model: str, prompt: str, system: str = "") -> str:
    """Обычный запрос к Gemini без инструментов (квота у lite-моделей заметно больше)."""
    body: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    req = urllib.request.Request(API_URL.format(model=model), data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "x-goog-api-key": api_key})
    with urllib.request.urlopen(req, timeout=TASK_TIMEOUT_S) as r:
        data = json.load(r)
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def research_sync(api_key: str, model: str, goal: str, *,
                  search=web_search, ask=ask_model) -> str:
    """Исследование без поиска Google: модель формулирует запросы, берём выдачу DuckDuckGo,
    модель пересказывает по отрывкам. Три коротких обращения, квота Google-поиска не тратится."""
    raw = ask(api_key, model, "Дай 1-2 коротких поисковых запроса для интернета (по одному в строке, без "
                              f"нумерации, пояснений и разметки) для задачи:\n{goal}")
    queries = []
    for line in raw.splitlines():
        q = re.sub(r"^\s*(\d+[.)]|[-•*])\s*", "", line).strip(" *_`\"'«»")
        if q and not q.endswith(":") and not q.lower().startswith(("вот ", "запрос")):
            queries.append(q)
    queries = queries[:2] or [goal[:100]]
    seen, found = set(), []
    for q in queries:
        for title, url, snippet in search(q):
            if url not in seen:
                seen.add(url)
                found.append(f"- {title}: {snippet} ({urllib.parse.urlparse(url).netloc})")
    if not found:
        raise RuntimeError("поиск ничего не вернул")
    return ask(api_key, model, f"Задача: {goal}\n\nЧто нашлось в интернете:\n" + "\n".join(found[:12])
               + "\n\nОтветь по этим данным; если данных не хватает, так и скажи.", SYSTEM)


class Agents:
    def __init__(self, api_key: str, model: str, announce: Callable[[str], Awaitable[bool]], *,
                 generate: Callable[[str], Awaitable[str]] | None = None, clock=None,
                 search: str = "web", search_fn=None):
        """announce(prompt) → True, если колонка взялась озвучить (иначе повторим позже)."""
        self.announce = announce
        if search == "google":
            self.generate = generate or (lambda goal: asyncio.to_thread(generate_sync, api_key, model, goal))
        else:
            finder = search_fn or web_search
            self.generate = generate or (lambda goal: asyncio.to_thread(
                research_sync, api_key, model, goal, search=finder))
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
        return {"ok": True, "note": "принято, работает в фоне. Больше ничего не говори про поиск и "
                                    "не переспрашивай: колонка сама озвучит результат, когда он будет готов"}

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
            task.error = "квота Gemini исчерпана" if "429" in repr(e) else str(e)[:120]
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
            why = f" Причина: {task.error}." if task.error else ""
            return (f"Фоновая задача «{task.title}» не получилась.{why} Коротко и честно скажи "
                    "пользователю, что не вышло и почему, и не придумывай результат.")
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
