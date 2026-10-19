import json
from datetime import datetime

import pytest

from jarvis.brain import Brain
from jarvis.google import TOOLS, Google, GoogleError

NOW = datetime(2026, 10, 19, 9, 0)


class FakeHttp:
    """Вместо Google: отвечает по подстроке адреса и запоминает все запросы."""

    def __init__(self, token_ok=True):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self.token_ok = token_ok

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data and headers.get("Content-Type") == "application/json" else None
        self.calls.append((method, url, headers, body))
        if "oauth2.googleapis.com/token" in url:
            return (200, {"access_token": "AT", "expires_in": 3600}) if self.token_ok else (
                400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        if url.endswith("/users/me/calendarList") or "/calendarList?" in url:
            return 200, {"items": [{"id": "primary", "selected": True}, {"id": "off@x", "selected": False}]}
        if "/calendars/primary/events" in url and method == "GET":
            return 200, {"items": [
                {"summary": "Встреча", "start": {"dateTime": "2026-10-19T14:00:00+02:00"}},
                {"summary": "День рождения", "start": {"date": "2026-10-20"}}]}
        if "/calendars/primary/events" in url and method == "POST":
            return 200, {"id": "e1"}
        if "gmail" in url and url.rstrip("/").endswith("/messages") or "/messages?" in url:
            return 200, {"messages": [{"id": "m1"}]}
        if "/messages/m1" in url:
            return 200, {"payload": {"headers": [{"name": "From", "value": '"Банк" <a@bank.at>'},
                                                  {"name": "Subject", "value": "Выписка"}]}}
        if "/lists/@default/tasks" in url and method == "GET":
            return 200, {"items": [{"id": "t1", "title": "Купить хлеб"}, {"id": "t2", "title": "Позвонить маме"}]}
        if "/lists/@default/tasks" in url and method in ("POST", "PATCH"):
            return 200, {"id": "t9"}
        return 404, {"error": {"message": url}}


@pytest.fixture
def http():
    return FakeHttp()


@pytest.fixture
def g(http):
    return Google("cid", "secret", "refresh", http=http, now=lambda: NOW)


async def test_calendar_list_merges_and_skips_unselected(g, http):
    out = await g.call("google_calendar_list", {"days": 2})
    assert out["events"] == ["сегодня в 14:00: Встреча", "завтра: День рождения"]
    assert not any("off%40x" in c[1] or "off@x" in c[1] for c in http.calls)
    assert http.calls[0][1].endswith("/token")
    assert http.calls[1][2]["Authorization"] == "Bearer AT"


async def test_token_is_cached(g, http):
    await g.call("google_tasks_list", {})
    await g.call("google_tasks_list", {})
    assert sum("oauth2" in c[1] for c in http.calls) == 1


async def test_calendar_add_sends_timezone(g, http):
    out = await g.call("google_calendar_add", {"summary": "Зубной", "start": "2026-10-20T08:15", "duration_min": 30})
    assert out == {"ok": True, "when": "завтра в 08:15"}
    body = http.calls[-1][3]
    assert body["start"] == {"dateTime": "2026-10-20T08:15:00", "timeZone": "Europe/Vienna"}
    assert body["end"]["dateTime"] == "2026-10-20T08:45:00"
    assert (await g.call("google_calendar_add", {"summary": "x", "start": "вчера"}))["ok"] is False


async def test_mail_and_tasks(g, http):
    assert (await g.call("google_mail_unread", {"limit": 3}))["unread"] == ["Банк: Выписка"]
    assert (await g.call("google_tasks_list", {}))["tasks"] == ["Купить хлеб", "Позвонить маме"]
    assert (await g.call("google_tasks_done", {"title": "хлеб"}))["done"] == ["Купить хлеб"]
    assert http.calls[-1][0] == "PATCH" and http.calls[-1][3] == {"status": "completed"}
    await g.call("google_tasks_add", {"title": "Заметка", "due": "2026-10-25"})
    assert http.calls[-1][3] == {"title": "Заметка", "due": "2026-10-25T00:00:00.000Z"}


async def test_revoked_token_gives_clear_error():
    bad = Google("c", "s", "r", http=FakeHttp(token_ok=False), now=lambda: NOW)
    out = await bad.call("google_tasks_list", {})
    assert out["ok"] is False and "google_auth.py" in out["error"] and "revoked" in out["error"]
    with pytest.raises(GoogleError):
        await bad.api("GET", "https://x")


async def test_brain_routes_google_tools(g, tmp_path):
    brain = Brain(tmp_path / "b.db", google=g)
    assert {t["name"] for t in TOOLS} <= {t["name"] for t in brain.tools()}

    async def send(msg):
        pass
    assert (await brain.call("google_tasks_list", {}, send))["tasks"]
    assert "google_tasks_list" not in {t["name"] for t in Brain(tmp_path / "c.db").tools()}
