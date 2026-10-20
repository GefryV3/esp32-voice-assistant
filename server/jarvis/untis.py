"""Расписание ученика из WebUntis: первый урок на день с учётом отмен и замен.

Два способа, оба без паролей в коде (всё из .env, см. UntisConfig):
  1. iCal-ссылка «Календарь-Abo» из WebUntis (UNTIS_ICAL_URL): только чтение, без логина.
     Ссылка сама содержит секретный токен, поэтому лежит только в .env.
  2. JSON-RPC API WebUntis с логином ученика (UNTIS_SERVER, UNTIS_SCHOOL, UNTIS_USER, UNTIS_PASSWORD).
Снаружи один интерфейс: Untis.lessons(day) → список Lesson; Untis.first_lesson(day).
Сеть подменяется (fetch), поэтому тесты ходят без интернета.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

CACHE_S = 600.0
CANCEL_WORDS = ("entfall", "entfällt", "ausfall", "cancel", "fällt aus", "отмен")
CHANGE_WORDS = ("vertretung", "substitut", "verlegt", "raumänderung", "замен", "irregular")

# fetch(url, body, headers) → тело ответа. body None — GET, иначе POST.
Fetch = Callable[[str, bytes | None, dict[str, str]], Awaitable[bytes]]


class UntisError(Exception):
    pass


@dataclass(frozen=True)
class Lesson:
    start: datetime          # местное время, без часового пояса
    end: datetime
    subject: str
    cancelled: bool = False
    changed: bool = False    # замена, другая аудитория и т. п.


@dataclass(frozen=True)
class UntisConfig:
    ical_url: str = ""
    server: str = ""         # например mese.webuntis.com
    school: str = ""
    user: str = ""
    password: str = ""
    tz: str = "Europe/Vienna"

    @property
    def mode(self) -> str:
        if self.ical_url:
            return "ical"
        if self.server and self.school and self.user and self.password:
            return "rpc"
        return ""

    @classmethod
    def from_env(cls, env: dict[str, str]) -> UntisConfig:
        g = lambda k: env.get(k, "").strip()          # noqa: E731
        return cls(ical_url=g("UNTIS_ICAL_URL"), server=g("UNTIS_SERVER"), school=g("UNTIS_SCHOOL"),
                   user=g("UNTIS_USER"), password=env.get("UNTIS_PASSWORD", ""),
                   tz=g("JARVIS_GOOGLE_TZ") or "Europe/Vienna")


async def urllib_fetch(url: str, body: bytes | None, headers: dict[str, str]) -> bytes:
    def run() -> bytes:
        req = urllib.request.Request(url, data=body, headers={"User-Agent": "jarvis", **headers})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read()
    return await asyncio.to_thread(run)


# ── iCal ──

def _eu_summer(utc: datetime) -> bool:
    """Летнее время ЕС (последнее воскресенье марта 01:00 UTC … октября 01:00 UTC): запасной вариант без tzdata."""
    def last_sunday(month: int) -> datetime:
        d = datetime(utc.year, month, 31, 1)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return last_sunday(3) <= utc.replace(tzinfo=None) < last_sunday(10)


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name)
    except Exception:       # на Windows и в минимальных образах базы часовых поясов может не быть
        return None


def _to_local(dt_utc: datetime, zone: ZoneInfo | None) -> datetime:
    """Время в UTC → местное (Вена), без tzinfo."""
    if zone is not None:
        return dt_utc.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)
    return dt_utc + timedelta(hours=2 if _eu_summer(dt_utc) else 1)


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _ics_time(name: str, params: dict[str, str], value: str, tz: ZoneInfo | None) -> datetime | None:
    """Время события → местное, без tzinfo. Целодневные (VALUE=DATE) возвращают None."""
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return None
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None
    dt = datetime.strptime(m[1] + m[2], "%Y%m%d%H%M%S")
    if m[3]:
        return _to_local(dt, tz)
    zone = _zone(params["TZID"]) if params.get("TZID") else None
    if zone is not None and tz is not None:
        return dt.replace(tzinfo=zone).astimezone(tz).replace(tzinfo=None)
    return dt           # время с поясом, которого не знаем, считаем местным


def parse_ical(text: str, tz: str = "Europe/Vienna") -> list[Lesson]:
    zone = _zone(tz)
    out: list[Lesson] = []
    ev: dict[str, tuple[dict[str, str], str]] | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            ev = {}
        elif line == "END:VEVENT" and ev is not None:
            lesson = _lesson_from_event(ev, zone)
            if lesson:
                out.append(lesson)
            ev = None
        elif ev is not None and ":" in line:
            head, _, value = line.partition(":")
            name, *plist = head.split(";")
            params = dict(p.split("=", 1) for p in plist if "=" in p)
            ev[name.upper()] = (params, value)
    return sorted(out, key=lambda x: x.start)


def _lesson_from_event(ev: dict[str, tuple[dict[str, str], str]], zone: ZoneInfo | None) -> Lesson | None:
    if "DTSTART" not in ev or "DTEND" not in ev:
        return None
    start = _ics_time("DTSTART", *ev["DTSTART"], zone)
    end = _ics_time("DTEND", *ev["DTEND"], zone)
    if start is None or end is None:
        return None
    summary = _ics_unescape(ev.get("SUMMARY", ({}, ""))[1]).strip()
    descr = _ics_unescape(ev.get("DESCRIPTION", ({}, ""))[1])
    status = ev.get("STATUS", ({}, ""))[1].upper()
    blob = f"{summary} {descr}".casefold()
    cancelled = status == "CANCELLED" or any(w in blob for w in CANCEL_WORDS)
    changed = any(w in blob for w in CHANGE_WORDS)
    return Lesson(start, end, summary or "урок", cancelled, changed and not cancelled)


# ── JSON-RPC ──

def _hhmm(n: int) -> tuple[int, int]:
    return n // 100, n % 100


def parse_timetable(rows: list[dict[str, Any]]) -> list[Lesson]:
    """Ответ getTimetable: date=ГГГГММДД, startTime=ЧЧММ; code 'cancelled' / 'irregular'."""
    out = []
    for r in rows:
        try:
            d = datetime.strptime(str(r["date"]), "%Y%m%d")
            sh, sm = _hhmm(int(r["startTime"]))
            eh, em = _hhmm(int(r["endTime"]))
        except (KeyError, ValueError, TypeError):
            continue
        subj = (r.get("su") or [{}])[0]
        name = subj.get("longname") or subj.get("name") or "урок"
        code = str(r.get("code", "")).lower()
        out.append(Lesson(d.replace(hour=sh, minute=sm), d.replace(hour=eh, minute=em), name,
                          cancelled=code == "cancelled", changed=code == "irregular" or bool(r.get("substText"))))
    return sorted(out, key=lambda x: x.start)


class Untis:
    def __init__(self, cfg: UntisConfig, *, fetch: Fetch = urllib_fetch,
                 clock: Callable[[], float] | None = None):
        import time
        self.cfg = cfg
        self.fetch = fetch
        self._clock = clock or time.monotonic
        self._cache: dict[date, tuple[float, list[Lesson]]] = {}
        self._ical: tuple[float, list[Lesson]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.mode)

    async def lessons(self, day: date) -> list[Lesson]:
        """Уроки на день, по порядку. Отменённые остаются в списке с cancelled=True."""
        if not self.enabled:
            raise UntisError("WebUntis не настроен: нужен UNTIS_ICAL_URL или UNTIS_SERVER/SCHOOL/USER/PASSWORD")
        now = self._clock()
        if self.cfg.mode == "ical":
            if self._ical is None or now - self._ical[0] > CACHE_S:
                raw = await self.fetch(self.cfg.ical_url, None, {})
                self._ical = (now, parse_ical(raw.decode("utf-8", "replace"), self.cfg.tz))
            return [x for x in self._ical[1] if x.start.date() == day]
        hit = self._cache.get(day)
        if hit is None or now - hit[0] > CACHE_S:
            self._cache[day] = (now, await self._rpc_day(day))
        return self._cache[day][1]

    async def first_lesson(self, day: date) -> Lesson | None:
        """Первый урок, который состоится (отменённые пропускаются)."""
        return next((x for x in await self.lessons(day) if not x.cancelled), None)

    async def _rpc(self, url: str, method: str, params: dict[str, Any], cookie: str = "") -> Any:
        body = json.dumps({"id": "jarvis", "method": method, "params": params, "jsonrpc": "2.0"}).encode()
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"JSESSIONID={cookie}"
        data = json.loads(await self.fetch(url, body, headers))
        if data.get("error"):
            raise UntisError(f"WebUntis: {data['error'].get('message', data['error'])}")
        return data.get("result")

    async def _rpc_day(self, day: date) -> list[Lesson]:
        c = self.cfg
        url = f"https://{c.server}/WebUntis/jsonrpc.do?school={quote(c.school)}"
        auth = await self._rpc(url, "authenticate", {"user": c.user, "password": c.password, "client": "jarvis"})
        sid = auth["sessionId"]
        try:
            n = int(day.strftime("%Y%m%d"))
            rows = await self._rpc(url, "getTimetable", {
                "options": {"element": {"id": auth["personId"], "type": auth.get("personType", 5)},
                            "startDate": n, "endDate": n, "showSubstText": True,
                            "klasseFields": ["id"], "subjectFields": ["name", "longname"]}}, sid)
        finally:
            try:
                await self._rpc(url, "logout", {}, sid)
            except Exception:
                pass
        return parse_timetable(rows or [])
