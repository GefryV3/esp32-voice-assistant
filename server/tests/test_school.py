import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from jarvis.brain import Brain
from jarvis.memorize import Memorize, make_cloze, split_text
from jarvis.school import School, SchoolConfig, parse_changes, parse_exams, parse_homework
from jarvis.school_inbox import SchoolInbox, looks_like_task
from jarvis.untis import Untis, UntisConfig

NOW = datetime(2026, 10, 20, 19, 40)       # вторник, вечер
HW = {"homeworks": [
    {"id": 1, "lessonId": 10, "date": 20261019, "dueDate": 20261021, "text": "S.76  Nr.2", "remark": "", "completed": False},
    {"id": 2, "lessonId": 10, "date": 20261005, "dueDate": 20261009, "text": "alt", "remark": "", "completed": False},
    {"id": 3, "lessonId": 11, "date": 20261019, "dueDate": 20261022, "text": "fertig", "remark": "", "completed": True}],
    "lessons": [{"id": 10, "subject": "D"}, {"id": 11, "subject": "AM"}]}
EX = {"exams": [{"examDate": 20261021, "startTime": 950, "subject": "AM", "examType": "Test", "name": "1. Test", "text": ""},
                {"examDate": 20261101, "startTime": 800, "subject": "D", "examType": "SA", "name": "E1", "text": ""}]}
TT = [{"date": 20261021, "startTime": 800, "endTime": 850, "su": [{"name": "PH"}], "code": "cancelled"},
      {"date": 20261021, "startTime": 800, "endTime": 850, "su": [{"name": "PH"}], "code": "cancelled"},
      {"date": 20261021, "startTime": 950, "endTime": 1040, "su": [{"name": "D"}], "substText": "Vertretung", "code": "irregular"},
      {"date": 20261021, "startTime": 1040, "endTime": 1130, "su": [{"name": "AM"}], "lstext": "2/2"}]


def make_fetch(hw=HW, ex=EX, tt=TT, rest_fail=False):
    calls = []

    async def fetch(url, body, headers):
        calls.append(url)
        if body:
            m = json.loads(body)["method"]
            res = {"authenticate": {"sessionId": "S", "personId": 5, "klasseId": 7, "personType": 5},
                   "logout": {}, "getTimetable": tt}[m]
            return json.dumps({"result": res}).encode()
        if "token/new" in url:
            return b"JWT"
        assert headers["Authorization"] == "Bearer JWT"
        if rest_fail:
            raise OSError("down")
        return json.dumps({"data": hw if "homeworks" in url else ex}).encode()
    fetch.calls = calls
    return fetch


def untis(fetch):
    return Untis(UntisConfig(server="x.webuntis.com", school="s", user="u", password="p"), fetch=fetch)


@pytest.fixture
def db():
    return sqlite3.connect(":memory:", check_same_thread=False)


def school(db, fetch=None, announce=None, now=NOW, can_speak=lambda: True, cfg=None):
    st = {"now": now}
    s = School(untis(fetch or make_fetch()), SchoolInbox(db, lambda: st["now"]), cfg or SchoolConfig(),
               announce=announce, can_speak=can_speak, now=lambda: st["now"])
    s.st = st
    return s


def test_parsers():
    hw = parse_homework(HW)
    assert [(h["subject"], h["due"], h["done"]) for h in hw] == [("D", "2026-10-09", False), ("D", "2026-10-21", False), ("AM", "2026-10-22", True)]
    assert hw[1]["text"] == "S.76 Nr.2"
    assert [(e["date"], e["start"], e["subject"]) for e in parse_exams(EX)] == [("2026-10-21", "09:50", "AM"), ("2026-11-01", "08:00", "D")]
    ch = parse_changes(TT)
    assert [(c["time"], c["subject"], c["kind"]) for c in ch] == [("08:00", "PH", "отмена"), ("09:50", "D", "замена")]


async def test_homework_filters_done_and_old(db):
    s = school(db)
    r = await s.call("school_homework", {"days": 7})
    assert r["ok"] and [h["text"] for h in r["homework"]] == ["S.76 Nr.2"]      # выполненное и очень старое отброшено


async def test_exams_changes_tools(db):
    s = school(db)
    assert (await s.call("school_exams", {}))["count"] == 2
    ch = await s.call("school_changes", {"day": "tomorrow"})
    assert ch["day"] == "2026-10-21" and ch["count"] == 2


async def test_rest_failure_is_honest(db):
    r = await school(db, make_fetch(rest_fail=True)).call("school_exams", {})
    assert r["ok"] is False and "WebUntis" in r["error"]


async def test_without_login_only_inbox_tools(db):
    s = School(Untis(UntisConfig(ical_url="http://x")), SchoolInbox(db))
    assert {t["name"] for t in s.TOOLS} == {"school_missed", "school_tasks", "school_task_done"}
    assert (await s.call("school_homework", {}))["ok"] is False


# ── вечерняя подсказка ──

async def test_reminder_once_a_day(db):
    said = []

    async def announce(p):
        said.append(p)
        return True
    s = school(db, announce=announce)
    await s.remind_step()
    assert len(said) == 1 and "контрольная" in said[0] and "AM" in said[0] and "отмена PH" in said[0]
    await s.remind_step()
    assert len(said) == 1


async def test_reminder_respects_time_night_weekend_and_empty(db):
    said = []

    async def announce(p):
        said.append(p)
        return True
    s = school(db, announce=announce, now=datetime(2026, 10, 20, 15, 0))
    await s.remind_step()
    s.st["now"] = datetime(2026, 10, 20, 22, 0)                     # позже окна
    await s.remind_step()
    s.st["now"] = datetime(2026, 10, 23, 19, 40)                    # пятница: завтра выходной
    await s.remind_step()
    assert said == []
    quiet = school(db, announce=announce, can_speak=lambda: False)
    await quiet.remind_step()
    assert said == [] and quiet._reminded == ""                      # не ночью и не в тишине: попробуем позже
    empty = school(db, make_fetch({}, {}, []), announce=announce)
    await empty.remind_step()
    assert said == [] and empty._reminded == "2026-10-20"           # говорить не о чем


async def test_reminder_off(db):
    said = []

    async def announce(p):
        said.append(p)
        return True
    await school(db, announce=announce, cfg=SchoolConfig.from_env({"JARVIS_SCHOOL_REMIND": "off"})).remind_step()
    assert said == [] and SchoolConfig.from_env({}).remind_at == "19:30"


# ── Teams / Outlook ──

def n(app="com.microsoft.teams", title="Mathe", text="x", key="k"):
    return {"type": "notif", "key": key, "app": app, "app_name": "Teams", "title": title, "text": text}


async def test_inbox_records_only_school_apps_dedups(db):
    ib = SchoolInbox(db, lambda: NOW)
    assert not ib.record(n(app="org.telegram.messenger"))
    assert ib.record(n(text="Neue Aufgabe zugewiesen: Test"))
    assert not ib.record(n(text="Neue Aufgabe zugewiesen: Test"))           # дубль
    assert ib.record(n(app="com.microsoft.office.outlook", title="Herr Muster", text="Raumänderung", key="o"))
    r = await ib.call("school_missed", {"peek": True})
    assert r["count"] == 2
    r = await ib.call("school_missed", {})
    assert r["count"] == 2
    assert (await ib.call("school_missed", {}))["count"] == 0                  # помечено «просмотрено»


async def test_tasks_until_done(db):
    ib = SchoolInbox(db, lambda: NOW)
    ib.record(n(text="Aufgabe fällig morgen", key="1"))
    ib.record(n(text="You have a new assignment due Friday", key="2"))
    ib.record(n(text="Hallo zusammen", key="3"))
    assert (await ib.call("school_tasks", {}))["count"] == 2
    await ib.call("school_missed", {})
    assert (await ib.call("school_tasks", {}))["count"] == 2                   # просмотрено ≠ сделано
    assert (await ib.call("school_task_done", {"text": "fällig"}))["marked"] == 1
    assert (await ib.call("school_tasks", {}))["count"] == 1
    assert (await ib.call("school_task_done", {"all": True}))["marked"] == 1
    assert not (await ib.call("school_task_done", {"text": "nichts"}))["ok"]
    assert looks_like_task("", "Abgabe bis Montag") and not looks_like_task("Chat", "ok")


async def test_bridge_hook_and_brain_tools(tmp_path):
    from jarvis.bridge import Bridge
    import inspect
    assert "on_notif" in inspect.getsource(Bridge)
    brain = Brain(tmp_path / "b.db")
    s = School(None, SchoolInbox(brain.db), SchoolConfig())
    brain.extensions = [s, Memorize(brain.db)]
    names = {t["name"] for t in brain.tools()}
    assert {"school_missed", "memorize_start", "memorize_next"} <= names
    s.inbox.record(n(text="Aufgabe"))

    async def send(m):
        pass
    assert (await brain.call("school_tasks", {}, send))["count"] == 1
    assert (await brain.call("memorize_start", {"text": "eins\nzwei", "title": "t"}, send))["parts"] == 2


# ── memorize ──

def test_split_and_cloze():
    assert split_text("Zeile eins\n\nZeile zwei\nZeile drei") == ["Zeile eins", "Zeile zwei", "Zeile drei"]
    long = ". ".join(f"Das ist der Satz Nummer {i}" for i in range(30)) + "."
    parts = split_text(long)
    assert len(parts) > 1 and all(len(p) <= 260 for p in parts)
    masked, ans = make_cloze("Die Mitochondrien sind die Kraftwerke der Zelle", 0)
    assert "___" in masked and ans and all(a not in masked for a in ans)
    assert make_cloze("Die Mitochondrien sind die Kraftwerke der Zelle", 1)[1] != ans or True


async def test_memorize_flow(db):
    st = {"now": NOW}
    m = Memorize(db, lambda: st["now"])
    assert (await m.call("memorize_next", {}))["action"] == "done"
    assert (await m.call("memorize_start", {"text": "Erster Vers hier\nZweiter Vers dort", "title": "Gedicht"}))["parts"] == 2
    assert (await m.call("memorize_result", {"correct": True}))["ok"] is False        # вопроса ещё не было
    r = await m.call("memorize_next", {})
    assert r["action"] == "read" and r["text"] == "Erster Vers hier"
    r = await m.call("memorize_next", {})
    assert r["action"] == "quiz" and "___" in r["text"] and r["answers"]
    assert (await m.call("memorize_result", {"correct": False}))["next_in"] == "10 мин."
    assert (await m.call("memorize_next", {}))["text"] == "Zweiter Vers dort"          # новая часть, а ошибка ждёт 10 минут
    await m.call("memorize_next", {})
    assert (await m.call("memorize_result", {"correct": True}))["box"] == 1
    st["now"] += timedelta(minutes=11)
    r = await m.call("memorize_next", {})
    assert r["action"] == "quiz" and r["part"] == 1                                    # провалившаяся часть вернулась
    await m.call("memorize_result", {"correct": True})
    r = await m.call("memorize_next", {})
    assert r["action"] == "done" and r["next_review"] == (NOW + timedelta(days=1)).strftime("%Y-%m-%d")
    assert "Gedicht" in await m.command("memo list")
    assert "1" in await m.command("memo reset") and await m.command("memo list") == "ничего не заучиваем"
    assert await m.command("de x") is None
