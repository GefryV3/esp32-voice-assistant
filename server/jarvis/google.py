"""Google Календарь, Gmail и Задачи напрямую, без Home Assistant.

Доступ по OAuth: один раз `python tools/google_auth.py` (вход делает сам пользователь в браузере),
дальше сервер сам обновляет короткий токен по refresh-токену из .env. Запросы к REST API идут
через urllib в потоке, без google-api-python-client. Gemini зовёт функции google_*; ответы —
короткие русские строки, пригодные для речи.

Keep здесь нет: официального API у личных аккаунтов нет, заметки уходят в Google Задачи.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TASKS = "https://tasks.googleapis.com/tasks/v1"
SCOPES = ("https://www.googleapis.com/auth/calendar",
          "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/tasks")
MAX_CALENDARS = 6
TIMEOUT_S = 20

TOOLS: list[dict[str, Any]] = [
    {"name": "google_calendar_list", "description": "События Google Календаря на ближайшие дни "
     "(всех календарей). days: сколько дней вперёд, по умолчанию 1 (сегодня и завтра — 2).",
     "parameters": {"type": "OBJECT", "properties": {"days": {"type": "NUMBER"}}}},
    {"name": "google_calendar_add", "description": "Добавить событие в Google Календарь. start — "
     "местное время YYYY-MM-DDTHH:MM, duration_min — длительность в минутах (по умолчанию 60).",
     "parameters": {"type": "OBJECT", "properties": {
         "summary": {"type": "STRING"}, "start": {"type": "STRING"}, "duration_min": {"type": "NUMBER"},
         "description": {"type": "STRING"}}, "required": ["summary", "start"]}},
    {"name": "google_mail_unread", "description": "Непрочитанные письма во входящих: от кого и тема. "
     "query — необязательный поиск Gmail («from:bank», «is:important»).",
     "parameters": {"type": "OBJECT", "properties": {
         "limit": {"type": "NUMBER"}, "query": {"type": "STRING"}}}},
    {"name": "google_tasks_list", "description": "Невыполненные задачи и заметки из Google Задач.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "google_tasks_add", "description": "Добавить задачу или заметку в Google Задачи "
     "(видна в приложении на телефоне). due — необязательный срок YYYY-MM-DD.",
     "parameters": {"type": "OBJECT", "properties": {
         "title": {"type": "STRING"}, "due": {"type": "STRING"}}, "required": ["title"]}},
    {"name": "google_tasks_done", "description": "Отметить задачу выполненной по подстроке названия.",
     "parameters": {"type": "OBJECT", "properties": {"title": {"type": "STRING"}}, "required": ["title"]}},
]


class GoogleError(Exception):
    pass


def default_http(method: str, url: str, headers: dict, data: bytes | None) -> tuple[int, Any]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def when_words(dt: datetime, now: datetime, all_day: bool = False) -> str:
    if dt.date() == now.date():
        day = "сегодня"
    elif dt.date() == (now + timedelta(days=1)).date():
        day = "завтра"
    else:
        day = f"{dt:%d.%m}"
    return day if all_day else f"{day} в {dt:%H:%M}"


class Google:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, tz: str = "Europe/Vienna", *,
                 http: Callable = default_http, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.tz, self._http, self._clock, self.now = tz, http, clock, now
        self._token = ""
        self._expires = 0.0

    # ── доступ ──

    async def _access_token(self) -> str:
        if self._token and self._clock() < self._expires - 60:
            return self._token
        body = urllib.parse.urlencode({
            "client_id": self.client_id, "client_secret": self.client_secret,
            "refresh_token": self.refresh_token, "grant_type": "refresh_token"}).encode()
        status, data = await asyncio.to_thread(
            self._http, "POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        if status != 200 or "access_token" not in data:
            reason = data.get("error_description") or data.get("error") or status
            raise GoogleError(f"Google не дал доступ: {reason}. Запусти tools/google_auth.py заново")
        self._token = data["access_token"]
        self._expires = self._clock() + float(data.get("expires_in", 3600))
        return self._token

    async def api(self, method: str, url: str, params: dict | None = None, body: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {await self._access_token()}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        status, out = await asyncio.to_thread(self._http, method, url, headers, data)
        if status >= 400:
            msg = (out.get("error") or {}).get("message", "") if isinstance(out.get("error"), dict) else out
            raise GoogleError(f"Google ответил {status}: {msg}")
        return out

    # ── календарь ──

    async def calendar_list(self, days: float = 1) -> dict:
        now = self.now().astimezone()
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=max(1, min(int(days), 31)) + 0)
        cals = (await self.api("GET", f"{CAL}/users/me/calendarList",
                               {"minAccessRole": "reader"})).get("items", [])
        cals = [c for c in cals if c.get("selected", True)][:MAX_CALENDARS]
        events: list[tuple[datetime, str, bool]] = []
        for c in cals:
            items = (await self.api("GET", f"{CAL}/calendars/{urllib.parse.quote(c['id'], safe='')}/events", {
                "timeMin": now.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                "orderBy": "startTime", "maxResults": 15})).get("items", [])
            for e in items:
                start = e.get("start", {})
                if "dateTime" in start:
                    events.append((datetime.fromisoformat(start["dateTime"]), e.get("summary", "без названия"), False))
                elif "date" in start:
                    events.append((datetime.fromisoformat(start["date"]).astimezone(), e.get("summary", "без названия"), True))
        events.sort(key=lambda x: x[0])
        return {"events": [f"{when_words(d, now, allday)}: {name}" for d, name, allday in events[:15]]}

    async def calendar_add(self, summary: str, start: str, duration_min: float = 60, description: str = "") -> dict:
        begin = datetime.fromisoformat(start)
        finish = begin + timedelta(minutes=max(5, float(duration_min or 60)))
        body: dict[str, Any] = {"summary": summary,
                                "start": {"dateTime": begin.replace(tzinfo=None).isoformat(), "timeZone": self.tz},
                                "end": {"dateTime": finish.replace(tzinfo=None).isoformat(), "timeZone": self.tz}}
        if description:
            body["description"] = description
        await self.api("POST", f"{CAL}/calendars/primary/events", body=body)
        return {"ok": True, "when": when_words(begin, self.now())}

    # ── почта ──

    async def mail_unread(self, limit: float = 5, query: str = "") -> dict:
        q = "is:unread in:inbox " + query
        ids = (await self.api("GET", f"{GMAIL}/messages", {"q": q.strip(), "maxResults": max(1, min(int(limit), 10))})
               ).get("messages", [])
        out = []
        for m in ids:
            msg = await self.api("GET", f"{GMAIL}/messages/{m['id']}", {
                "format": "metadata", "metadataHeaders": ["From", "Subject"]})
            h = {x["name"]: x["value"] for x in msg.get("payload", {}).get("headers", [])}
            sender = h.get("From", "").split("<")[0].strip().strip('"') or h.get("From", "неизвестно")
            out.append(f"{sender}: {h.get('Subject', 'без темы')}")
        return {"unread": out, "count": len(out)}

    # ── задачи ──

    async def tasks_list(self) -> dict:
        items = (await self.api("GET", f"{TASKS}/lists/@default/tasks",
                                {"showCompleted": "false", "maxResults": 30})).get("items", [])
        return {"tasks": [t.get("title", "") for t in items if t.get("title")]}

    async def tasks_add(self, title: str, due: str = "") -> dict:
        body: dict[str, Any] = {"title": title.strip()}
        if due:
            body["due"] = f"{datetime.fromisoformat(due):%Y-%m-%d}T00:00:00.000Z"
        await self.api("POST", f"{TASKS}/lists/@default/tasks", body=body)
        return {"ok": True}

    async def tasks_done(self, title: str) -> dict:
        needle = title.casefold()
        items = (await self.api("GET", f"{TASKS}/lists/@default/tasks",
                                {"showCompleted": "false", "maxResults": 100})).get("items", [])
        hit = [t for t in items if needle in t.get("title", "").casefold()]
        for t in hit:
            await self.api("PATCH", f"{TASKS}/lists/@default/tasks/{t['id']}", body={"status": "completed"})
        return {"ok": True, "done": [t["title"] for t in hit]}

    # ── вход для Gemini ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        try:
            if name == "google_calendar_list":
                return await self.calendar_list(args.get("days") or 1)
            if name == "google_calendar_add":
                return await self.calendar_add(str(args.get("summary", "")), str(args.get("start", "")),
                                               args.get("duration_min") or 60, str(args.get("description", "")))
            if name == "google_mail_unread":
                return await self.mail_unread(args.get("limit") or 5, str(args.get("query", "")))
            if name == "google_tasks_list":
                return await self.tasks_list()
            if name == "google_tasks_add":
                return await self.tasks_add(str(args.get("title", "")), str(args.get("due", "")))
            if name == "google_tasks_done":
                return await self.tasks_done(str(args.get("title", "")))
        except ValueError as e:
            return {"ok": False, "error": f"неверные параметры: {e}"}
        except GoogleError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}
