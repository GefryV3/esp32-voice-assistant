"""Новости по темам (идея #47): до трёх коротких новостей в утренний брифинг.

Темы лежат в brain.db (таблица news_topics): голосом «следи за новостями про: техника, Австрия» (функция
news_topics) и консолью `news topics|add|del|now|on|off`. В брифинг новости попадают одной добавкой к его
подсказке (Routines.briefing_extra), а не отдельным объявлением, поэтому дублей нет. Ищут фоновые агенты
(agents.py: Tavily/Serper/DuckDuckGo, как у start_task). За день ищем один раз: повторы брифинга (колонка
была занята) берут готовый текст. Вчерашние новости не повторяем.

Выключатель: JARVIS_NEWS=on|off. По умолчанию (не задан) новости включатся сами, как только названы темы;
`off` выключает совсем; консоль `news on|off` ставит ручную отметку (если env не off).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

log = logging.getLogger(__name__)

MAX_TOPICS = 12
MAX_ITEMS = 3
SEARCH_TIMEOUT_S = 45.0
MAX_TOPIC_LEN = 40

SCHEMA = """
CREATE TABLE IF NOT EXISTS news_topics (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, topic TEXT NOT NULL, added TEXT NOT NULL);
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "news_topics", "description": "Темы утренних новостей. action=add — «следи за новостями про: "
     "техника, Австрия» (topics — список тем), del — «не надо новостей про X», list — «какие у меня темы», "
     "clear — «хватит новостей». Новости будут в утренней сводке, до трёх коротких.",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING"}, "topics": {"type": "ARRAY", "items": {"type": "STRING"}}},
         "required": ["action"]}},
]

GOAL = ("Найди одну самую важную свежую новость (за последние сутки-двое) по теме «{topic}». Ответь ОДНОЙ "
        "короткой фразой до 25 слов по-русски: что произошло, без ссылок, без вступления и без названия "
        "источника. Если ничего свежего и надёжного нет, ответь одним словом: НЕТ.")

PROMPT = ("\n\nЕщё в сводке: новости по его темам. Расскажи кратко, по фразе на каждую, называя тему "
          "(«про технику: …»), в своей манере; ничего не добавляй от себя:\n{items}")

Generate = Callable[[str], Awaitable[str]]


def split_topics(raw: Any) -> list[str]:
    """«техника, Австрия» или список → чистые темы без повторов."""
    parts = raw if isinstance(raw, (list, tuple)) else re.split(r"[,;\n]", str(raw or ""))
    out: list[str] = []
    for p in parts:
        t = " ".join(str(p).replace("«", "").replace("»", "").split()).strip(" .:")
        if t and len(t) <= MAX_TOPIC_LEN and t.casefold() not in (o.casefold() for o in out):
            out.append(t)
    return out


class News:
    TOOLS = TOOLS
    tool_names = frozenset(t["name"] for t in TOOLS)

    def __init__(self, brain: Any, generate: Callable[[], Generate | None], *, env_state: str = "",
                 now: Callable[[], datetime] = datetime.now):
        """generate() → функция «цель → текст» (агенты) или None, если агентов нет."""
        self.brain, self._generate, self.now = brain, generate, now
        self.env_state = env_state.strip().lower()           # on | off | '' (не задан)
        brain.db.executescript(SCHEMA)
        brain.db.commit()

    @classmethod
    def from_env(cls, brain: Any, generate: Callable[[], Generate | None], env: dict[str, str]) -> News:
        return cls(brain, generate, env_state=env.get("JARVIS_NEWS", ""))

    # ── темы ──

    def topics(self) -> list[str]:
        return [r[0] for r in self.brain.db.execute("SELECT topic FROM news_topics ORDER BY id")]

    def add(self, topics: list[str]) -> int:
        n = 0
        for t in topics:
            if len(self.topics()) >= MAX_TOPICS:
                break
            cur = self.brain.db.execute("INSERT OR IGNORE INTO news_topics (key, topic, added) VALUES (?, ?, ?)",
                                        (t.casefold(), t, self.now().isoformat(timespec="seconds")))
            n += cur.rowcount
        self.brain.db.commit()
        return n

    def delete(self, topics: list[str]) -> int:
        n = 0
        for t in topics:
            n += self.brain.db.execute("DELETE FROM news_topics WHERE key = ?", (t.casefold(),)).rowcount
        self.brain.db.commit()
        return n

    def clear(self) -> int:
        n = self.brain.db.execute("DELETE FROM news_topics").rowcount
        self.brain.db.commit()
        return n

    def enabled(self) -> bool:
        if self.env_state in ("0", "false", "no", "off"):
            return False
        manual = self.brain.pref("news_enabled")
        if manual in ("0", "1"):
            return manual == "1"
        return self.env_state in ("1", "true", "yes", "on") or bool(self.topics())

    # ── поиск ──

    async def _one(self, gen: Generate, topic: str) -> str:
        try:
            text = " ".join((await gen(GOAL.format(topic=topic))).split())
        except Exception as e:
            log.warning("новости «%s»: %r", topic, e)
            return ""
        return "" if not text or text.upper().strip(" .!") == "НЕТ" else text[:300]

    async def fetch(self) -> list[tuple[str, str]]:
        """До трёх (тема, новость): темы по кругу от дня года, чтобы доставались все."""
        gen = self._generate()
        topics = self.topics()
        if gen is None or not topics:
            return []
        shift = self.now().timetuple().tm_yday % len(topics)
        chosen = (topics[shift:] + topics[:shift])[:MAX_ITEMS]
        try:
            async with asyncio.timeout(SEARCH_TIMEOUT_S):
                found = await asyncio.gather(*(self._one(gen, t) for t in chosen))
        except TimeoutError:
            return []
        return [(t, n) for t, n in zip(chosen, found) if n]

    async def briefing_extra(self) -> str:
        """Добавка к подсказке брифинга ('' — новостей нет). Раз в день, повторы берут кэш."""
        if not self.enabled():
            return ""
        day = f"{self.now():%Y-%m-%d}"
        try:
            cache = json.loads(self.brain.pref("news_cache", "{}"))
        except ValueError:
            cache = {}
        if cache.get("day") != day:
            items = await self.fetch()
            old = {n for _, n in cache.get("items", [])}
            items = [i for i in items if i[1] not in old] if cache.get("day") else items   # вчерашнее не повторяем
            cache = {"day": day, "items": items}
            self.brain.set_pref("news_cache", json.dumps(cache, ensure_ascii=False))
        items = cache.get("items") or []
        if not items:
            return ""
        return PROMPT.format(items="\n".join(f"- {t}: {n}" for t, n in items))

    # ── инструменты и консоль ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name != "news_topics":
            return {"ok": False, "error": f"нет такого инструмента: {name}"}
        action = str(args.get("action", "")).strip().casefold()
        topics = split_topics(args.get("topics"))
        if action in ("add", "добавить", "follow"):
            if not topics:
                return {"ok": False, "error": "какие темы? назови хотя бы одну"}
            self.brain.set_pref("news_enabled", "")          # свежие темы снимают ручное «выключено»
            return {"ok": True, "added": self.add(topics), "topics": self.topics(),
                    "enabled": self.enabled(), "note": "новости будут в утренней сводке, до трёх коротких"}
        if action in ("del", "delete", "remove", "удалить"):
            return {"ok": True, "removed": self.delete(topics), "topics": self.topics()}
        if action == "clear":
            return {"ok": True, "removed": self.clear()}
        if action == "list":
            return {"topics": self.topics(), "enabled": self.enabled()}
        return {"ok": False, "error": "action: add, del, list или clear"}

    HELP = "news topics | add <темы через запятую> | del <тема> | now | on | off — темы утренних новостей"

    async def command(self, line: str) -> str | None:
        head, _, rest = line.strip().partition(" ")
        if head.lower() != "news":
            return None
        sub, _, arg = rest.strip().partition(" ")
        match sub.lower():
            case "topics" | "list" | "":
                return (f"новости {'вкл' if self.enabled() else 'выкл'} (JARVIS_NEWS={self.env_state or 'не задан'}); темы: "
                        + (", ".join(self.topics()) or "нет"))
            case "add":
                self.brain.set_pref("news_enabled", "")
                return f"добавлено {self.add(split_topics(arg))}; темы: {', '.join(self.topics())}"
            case "del":
                return f"удалено {self.delete(split_topics(arg))}; темы: {', '.join(self.topics()) or 'нет'}"
            case "on" | "off":
                self.brain.set_pref("news_enabled", "1" if sub.lower() == "on" else "0")
                return f"новости {sub.lower()}" + (" (но JARVIS_NEWS=off сильнее)" if self.env_state == "off" else "")
            case "now":
                items = await self.fetch()
                return "\n".join(f"{t}: {n}" for t, n in items) or "ничего не нашлось (нет тем или поиска)"
        return self.HELP
