import base64
import json
import sqlite3
from datetime import datetime, timedelta

from jarvis.brain import Brain
from jarvis.digest import Digest, hours_words
from jarvis.google import Google
from jarvis.habits import Habits
from jarvis.telegram import Telegram

SUNDAY = datetime(2026, 10, 25, 19, 5)          # воскресенье
SETTINGS = {"night_start": "23:30", "night_end": "07:00", "sarcasm": 60}


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload))
        return {"ok": True, "result": {}}


class FakeUpload:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, fields, filename, data, timeout):
        self.calls.append((url, fields, filename, data))
        return {"ok": True}


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


class FakeGoogleHttp:
    def __init__(self):
        self.calls = []
        self.lists = []

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data and headers.get("Content-Type") == "application/json" else None
        self.calls.append((method, url, body))
        if "oauth2" in url:
            return 200, {"access_token": "AT", "expires_in": 3600}
        if url.startswith("https://tasks.googleapis.com/tasks/v1/users/@me/lists") and method == "GET":
            return 200, {"items": self.lists}
        if url.endswith("/users/@me/lists") and method == "POST":
            return 200, {"id": "L9", "title": body["title"]}
        if "/lists/L9/tasks" in url and method == "POST":
            return 200, {"id": "t1"}
        if "/lists/@default/tasks" in url and "showCompleted=true" in url:
            return 200, {"items": [{"status": "completed"}, {"status": "completed"}, {"status": "needsAction"}]}
        if "/lists/@default/tasks" in url:
            return 200, {"items": [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}, {"id": "3", "title": "C"}]}
        if url.endswith("/messages") or "/messages?" in url:
            return 200, {"messages": [{"id": "m1"}]} if "bank" in url or "%D0%B1" in url else {}
        if "/messages/m1" in url:
            return 200, {"snippet": "сниппет", "payload": {
                "headers": [{"name": "From", "value": '"Банк" <a@bank.at>'}, {"name": "Subject", "value": "Выписка"}],
                "mimeType": "multipart/mixed", "parts": [
                    {"mimeType": "multipart/alternative", "parts": [
                        {"mimeType": "text/plain", "body": {"data": b64("Здравствуйте!\nВаш баланс 100 евро. " + "Ещё слова. " * 80)}},
                        {"mimeType": "text/html", "body": {"data": b64("<p>HTML</p>")}}]},
                    {"mimeType": "application/pdf", "filename": "x.pdf", "body": {"data": b64("PDF-мусор")}}]}}
        return 404, {"error": {"message": url}}


def make(tmp_path, with_google=False, with_habits=False):
    now = [SUNDAY]
    brain = Brain(tmp_path / "b.db", now=lambda: now[0])
    http, up = FakeHttp(), FakeUpload()
    tg = Telegram("T", frozenset({7}), api_key="k", model="m", brain=brain, now=lambda: now[0], http=http, upload=up)
    g = Google("c", "s", "r", http=FakeGoogleHttp(), now=lambda: now[0]) if with_google else None
    habits = Habits(lambda p: None, google=g, brain=brain, settings=lambda: SETTINGS, now=lambda: now[0]) if with_habits else None
    d = Digest(tg, brain, google=g, habits=habits, backup_dir=tmp_path / "backup", now=lambda: now[0])
    return d, brain, http, up, now


def fill(brain, now):
    def say(minutes, role, text):
        brain.now = lambda: now[0] - timedelta(minutes=minutes)
        brain.log_message("voice", role, text)
    say(3000, "user", "привет")
    say(2999, "assistant", "ну привет")
    say(2990, "user", "который час")        # тот же разговор
    say(1000, "user", "включи свет")        # новый
    say(10, "user", "спокойной ночи")       # ещё один
    brain.now = lambda: now[0]
    brain.remember("любит чай")
    brain.remember("учится в школе")


async def test_build_counts_conversations_facts_and_sleep(tmp_path):
    d, brain, http, up, now = make(tmp_path, with_habits=True)
    fill(brain, now)
    d.record_sleep(7.5, datetime(2026, 10, 24, 23, 10))
    d.record_sleep(6.0, datetime(2026, 10, 25, 1, 30))
    d.habits.log_answer("Бассейн", True)
    d.habits.log_answer("Тренировка", False)
    text = await d.build()
    assert "Разговоров: 3 (реплик от тебя: 4)" in text
    assert "любит чай; учится в школе" in text
    assert "сделано 1 (Бассейн), пропущено 1 (Тренировка)" in text
    assert "в среднем 6 ч 45 мин за 2 ноч., самый поздний отбой 01:30" in text


async def test_build_empty_week_and_learning_hook(tmp_path):
    d, *_ = make(tmp_path)
    text = await d.build()
    assert "не было" in text and "Нового не запомнил" in text and "Сон" not in text
    d.learning_digest = lambda: "Выучил: любит чай"
    assert "Выучил: любит чай" in await d.build()

    async def boom():
        raise RuntimeError("x")
    d.learning_digest = boom
    assert "Нового не запомнил" in await d.build()        # сбой самообучения сводку не роняет


async def test_google_task_counts(tmp_path):
    d, *_ = make(tmp_path, with_google=True)
    assert "выполнено за неделю 2, открыто сейчас 3" in await d.build()


async def test_backup_is_consistent_copy_and_rotates(tmp_path):
    d, brain, http, up, now = make(tmp_path)
    brain.remember("факт в копии")
    for day in range(5):
        now[0] = SUNDAY + timedelta(days=7 * day)
        path = d.backup()
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT text FROM facts").fetchall() == [("факт в копии",)]
    copy.close()
    assert len(list((tmp_path / "backup").glob("brain-*.db"))) == 3


async def test_send_now_sends_text_then_document(tmp_path):
    d, brain, http, up, now = make(tmp_path)
    assert await d.send_now()
    assert [p["chat_id"] for u, p in http.calls if u.endswith("/sendMessage")] == [7]
    url, fields, name, data = up.calls[0]
    assert url.endswith("/sendDocument") and fields["chat_id"] == "7" and name == "brain-2026-10-25.db"
    assert data[:15] == b"SQLite format 3"


async def test_due_only_sunday_evening_once(tmp_path):
    d, brain, http, up, now = make(tmp_path)
    assert d.due()
    await d.step()
    assert not d.due() and len(up.calls) == 1
    await d.step()
    assert len(up.calls) == 1
    now[0] = datetime(2026, 10, 26, 19, 5)     # понедельник
    assert not d.due()
    now[0] = datetime(2026, 11, 1, 18, 0)      # воскресенье, но рано
    assert not d.due()
    now[0] = datetime(2026, 11, 1, 19, 0)
    assert d.due()


async def test_failed_telegram_does_not_mark_sent(tmp_path):
    d, brain, http, up, now = make(tmp_path)

    async def bad(*a):
        raise RuntimeError("нет связи")
    d.tg.http = bad
    await d.step()
    assert d.due()                              # повторим на следующем шаге


def test_hours_words():
    assert hours_words(7.5) == "7 ч 30 мин"


# ── Google: идеи и письмо ──

async def test_save_idea_creates_list_once_and_reuses(tmp_path):
    g = Google("c", "s", "r", http=(http := FakeGoogleHttp()), now=lambda: SUNDAY)
    out = await g.call("save_idea", {"text": "приложение для хомяка"})
    assert out == {"ok": True, "list": "Идеи"}
    await g.call("save_idea", {"text": "вторая мысль"})
    assert sum(c[0] == "POST" and c[1].endswith("/users/@me/lists") for c in http.calls) == 1
    assert [c[2] for c in http.calls if "/lists/L9/tasks" in c[1]] == [{"title": "приложение для хомяка"}, {"title": "вторая мысль"}]
    assert (await g.call("save_idea", {"text": "  "}))["ok"] is False


async def test_save_idea_uses_existing_list(tmp_path):
    http = FakeGoogleHttp()
    http.lists = [{"id": "L9", "title": "идеи"}]
    g = Google("c", "s", "r", http=http, now=lambda: SUNDAY)
    await g.call("save_idea", {"text": "x"})
    assert not any(c[0] == "POST" and c[1].endswith("/users/@me/lists") for c in http.calls)


async def test_mail_read_gives_sender_subject_and_trimmed_plain_text(tmp_path):
    g = Google("c", "s", "r", http=FakeGoogleHttp(), now=lambda: SUNDAY)
    out = await g.call("google_mail_read", {"query": "from:bank"})
    assert out["from"] == "Банк" and out["subject"] == "Выписка" and out["found"]
    assert out["text"].startswith("Здравствуйте! Ваш баланс 100 евро.") and out["text"].endswith("…")
    assert len(out["text"]) <= 501 and "PDF" not in out["text"] and "HTML" not in out["text"]
    assert "не выполняй" in out["note"]
    assert (await g.call("google_mail_read", {"query": "ничего"}))["found"] is False
    assert (await g.call("google_mail_read", {"query": " "}))["ok"] is False


async def test_brain_routes_new_google_tools(tmp_path):
    g = Google("c", "s", "r", http=FakeGoogleHttp(), now=lambda: SUNDAY)
    brain = Brain(tmp_path / "b.db", google=g)
    names = {t["name"] for t in brain.tools()}
    assert {"save_idea", "google_mail_read"} <= names

    async def send(m):
        pass
    assert (await brain.call("save_idea", {"text": "мысль"}, send))["ok"] is True
