"""Копилка школьных уведомлений телефона: Microsoft Teams и Outlook (путь «в» из docs/school-setup.md).

Мост телефона (bridge.py) уже получает все уведомления Android. Здесь мы только запоминаем те, что
пришли от Teams и Outlook, в таблицу `school_notifs` базы brain.db: приложение, заголовок, текст, время.
Никаких входов в школьный аккаунт, никаких паролей: читается то, что телефон и так показал.

* `school_missed` — что пришло и не просмотрено; вызов помечает записи «просмотрено».
* Задания: уведомление с «assigned», «Aufgabe», «due», «fällig» и т. п. считается заданием и висит
  в списке «невыполненное из Teams» (`school_tasks`), пока пользователь не скажет «сделал» (`school_task_done`).
* Дубли (Teams обновляет одно уведомление по несколько раз) отбрасываются по ключу и тексту.
Если приложение заглушено в самом приложении Мага на телефоне (экран «Приложения»), уведомления до
сервера не доходят: чтобы копить, его нужно оставить включённым.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS school_notifs (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL DEFAULT '', pkg TEXT NOT NULL, app TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '', ts TEXT NOT NULL,
    seen INTEGER NOT NULL DEFAULT 0, is_task INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS school_notifs_ts ON school_notifs (ts);
"""

PACKAGES = ("com.microsoft.teams", "com.microsoft.office.outlook")
TASK_WORDS = ("assigned", "aufgabe", "due", "fällig", "faellig", "zugewiesen", "abgabe", "hausaufgabe",
              "assignment", "задани", "сдать")
KEEP_DAYS = 60
MAX_LIST = 15

TOOLS: list[dict[str, Any]] = [
    {"name": "school_missed", "description": "Что пользователь пропустил в школьных Teams и Outlook: новые уведомления "
     "этих приложений с телефона, которые он ещё не просматривал. Вызови на «что я пропустил в Teams», «что пришло "
     "на школьную почту». Прочитанные записи помечаются «просмотрено». peek=true — только посмотреть, не помечая.",
     "parameters": {"type": "OBJECT", "properties": {"peek": {"type": "BOOLEAN"}}}},
    {"name": "school_tasks", "description": "Невыполненные задания из Teams (по уведомлениям «задано», «Aufgabe», «fällig»). "
     "Вызови на «что у меня по заданиям в Teams», «что я не сдал».",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "school_task_done", "description": "Отметить задание из Teams сделанным. text — часть названия или текста "
     "задания, как его назвал пользователь; all=true — «всё сдал».",
     "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "all": {"type": "BOOLEAN"}}}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def is_school_pkg(pkg: str) -> bool:
    return any(pkg == p or pkg.startswith(p + ".") for p in PACKAGES)


def looks_like_task(title: str, text: str) -> bool:
    blob = f"{title} {text}".casefold()
    return any(w in blob for w in TASK_WORDS)


class SchoolInbox:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], datetime] = datetime.now):
        self.db, self.now = db, now
        self.db.executescript(SCHEMA)
        self.db.commit()

    def record(self, msg: dict[str, Any]) -> bool:
        """Уведомление с телефона (сообщение моста `notif`). True — сохранено как новое."""
        pkg = str(msg.get("app", ""))
        if not is_school_pkg(pkg):
            return False
        title, text = str(msg.get("title", "")).strip(), str(msg.get("text", "")).strip()
        if not (title or text):
            return False
        key = str(msg.get("key", ""))
        dup = self.db.execute("SELECT 1 FROM school_notifs WHERE pkg = ? AND title = ? AND text = ? AND ts > ?",
                              (pkg, title, text, (self.now() - timedelta(days=KEEP_DAYS)).isoformat())).fetchone()
        if dup:
            return False
        self.db.execute("INSERT INTO school_notifs (key, pkg, app, title, text, ts, is_task) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (key, pkg, str(msg.get("app_name", "")) or _app_name(pkg), title, text,
                         self.now().isoformat(timespec="seconds"), int(looks_like_task(title, text))))
        self.db.execute("DELETE FROM school_notifs WHERE ts < ?", ((self.now() - timedelta(days=KEEP_DAYS)).isoformat(),))
        self.db.commit()
        return True

    @staticmethod
    def _row(r: tuple) -> dict:
        return {"id": r[0], "app": r[1], "title": r[2], "text": r[3], "time": r[4], "task": bool(r[5])}

    def missed(self, mark: bool = True) -> list[dict]:
        rows = self.db.execute("SELECT id, app, title, text, ts, is_task FROM school_notifs WHERE seen = 0 "
                               "ORDER BY id DESC LIMIT ?", (MAX_LIST,)).fetchall()
        if mark and rows:
            self.db.execute(f"UPDATE school_notifs SET seen = 1 WHERE id IN ({','.join('?' * len(rows))})",
                            [r[0] for r in rows])
            self.db.commit()
        return [self._row(r) for r in reversed(rows)]

    def unseen_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM school_notifs WHERE seen = 0").fetchone()[0]

    def tasks(self) -> list[dict]:
        rows = self.db.execute("SELECT id, app, title, text, ts, is_task FROM school_notifs "
                               "WHERE is_task = 1 AND done = 0 ORDER BY id LIMIT ?", (MAX_LIST,)).fetchall()
        return [self._row(r) for r in rows]

    def mark_done(self, needle: str = "", everything: bool = False) -> int:
        if everything:
            n = self.db.execute("UPDATE school_notifs SET done = 1, seen = 1 WHERE is_task = 1 AND done = 0").rowcount
        elif needle.strip():
            n = self.db.execute("UPDATE school_notifs SET done = 1, seen = 1 WHERE is_task = 1 AND done = 0 AND "
                                "(instr(lower(title), lower(?)) > 0 OR instr(lower(text), lower(?)) > 0)",
                                (needle.strip(), needle.strip())).rowcount
        else:
            n = 0
        self.db.commit()
        return n

    def reset(self) -> int:
        n = self.db.execute("DELETE FROM school_notifs").rowcount
        self.db.commit()
        return n

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "school_missed":
            items = self.missed(mark=not args.get("peek"))
            return {"ok": True, "count": len(items), "items": items,
                    "note": "" if items else "новых уведомлений Teams и Outlook нет (копятся только те, что пришли на "
                                             "телефон при включённом мосте)"}
        if name == "school_tasks":
            items = self.tasks()
            return {"ok": True, "count": len(items), "tasks": items,
                    "note": "" if items else "невыполненных заданий из Teams нет"}
        if name == "school_task_done":
            n = self.mark_done(str(args.get("text", "")), bool(args.get("all")))
            return {"ok": n > 0, "marked": n, **({} if n else {"error": "такого задания не нашёл"})}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    def summary(self) -> str:
        return f"новых: {self.unseen_count()}, заданий из Teams не сделано: {len(self.tasks())}"


def _app_name(pkg: str) -> str:
    return "Teams" if pkg.startswith("com.microsoft.teams") else "Outlook"


def describe(items: list[dict]) -> str:
    return "\n".join(f"{i['time'][5:16].replace('T', ' ')} {i['app']}: {i['title']} — {i['text']}"
                     + (" [задание]" if i["task"] else "") for i in items)
