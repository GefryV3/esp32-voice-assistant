"""Предложения новых навыков и путь от «Мага предложила» до «навык написан на ноутбуке».

Поток (docs/skills.md):
  propose_skill (Gemini) → запись в brain.db (skill_requests) + карточка в Telegram
  → /approve N | голос «да, делай» → задание `skill_job` уходит на ноутбук (мост ПК → tools/skill_runner.py)
  → `skill_result` (ветка + тесты) → Telegram «навык готов, смотри diff»
  → /enable имя (после того как человек слил ветку и выложил сервер) — только по явному «да».
Ничего не сливается и не выкладывается автоматически.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Awaitable, Callable
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .skills_loader import NAME_RE, SkillLoader

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_requests (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, title TEXT NOT NULL, what TEXT NOT NULL, why TEXT NOT NULL,
    status TEXT NOT NULL, created TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '', report TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS skill_runs (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, rid INTEGER NOT NULL);
"""
MAX_OPEN = 5            # не больше стольких нерешённых предложений: Мага не должна заваливать карточками
STATUS_RU = {"proposed": "ждёт ответа", "approved": "одобрено, ждёт ноутбук", "running": "пишется",
             "ready": "готово, ждёт включения", "failed": "не вышло", "rejected": "отклонено", "enabled": "включено"}
OPEN_STATUSES = ("proposed", "approved", "running")



@dataclass(frozen=True)
class Quota:
    """Лимит сборок навыков: каждая сборка тратит лимит подписки Claude, поэтому по умолчанию строго.
    Считаются запуски `claude -p` на ноутбуке (одобренные задания, ушедшие на ноутбук)."""
    per_day: int = 1
    per_week: int = 3
    min_gap_h: float = 4.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Quota":
        env = {**os.environ} if env is None else env
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return max(float(env.get(key, default)), 0.0)
            except (TypeError, ValueError):
                return default
        return cls(int(num("SKILL_MAX_PER_DAY", d.per_day)), int(num("SKILL_MAX_PER_WEEK", d.per_week)),
                   num("SKILL_MIN_GAP_H", d.min_gap_h))


TOOLS: list[dict[str, Any]] = [
    {"name": "propose_skill", "description": "Предложить себе новый навык, которого не хватает: пользователь "
     "просит «научись делать X» или нужна новая функция с данными и действиями из внешних систем. НЕ для игр, "
     "историй, шуток, загадок и разговоров: в них ты умеешь всё сам. Ничего не пишется сразу: пользователю "
     "уходит карточка в Telegram, и только после его «да» навык пишется на ноутбуке и проверяется человеком. "
     "title — короткое название, what — что навык делает (какие функции и откуда данные), why — зачем.",
     "parameters": {"type": "OBJECT", "properties": {
         "title": {"type": "STRING"}, "what": {"type": "STRING"}, "why": {"type": "STRING"},
         "name": {"type": "STRING", "description": "латинское имя snake_case, необязательно"}},
         "required": ["title", "what"]}},
    {"name": "skill_decision", "description": "Пользователь ответил на предложение навыка. decision: approve "
     "(«да, делай», «согласен») или reject («не надо»). id не нужен, если ждёт одно предложение. "
     "Вызывай только на прямой ответ пользователя.",
     "parameters": {"type": "OBJECT", "properties": {
         "decision": {"type": "STRING"}, "id": {"type": "NUMBER"}}, "required": ["decision"]}},
]

_TRANSLIT = dict(zip("абвгдезийклмнопрстуфыэ", "abvgdezijklmnoprstufye"))
_TRANSLIT.update({"ж": "zh", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ю": "yu", "я": "ya",
                  "ё": "e", "ъ": "", "ь": ""})


def slug(text: str) -> str:
    """«Курс биткоина» → kurs_bitkoina (имя файла навыка)."""
    out = "".join(_TRANSLIT.get(c, c) for c in text.lower())
    out = re.sub(r"[^a-z0-9]+", "_", out).strip("_")[:28]
    return out if NAME_RE.match(out) else ("skill_" + out)[:30].rstrip("_") if out else "new_skill"


class SkillRequests:
    def __init__(self, db: sqlite3.Connection, *, loader: SkillLoader | None = None,
                 now: Callable[[], datetime] = datetime.now,
                 notify: Callable[[str], Awaitable[Any]] | None = None,
                 pc_send: Callable[[dict], Awaitable[bool]] | None = None, quota: Quota | None = None):
        self.db, self.loader, self.now = db, loader, now
        self.quota = quota or Quota.from_env()
        self.notify = notify          # текст в Telegram
        self.pc_send = pc_send        # сообщение мосту ПК; True — ушло
        db.executescript(SCHEMA)
        db.commit()

    # ── лимит сборок (экономим лимит подписки Claude) ──

    def build_enabled(self) -> bool:
        row = self.db.execute("SELECT value FROM prefs WHERE key = 'skills_build'").fetchone() \
            if self.db.execute("SELECT name FROM sqlite_master WHERE name = 'prefs'").fetchone() else None
        return not row or row[0] != "off"

    def set_build_enabled(self, on: bool) -> None:
        self.db.execute("CREATE TABLE IF NOT EXISTS prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("INSERT INTO prefs (key, value) VALUES ('skills_build', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("on" if on else "off",))
        self.db.commit()

    def _runs_since(self, since: datetime) -> list[datetime]:
        rows = self.db.execute("SELECT ts FROM skill_runs ORDER BY id").fetchall()
        return [t for t in (datetime.fromisoformat(r[0]) for r in rows) if t >= since]

    def quota_block(self) -> str:
        """Пустая строка, если сборку можно запускать, иначе причина и когда можно."""
        if not self.build_enabled():
            return "сборка навыков выключена (/skills build on, чтобы включить)"
        now, q = self.now(), self.quota
        day = self._runs_since(now - timedelta(days=1))
        week = self._runs_since(now - timedelta(days=7))
        if q.min_gap_h and day and now - day[-1] < timedelta(hours=q.min_gap_h):
            return f"после прошлой сборки не прошло {q.min_gap_h:g} ч, можно после {(day[-1] + timedelta(hours=q.min_gap_h)):%H:%M}"
        if len(day) >= q.per_day:
            return f"сегодня уже {len(day)} из {q.per_day} сборок, можно после {(day[0] + timedelta(days=1)):%d.%m %H:%M}"
        if len(week) >= q.per_week:
            return f"за неделю уже {len(week)} из {q.per_week} сборок, можно после {(week[0] + timedelta(days=7)):%d.%m %H:%M}"
        return ""

    def quota_status(self) -> str:
        now, q = self.now(), self.quota
        return (f"Сборка навыков: {'включена' if self.build_enabled() else 'выключена'}. За сутки "
                f"{len(self._runs_since(now - timedelta(days=1)))}/{q.per_day}, за неделю "
                f"{len(self._runs_since(now - timedelta(days=7)))}/{q.per_week}, пауза между сборками {q.min_gap_h:g} ч. "
                f"Меняется: SKILL_MAX_PER_DAY, SKILL_MAX_PER_WEEK, SKILL_MIN_GAP_H в .env.")

    # ── хранилище ──

    def get(self, rid: int) -> dict | None:
        r = self.db.execute("SELECT id, name, title, what, why, status, created, branch, report "
                            "FROM skill_requests WHERE id = ?", (rid,)).fetchone()
        return dict(zip(("id", "name", "title", "what", "why", "status", "created", "branch", "report"), r)) if r else None

    def by_status(self, *statuses: str) -> list[dict]:
        q = ",".join("?" * len(statuses))
        ids = [r[0] for r in self.db.execute(f"SELECT id FROM skill_requests WHERE status IN ({q}) ORDER BY id", statuses)]
        return [self.get(i) for i in ids]      # type: ignore[misc]

    def _set(self, rid: int, **fields: str) -> None:
        for k, v in fields.items():
            self.db.execute(f"UPDATE skill_requests SET {k} = ? WHERE id = ?", (v, rid))     # k — наши имена колонок
        self.db.commit()

    def propose(self, title: str, what: str, why: str = "", name: str = "") -> dict:
        title, what = title.strip(), what.strip()
        if not title or not what:
            raise ValueError("нужны название и описание")
        open_ = self.by_status(*OPEN_STATUSES)
        same = next((r for r in open_ if r["title"].casefold() == title.casefold()), None)
        if same:
            return {**same, "new": False}
        if len([r for r in open_ if r["status"] == "proposed"]) >= MAX_OPEN:
            raise ValueError("уже висит слишком много предложений, пусть пользователь ответит на старые")
        name = name.strip().lower()
        name = name if NAME_RE.match(name) else slug(title)
        cur = self.db.execute("INSERT INTO skill_requests (name, title, what, why, status, created) "
                              "VALUES (?, ?, ?, ?, 'proposed', ?)",
                              (name, title[:80], what[:1500], why.strip()[:500], self.now().isoformat(timespec="seconds")))
        self.db.commit()
        return {**self.get(cur.lastrowid), "new": True}      # type: ignore[arg-type,dict-item]

    # ── тексты ──

    @staticmethod
    def card(r: dict) -> str:
        why = f"\nЗачем: {r['why']}" if r["why"] else ""
        return (f"Предлагаю навык: «{r['title']}» (№{r['id']}, файл {r['name']}.py)\n{r['what']}{why}\n"
                f"Согласен? /approve {r['id']} или /reject {r['id']}. Можно ответить голосом: «да, делай».")

    def listing(self) -> str:
        rows = self.db.execute("SELECT id FROM skill_requests ORDER BY id DESC LIMIT 15").fetchall()
        lines = [f"№{r['id']} {r['title']} — {STATUS_RU.get(r['status'], r['status'])}"
                 + (f" (ветка {r['branch']})" if r["branch"] else "") for r in (self.get(i[0]) for i in rows) if r]
        return "\n".join(lines) or "предложений нет"

    async def _say(self, text: str) -> None:
        if self.notify is not None:
            await self.notify(text)

    # ── решения ──

    async def propose_and_announce(self, title: str, what: str, why: str = "", name: str = "") -> dict:
        r = self.propose(title, what, why, name)
        if r["new"]:
            await self._say(self.card(r))
        return r

    def _target(self, rid: int | None) -> dict:
        if rid is None:
            pending = self.by_status("proposed")
            if len(pending) != 1:
                raise ValueError("ждёт не одно предложение, назови номер" if pending else "нет предложений, которые ждут ответа")
            return pending[0]
        r = self.get(int(rid))
        if r is None:
            raise ValueError(f"предложения №{rid} нет")
        return r

    async def approve(self, rid: int | None = None) -> str:
        r = self._target(rid)
        if r["status"] not in ("proposed", "approved"):
            raise ValueError(f"№{r['id']} уже {STATUS_RU.get(r['status'], r['status'])}")
        self._set(r["id"], status="approved")
        return await self._dispatch(r)

    async def _dispatch(self, r: dict) -> str:
        if reason := self.quota_block():
            return (f"Одобрено: №{r['id']} «{r['title']}», но запускать пока нельзя: {reason}. "
                    "Оно ждёт: напиши /approve " + str(r["id"]) + " позже.")
        job = {"type": "skill_job", "id": r["id"], "name": r["name"], "title": r["title"],
               "what": r["what"], "why": r["why"]}
        if self.pc_send is not None and await self.pc_send(job):
            self.db.execute("INSERT INTO skill_runs (ts, rid) VALUES (?, ?)",
                            (self.now().isoformat(timespec="seconds"), r["id"]))
            self.db.commit()
            self._set(r["id"], status="running")
            return f"Одобрено: №{r['id']} «{r['title']}». Ноутбук взял задание, напишу, когда будет готово."
        return (f"Одобрено: №{r['id']} «{r['title']}», но ноутбук не на связи. Запущу, как только он "
                "подключится (программа pc/agent.py).")

    async def dispatch_waiting(self) -> None:
        """Ноутбук подключился: отправить одобренные, но не ушедшие задания."""
        for r in self.by_status("approved"):
            await self._say(await self._dispatch(r))

    def reject(self, rid: int | None = None) -> str:
        r = self._target(rid)
        if r["status"] not in ("proposed", "approved"):
            raise ValueError(f"№{r['id']} уже {STATUS_RU.get(r['status'], r['status'])}")
        self._set(r["id"], status="rejected")
        return f"Отклонено: №{r['id']} «{r['title']}»."

    # ── ответы ноутбука ──

    async def handle_pc(self, msg: dict[str, Any]) -> None:
        """skill_progress / skill_result от tools/skill_runner.py (через pc/agent.py)."""
        r = self.get(int(msg.get("id", 0) or 0))
        if r is None:
            return
        kind = msg.get("type")
        if kind == "skill_progress":
            self._set(r["id"], status="running")
            if msg.get("text"):
                log.info("навык №%s: %s", r["id"], msg["text"])
        elif kind == "skill_result":
            if msg.get("ok"):
                report = "\n".join(filter(None, [str(msg.get("diffstat", "")), "Тесты: " + str(msg.get("tests", "?")),
                                                 str(msg.get("diff", ""))]))
                self._set(r["id"], status="ready", branch=str(msg.get("branch", "")), report=report[:6000])
                await self._say(
                    f"Навык «{r['title']}» готов (№{r['id']}). Ветка {msg.get('branch')}, тесты: {msg.get('tests', '?')}.\n"
                    f"{msg.get('diffstat', '')}\n\nСмотри diff: git diff main...{msg.get('branch')} (или /diff {r['id']}). "
                    f"Если нравится: слей ветку, выложи сервер и напиши /enable {r['name']}. "
                    f"Включу только по твоему «да».")
            else:
                self._set(r["id"], status="failed", report=str(msg.get("error", ""))[:1500])
                await self._say(f"Навык «{r['title']}» (№{r['id']}) не получился: {str(msg.get('error', ''))[:400]}")

    # ── команды Telegram/консоли и вызовы Gemini ──

    async def command(self, line: str) -> str | None:
        parts = line.strip().split()
        if not parts:
            return None
        cmd = parts[0].lower().lstrip("/")
        arg = parts[1] if len(parts) > 1 else ""
        num = int(arg) if arg.isdigit() else None
        try:
            if cmd == "skills" and (len(parts) == 1 or parts[1].lower() in ("requests", "заявки")):
                head = self.loader.summary() if self.loader else "загрузчик навыков не запущен"
                return f"Навыки:\n{head}\n\nПредложения:\n{self.listing()}"
            if cmd == "skills" and len(parts) >= 2 and parts[1].lower() == "build":
                if len(parts) >= 3 and parts[2].lower() in ("on", "off"):
                    self.set_build_enabled(parts[2].lower() == "on")
                return self.quota_status()
            if cmd == "approve":
                return await self.approve(num)
            if cmd == "reject":
                return self.reject(num)
            if cmd == "diff" and num is not None:
                r = self.get(num)
                return (r["report"] or "отчёта нет") if r else f"предложения №{num} нет"
            if cmd == "enable" and arg and self.loader:
                return self._enable(arg)
            if cmd in ("disable", "reload", "skills") and self.loader:
                line2 = line if cmd == "skills" else f"skills {cmd} {arg}"
                out = self.loader.command(line2)
                if out is not None:
                    return out
        except ValueError as e:
            return str(e)
        except KeyError as e:
            return str(e.args[0])
        return None

    def _enable(self, name: str) -> str:
        assert self.loader is not None
        out = self.loader.enable(name.lower())
        for r in self.by_status("ready"):
            if r["name"] == name.lower() and self.loader.skills.get(name.lower()) and self.loader.skills[name.lower()].loaded:
                self._set(r["id"], status="enabled")
        return out

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        try:
            if name == "propose_skill":
                r = await self.propose_and_announce(str(args.get("title", "")), str(args.get("what", "")),
                                                    str(args.get("why", "")), str(args.get("name", "")))
                return {"ok": True, "id": r["id"], "status": r["status"],
                        "note": "карточка ушла в Telegram; скажи пользователю, что спросил, и жди его «да»"}
            if name == "skill_decision":
                rid = int(args["id"]) if args.get("id") else None
                decision = str(args.get("decision", "")).strip().lower()
                if decision in ("approve", "да", "yes"):
                    return {"ok": True, "result": await self.approve(rid)}
                if decision in ("reject", "нет", "no"):
                    return {"ok": True, "result": self.reject(rid)}
                return {"ok": False, "error": "decision: approve или reject"}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}
