"""Школа: домашка, контрольные и изменения расписания из WebUntis + вечерняя подсказка + копилка Teams/Outlook.

Что школа даёт ученику (проверено вживую 2026-10-20 на school.webuntis.com под логином ученика):
  * JSON-RPC `getHomeworks` и `getExams` — «Method not found»: закрыты (в JSON-RPC их и не было).
  * Но работает внутренний REST самой веб-страницы, тем же входом ученика: после `authenticate` берём
    `GET /WebUntis/api/token/new` (cookie JSESSIONID) и с `Authorization: Bearer` читаем
      /WebUntis/api/homeworks/lessons?startDate&endDate  → домашка (текст, срок, completed) и предметы уроков,
      /WebUntis/api/exams?studentId&klasseId&startDate&endDate → контрольные (тип, предмет, дата, время).
  * Замены и отмены — обычный `getTimetable` (code cancelled/irregular, substText, lstext — заметка к уроку).
Только чтение. Пароль берётся из .env (UntisConfig), нигде не печатается. Работает при UNTIS_SERVER/SCHOOL/USER/
PASSWORD; режим «только iCal» эти данные не даёт.

Вечерняя подсказка (JARVIS_SCHOOL_REMIND=on|off, по умолчанию on): раз в день в JARVIS_SCHOOL_REMIND_AT
(19:30) накануне учебного дня (вс–чт), только если есть что сказать, не ночью и не в режиме «не слушай».
Функции Gemini: school_homework, school_exams, school_changes (+ school_missed, school_tasks,
school_task_done из school_inbox.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from .school_inbox import TOOL_NAMES as INBOX_TOOL_NAMES, TOOLS as INBOX_TOOLS, SchoolInbox, describe
from .untis import Untis, UntisError

log = logging.getLogger(__name__)

TICK_S = 60.0
RETRY_S = 20.0
CACHE_S = 600.0
GIVE_UP_MIN = 120           # подсказка позже чем через два часа после назначенного времени уже не нужна
OVERDUE_DAYS = 3            # просроченную невыполненную домашку помним столько дней
PREF_REMINDED = "school_reminded_day"

TOOLS: list[dict[str, Any]] = [
    {"name": "school_homework", "description": "Невыполненная домашка из WebUntis на ближайшие дни (предмет, что задано, "
     "срок). Вызови на «что задано», «что у меня по домашке». days — на сколько дней вперёд (по умолчанию 7).",
     "parameters": {"type": "OBJECT", "properties": {"days": {"type": "NUMBER"}}}},
    {"name": "school_exams", "description": "Ближайшие контрольные и тесты из WebUntis (предмет, тип, дата). Вызови на "
     "«когда контрольная», «что у меня на этой неделе из тестов». days — на сколько дней вперёд (по умолчанию 21).",
     "parameters": {"type": "OBJECT", "properties": {"days": {"type": "NUMBER"}}}},
    {"name": "school_changes", "description": "Что изменилось в расписании на день: отмены, замены, заметки к урокам. "
     "Вызови на «что завтра отменили», «есть ли замены». day=today или tomorrow (по умолчанию tomorrow).",
     "parameters": {"type": "OBJECT", "properties": {"day": {"type": "STRING"}}}},
]
SCHOOL_NAMES = {t["name"] for t in TOOLS}

REMIND_PROMPT = (
    "Вечер, пользователь школьник. Напомни ему про школу на завтра одной-тремя короткими фразами, по-русски, "
    "в своей манере, без выдумок сверх фактов: {facts} Если есть контрольная, скажи про неё первой. Не перечисляй "
    "больше трёх пунктов, остальное предложи рассказать по просьбе.")


def _on(v: str, default: str = "on") -> bool:
    return (v.strip() or default).lower() not in ("0", "false", "no", "off")


def _hhmm_ok(v: str) -> bool:
    return len(v) == 5 and v[2] == ":" and v[:2].isdigit() and v[3:].isdigit() and int(v[:2]) < 24 and int(v[3:]) < 60


@dataclass(frozen=True)
class SchoolConfig:
    remind: bool = True            # JARVIS_SCHOOL_REMIND
    remind_at: str = "19:30"       # JARVIS_SCHOOL_REMIND_AT

    @classmethod
    def from_env(cls, env: dict[str, str]) -> SchoolConfig:
        at = env.get("JARVIS_SCHOOL_REMIND_AT", "").strip()
        return cls(remind=_on(env.get("JARVIS_SCHOOL_REMIND", "")), remind_at=at if _hhmm_ok(at) else cls.remind_at)


def _d(n: Any) -> date | None:
    try:
        return datetime.strptime(str(n), "%Y%m%d").date()
    except ValueError:
        return None


def _t(n: Any) -> str:
    try:
        return f"{int(n) // 100:02d}:{int(n) % 100:02d}"
    except (TypeError, ValueError):
        return ""


def parse_homework(data: dict[str, Any]) -> list[dict]:
    """Ответ /api/homeworks/lessons → список {subject, text, due, assigned, done}; по сроку."""
    subj = {x.get("id"): x.get("subject", "") for x in data.get("lessons", [])}
    out = []
    for h in data.get("homeworks", []):
        due, given = _d(h.get("dueDate")), _d(h.get("date"))
        if due is None:
            continue
        text = " ".join(str(h.get("text", "")).split())
        if h.get("remark"):
            text += f" ({' '.join(str(h['remark']).split())})"
        out.append({"subject": subj.get(h.get("lessonId"), ""), "text": text, "due": due.isoformat(),
                    "assigned": given.isoformat() if given else "", "done": bool(h.get("completed"))})
    return sorted(out, key=lambda x: x["due"])


def parse_exams(data: dict[str, Any]) -> list[dict]:
    out = []
    for e in data.get("exams", []):
        day = _d(e.get("examDate"))
        if day is None:
            continue
        out.append({"date": day.isoformat(), "start": _t(e.get("startTime")), "subject": e.get("subject", ""),
                    "type": e.get("examType", ""), "name": " ".join(str(e.get("name", "")).split()),
                    "text": " ".join(str(e.get("text", "")).split())})
    return sorted(out, key=lambda x: (x["date"], x["start"]))


def parse_changes(rows: list[dict[str, Any]]) -> list[dict]:
    """Строки getTimetable → только отмены, замены и уроки с заметками (lstext/substText)."""
    out = []
    for r in rows:
        code = str(r.get("code") or "").lower()
        note = " ".join(" ".join(str(r.get(k) or "") for k in ("substText", "lstext")).split())
        if code not in ("cancelled", "irregular") and not r.get("substText"):
            continue                       # просто заметка lstext без изменения — шум («2/2»)
        su = (r.get("su") or [{}])[0]
        out.append({"time": _t(r.get("startTime")), "subject": su.get("name") or su.get("longname") or "урок",
                    "kind": "отмена" if code == "cancelled" else "замена", "note": note})
    out.sort(key=lambda x: x["time"])
    merged: list[dict] = []
    for c in out:                          # двойные уроки приходят двумя строками
        if not merged or any(merged[-1][k] != c[k] for k in ("subject", "kind", "note")):
            merged.append(c)
    return merged


class School:
    def __init__(self, untis: Untis | None, inbox: SchoolInbox, cfg: SchoolConfig | None = None, *,
                 announce: Callable[[str], Awaitable[bool]] | None = None, brain=None,
                 can_speak: Callable[[], bool] = lambda: True,
                 now: Callable[[], datetime] = datetime.now):
        self.untis = untis if untis and untis.cfg.server and untis.cfg.school and untis.cfg.user and untis.cfg.password else None
        self.inbox, self.cfg, self.announce, self.brain = inbox, cfg or SchoolConfig(), announce, brain
        self.can_speak, self.now = can_speak, now
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._reminded = brain.pref(PREF_REMINDED) if brain else ""
        self._task: asyncio.Task | None = None
        self._warned = False

    @property
    def TOOLS(self) -> list[dict[str, Any]]:               # noqa: N802 — как атрибут у других расширений Brain
        return (TOOLS if self.untis else []) + INBOX_TOOLS

    @property
    def tool_names(self) -> set[str]:
        return (SCHOOL_NAMES if self.untis else set()) | INBOX_TOOL_NAMES

    # ── WebUntis ──

    async def _session(self, work: Callable[[str, dict[str, str], dict[str, Any]], Awaitable[Any]]) -> Any:
        """Войти учеником, дать `work(base, rest_headers, auth)` и выйти. Только чтение."""
        if not self.untis:
            raise UntisError("WebUntis с логином ученика не настроен (UNTIS_SERVER/SCHOOL/USER/PASSWORD)")
        u, c = self.untis, self.untis.cfg
        base = f"https://{c.server}"
        url = f"{base}/WebUntis/jsonrpc.do?school={quote(c.school)}"
        auth = await u._rpc(url, "authenticate", {"user": c.user, "password": c.password, "client": "jarvis"})
        sid = auth["sessionId"]
        try:
            auth["_rpc"] = (url, sid)
            cookie = {"Cookie": f"JSESSIONID={sid}"}
            token = (await u.fetch(f"{base}/WebUntis/api/token/new", None, cookie)).decode().strip()
            return await work(base, {**cookie, "Authorization": f"Bearer {token}"}, auth)
        finally:
            try:
                await u._rpc(url, "logout", {}, sid)
            except Exception:
                pass

    async def _cached(self, key: tuple, make: Callable[[], Awaitable[Any]]) -> Any:
        t = asyncio.get_running_loop().time()
        hit = self._cache.get(key)
        if hit is None or t - hit[0] > CACHE_S:
            self._cache[key] = (t, await make())
        return self._cache[key][1]

    async def _rest(self, path: str, base: str, headers: dict[str, str]) -> dict[str, Any]:
        raw = json.loads(await self.untis.fetch(base + path, None, headers))      # type: ignore[union-attr]
        return raw.get("data") or {}

    @staticmethod
    def _n(d: date) -> int:
        return int(d.strftime("%Y%m%d"))

    async def homework(self, days: int = 7) -> list[dict]:
        """Невыполненная домашка: срок от (сегодня − OVERDUE_DAYS) до сегодня + days."""
        today = self.now().date()
        lo, hi = today - timedelta(days=OVERDUE_DAYS), today + timedelta(days=days)

        async def make() -> list[dict]:
            async def work(base, headers, auth):
                q = f"startDate={self._n(today - timedelta(days=30))}&endDate={self._n(hi)}"
                return parse_homework(await self._rest(f"/WebUntis/api/homeworks/lessons?{q}", base, headers))
            return await self._session(work)
        rows = await self._cached(("hw", today, days), make)
        return [h for h in rows if not h["done"] and lo.isoformat() <= h["due"] <= hi.isoformat()]

    async def exams(self, days: int = 21) -> list[dict]:
        today = self.now().date()

        async def make() -> list[dict]:
            async def work(base, headers, auth):
                q = (f"studentId={auth['personId']}&klasseId={auth.get('klasseId', 0)}"
                     f"&startDate={self._n(today)}&endDate={self._n(today + timedelta(days=days))}")
                return parse_exams(await self._rest(f"/WebUntis/api/exams?{q}", base, headers))
            return await self._session(work)
        return await self._cached(("ex", today, days), make)

    async def changes(self, day: date) -> list[dict]:
        async def make() -> list[dict]:
            async def work(base, headers, auth):
                url, sid = auth["_rpc"]
                n = self._n(day)
                rows = await self.untis._rpc(url, "getTimetable", {      # type: ignore[union-attr]
                    "options": {"element": {"id": auth["personId"], "type": auth.get("personType", 5)},
                                "startDate": n, "endDate": n, "showSubstText": True, "showLsText": True,
                                "klasseFields": ["id"], "subjectFields": ["name", "longname"]}}, sid)
                return parse_changes(rows or [])
            return await self._session(work)
        return await self._cached(("ch", day), make)

    # ── инструменты Gemini ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name in INBOX_TOOL_NAMES:
            return await self.inbox.call(name, args)
        if not self.untis:
            return {"ok": False, "error": "WebUntis с логином ученика не настроен"}
        try:
            if name == "school_homework":
                items = await self.homework(_days(args, 7))
                return {"ok": True, "count": len(items), "homework": items,
                        "note": "" if items else "невыполненной домашки на эти дни нет"}
            if name == "school_exams":
                items = await self.exams(_days(args, 21))
                return {"ok": True, "count": len(items), "exams": items,
                        "note": "" if items else "контрольных в ближайшие дни нет"}
            if name == "school_changes":
                day = self.now().date() + (timedelta(0) if str(args.get("day", "")).lower() in ("today", "сегодня") else timedelta(days=1))
                items = await self.changes(day)
                return {"ok": True, "day": day.isoformat(), "count": len(items), "changes": items,
                        "note": "" if items else "отмен и замен нет"}
        except Exception as e:
            log.warning("школа: %s не вышло: %r", name, e)
            return {"ok": False, "error": f"WebUntis не ответил: {e}"}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── вечерняя подсказка ──

    async def facts(self, day: date) -> str:
        """Факты на `day` одной строкой; пусто — говорить не о чем."""
        parts: list[str] = []
        exams = [e for e in await self.exams(3) if e["date"] <= (day + timedelta(days=1)).isoformat()]
        for e in exams:
            when = "завтра" if e["date"] == day.isoformat() else f"послезавтра ({e['date'][5:]})"
            parts.append(f"{when} контрольная ({e['type'] or 'тест'}) по {e['subject']}")
        hw = await self.homework(3)
        if hw:
            parts.append("задано и не сделано: " + "; ".join(
                f"{h['subject']}: {h['text']} (до {h['due'][5:]})" for h in hw[:4]))
        ch = await self.changes(day)
        if ch:
            parts.append("в расписании на завтра: " + "; ".join(
                f"{c['kind']} {c['subject']} в {c['time']}" + (f" ({c['note']})" if c['note'] else "") for c in ch[:4]))
        tasks = self.inbox.tasks()
        if tasks:
            parts.append(f"в Teams висит заданий без отметки о выполнении: {len(tasks)}")
        return "Факты: " + ". ".join(parts) + "." if parts else ""

    def _in_window(self) -> bool:
        t = self.now()
        m, at = t.hour * 60 + t.minute, int(self.cfg.remind_at[:2]) * 60 + int(self.cfg.remind_at[3:])
        return at <= m < at + GIVE_UP_MIN

    async def remind_step(self) -> float:
        if not (self.cfg.remind and self.untis and self.announce) or not self._in_window():
            return TICK_S
        today = self.now().date()
        if self._reminded == today.isoformat():
            return TICK_S
        tomorrow = today + timedelta(days=1)
        if tomorrow.weekday() >= 5:                       # завтра выходной: накануне учебного дня только вс–чт
            self._mark(today, "завтра выходной")
            return TICK_S
        if not self.can_speak():
            return RETRY_S * 3
        facts = await self.facts(tomorrow)
        if not facts:
            self._mark(today, "говорить не о чем")
            return TICK_S
        if await self.announce(REMIND_PROMPT.format(facts=facts)):
            self._mark(today, "напомнил про школу")
            return TICK_S
        return RETRY_S

    def _mark(self, day: date, why: str) -> None:
        log.info("школа: %s", why)
        self._reminded = day.isoformat()
        if self.brain:
            self.brain.set_pref(PREF_REMINDED, self._reminded)

    async def _loop(self) -> None:
        while True:
            try:
                delay = await self.remind_step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._warned:
                    self._warned = True
                    log.warning("школа: вечерняя подсказка не вышла: %r", e)
                delay = 300.0
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self.cfg.remind and self.untis and self.announce:
            self._task = asyncio.create_task(self._loop())
        else:
            log.info("школа: вечерняя подсказка выключена или WebUntis не настроен")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # ── консоль ──

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "school":
            return None
        arg = parts[1].lower() if len(parts) > 1 else "status"
        try:
            if arg == "status":
                return (f"WebUntis: {'подключён' if self.untis else 'не настроен'}; "
                        f"напоминание: {'вкл' if self.cfg.remind else 'выкл'} в {self.cfg.remind_at}; "
                        f"Teams/Outlook: {self.inbox.summary()}")
            if arg in ("hw", "homework"):
                r = await self.call("school_homework", {})
                return "\n".join(f"{h['due']} {h['subject']}: {h['text']}" for h in r.get("homework", [])) or r.get("note") or r.get("error", "")
            if arg == "exams":
                r = await self.call("school_exams", {})
                return "\n".join(f"{e['date']} {e['start']} {e['subject']} {e['type']} {e['name']}" for e in r.get("exams", [])) or r.get("note") or r.get("error", "")
            if arg == "changes":
                r = await self.call("school_changes", {})
                return "\n".join(f"{c['time']} {c['kind']} {c['subject']} {c['note']}" for c in r.get("changes", [])) or r.get("note") or r.get("error", "")
            if arg == "missed":
                return describe(self.inbox.missed(mark=False)) or "новых нет"
            if arg == "tasks":
                return describe(self.inbox.tasks()) or "заданий из Teams нет"
            if arg == "remind":
                facts = await self.facts(self.now().date() + timedelta(days=1))
                if not facts:
                    return "говорить не о чем"
                if not self.announce:
                    return facts
                return "колонка заговорила" if await self.announce(REMIND_PROMPT.format(facts=facts)) else f"колонки нет или занята. {facts}"
            if arg == "clear":
                return f"удалено уведомлений: {self.inbox.reset()}"
        except Exception as e:
            return f"не вышло: {e!r}"
        return "school status|hw|exams|changes|missed|tasks|remind|clear"


def _days(args: dict[str, Any], default: int) -> int:
    try:
        return max(1, min(int(float(args.get("days", default))), 120))
    except (TypeError, ValueError):
        return default
