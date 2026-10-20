"""«Помоги запомнить» (идея #41): Мага режет текст на части, читает по частям, спрашивает пропущенные слова
и повторяет по интервалам.

Разговором ведёт модель, сервер держит то, что она не удержит: части текста, ящики Лейтнера и сроки
(как `de_words` в german.py, но своя таблица — немецкие слова не затронуты).

Ход занятия:
  memorize_start(text, title) → текст нарезан и сохранён, дальше модель вызывает memorize_next;
  memorize_next → шаг: `read` (прочитай часть вслух, дай повторить), `quiz` (прочитай часть с «пропусками»,
  пользователь называет пропущенные слова; ответы лежат в `answers`) или `done` (на сегодня всё);
  memorize_result(correct) → итог проверки части: верно — ящик выше, ошибка — снова сегодня.
Повторы: после верного ответа часть вернётся через 1, 3, 7, 14, 30 дней; в memorize_next сначала идут
части, чей срок пришёл, потом новые. Консоль: `memo list|reset`.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS memo_sets (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memo_chunks (
    id INTEGER PRIMARY KEY, set_id INTEGER NOT NULL, idx INTEGER NOT NULL, text TEXT NOT NULL,
    box INTEGER NOT NULL DEFAULT 0, hits INTEGER NOT NULL DEFAULT 0, misses INTEGER NOT NULL DEFAULT 0,
    read INTEGER NOT NULL DEFAULT 0, due TEXT NOT NULL);
"""

INTERVAL_DAYS = (0, 1, 3, 7, 14, 30)
RETRY_MIN = 10
MAX_CHUNK = 220            # знаков в части
MAX_TEXT = 12000
MIN_BLANK_LEN = 4

TOOLS: list[dict[str, Any]] = [
    {"name": "memorize_start", "description": "Помочь выучить текст наизусть (стих, определения, список, конспект). "
     "Вызови, когда пользователь просит «помоги выучить/запомнить»: text — сам текст, title — короткое название. "
     "Дальше веди занятие функциями memorize_next и memorize_result.",
     "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}, "title": {"type": "STRING"}},
                    "required": ["text"]}},
    {"name": "memorize_next", "description": "Следующий шаг заучивания. action=read: прочитай часть вслух, неспешно, "
     "и попроси повторить. action=quiz: прочитай часть, на месте пропусков (___) сделай паузу и спроси пропущенные "
     "слова; верные ответы в answers, проверь ответ сам. action=done: на сегодня всё, скажи когда повторить.",
     "parameters": {"type": "OBJECT", "properties": {"title": {"type": "STRING"}}}},
    {"name": "memorize_result", "description": "Итог проверки части после quiz: correct=true — назвал все пропущенные "
     "слова, false — ошибся или не вспомнил. Сервер сам назначит повтор.",
     "parameters": {"type": "OBJECT", "properties": {"correct": {"type": "BOOLEAN"}}, "required": ["correct"]}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}

_SENT = re.compile(r"(?<=[.!?…;:])\s+")
_WORD = re.compile(r"[^\W\d_]{%d,}" % MIN_BLANK_LEN, re.UNICODE)


def split_text(text: str) -> list[str]:
    """Нарезка: каждая строка (стих, пункт списка) — отдельная часть; длинные строки делим по предложениям,
    короткие соседние строки склеиваем, пока часть не длиннее MAX_CHUNK."""
    pieces: list[str] = []
    for line in text.replace("\r", "").split("\n"):
        line = " ".join(line.split())
        if not line:
            continue
        if len(line) <= MAX_CHUNK:
            pieces.append(line)
            continue
        cur = ""
        for s in _SENT.split(line):
            if cur and len(cur) + 1 + len(s) > MAX_CHUNK:
                pieces.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            pieces.append(cur)
    lines_are_items = len(pieces) >= 2 and all(len(p) <= 90 for p in pieces)       # список или стих: строки не склеиваем
    if lines_are_items:
        return pieces
    out: list[str] = []
    for p in pieces:
        if out and len(out[-1]) + 1 + len(p) <= MAX_CHUNK:
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def make_cloze(text: str, seed: int) -> tuple[str, list[str]]:
    """Часть с пропусками: прячем от 1 до 3 длинных слов, какие именно — зависит от seed (меняется от повтора к повтору)."""
    words = list(_WORD.finditer(text))
    if not words:
        words = list(re.finditer(r"\S+", text))
    if not words:
        return text, []
    k = max(1, min(3, len(words) // 6))
    step = max(len(words) // k, 1)
    picks = sorted({(seed + i * step) % len(words) for i in range(k)})
    out, last, answers = [], 0, []
    for i in picks:
        m = words[i]
        out.append(text[last:m.start()] + "___")
        answers.append(m.group())
        last = m.end()
    out.append(text[last:])
    return "".join(out), answers


class Memorize:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], datetime] = datetime.now):
        self.db, self.now = db, now
        self.db.executescript(SCHEMA)
        self.db.commit()
        self._current: tuple[int, str] | None = None        # (chunk_id, шаг) — ждём memorize_result после quiz

    @property
    def TOOLS(self) -> list[dict[str, Any]]:               # noqa: N802
        return TOOLS

    @property
    def tool_names(self) -> set[str]:
        return TOOL_NAMES

    # ── данные ──

    def start(self, text: str, title: str = "") -> dict:
        text = text.strip()[:MAX_TEXT]
        chunks = split_text(text)
        if not chunks:
            return {"ok": False, "error": "текст пустой"}
        title = (title.strip() or chunks[0][:40])[:60]
        now = self.now()
        cur = self.db.execute("INSERT INTO memo_sets (title, created) VALUES (?, ?)", (title, now.isoformat(timespec="seconds")))
        for i, c in enumerate(chunks):
            self.db.execute("INSERT INTO memo_chunks (set_id, idx, text, due) VALUES (?, ?, ?, ?)",
                            (cur.lastrowid, i, c, now.isoformat(timespec="seconds")))
        self.db.commit()
        self._current = None
        return {"ok": True, "title": title, "parts": len(chunks),
                "note": "текст нарезан; теперь зови memorize_next и веди занятие по шагам. Начни с короткого "
                        "«поехали» и первой части, не читай весь текст сразу"}

    def _pick(self, title: str = "") -> sqlite3.Row | tuple | None:
        now = self.now().isoformat(timespec="seconds")
        q = ("SELECT c.id, c.text, c.box, c.read, c.hits + c.misses, s.title, c.idx FROM memo_chunks c "
             "JOIN memo_sets s ON s.id = c.set_id WHERE c.due <= ? ")
        args: list[Any] = [now]
        if title.strip():
            q += "AND instr(lower(s.title), lower(?)) > 0 "
            args.append(title.strip())
        # сначала те, что уже учили и срок пришёл (самые путаные первыми), потом новые по порядку
        q += "ORDER BY (c.read = 0), c.misses DESC, c.due, s.id, c.idx LIMIT 1"
        return self.db.execute(q, args).fetchone()

    def next_step(self, title: str = "") -> dict:
        row = self._pick(title)
        if row is None:
            self._current = None
            nxt = self.db.execute("SELECT MIN(due) FROM memo_chunks WHERE due > ?",
                                  (self.now().isoformat(timespec="seconds"),)).fetchone()[0]
            if nxt is None:
                return {"ok": True, "action": "done", "note": "заучивать пока нечего: скажи, какой текст выучить"}
            return {"ok": True, "action": "done", "next_review": nxt[:10],
                    "note": f"на сегодня всё, следующий повтор {nxt[:10]}"}
        cid, text, box, was_read, tries, title_, idx = row
        total = self.db.execute("SELECT COUNT(*) FROM memo_chunks WHERE set_id = (SELECT set_id FROM memo_chunks WHERE id = ?)",
                                (cid,)).fetchone()[0]
        base = {"ok": True, "title": title_, "part": idx + 1, "of": total}
        if not was_read:
            self.db.execute("UPDATE memo_chunks SET read = 1 WHERE id = ?", (cid,))
            self.db.commit()
            self._current = (cid, "read")
            return {**base, "action": "read", "text": text,
                    "note": "прочитай часть вслух и попроси повторить; потом снова вызови memorize_next (проверка "
                            "этой части придёт следующим шагом)"}
        masked, answers = make_cloze(text, seed=tries + idx)
        self._current = (cid, "quiz")
        return {**base, "action": "quiz", "text": masked, "answers": answers, "box": box,
                "note": "прочитай текст с паузой на месте ___, спроси пропущенные слова, после ответа вызови memorize_result"}

    def result(self, correct: bool) -> dict:
        if self._current is None or self._current[1] != "quiz":
            return {"ok": False, "error": "сейчас нет вопроса: зови memorize_next"}
        cid = self._current[0]
        box = self.db.execute("SELECT box FROM memo_chunks WHERE id = ?", (cid,)).fetchone()[0]
        now = self.now()
        if correct:
            box = min(box + 1, len(INTERVAL_DAYS) - 1)
            due = now + timedelta(days=INTERVAL_DAYS[box])
            self.db.execute("UPDATE memo_chunks SET box = ?, hits = hits + 1, due = ? WHERE id = ?",
                            (box, due.isoformat(timespec="seconds"), cid))
        else:
            box = 0
            due = now + timedelta(minutes=RETRY_MIN)
            self.db.execute("UPDATE memo_chunks SET box = 0, misses = misses + 1, due = ? WHERE id = ?",
                            (due.isoformat(timespec="seconds"), cid))
        self.db.commit()
        self._current = None
        return {"ok": True, "box": box, "next_in": f"{INTERVAL_DAYS[box]} дн." if correct else f"{RETRY_MIN} мин.",
                "note": "дальше зови memorize_next"}

    def listing(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT s.id, s.title, COUNT(c.id), SUM(c.read), AVG(c.box), MIN(c.due) FROM memo_sets s "
            "LEFT JOIN memo_chunks c ON c.set_id = s.id GROUP BY s.id ORDER BY s.id").fetchall()
        return [{"id": r[0], "title": r[1], "parts": r[2], "read": r[3] or 0, "avg_box": round(r[4] or 0, 1),
                 "next": (r[5] or "")[:16]} for r in rows]

    def reset(self) -> int:
        n = self.db.execute("DELETE FROM memo_sets").rowcount
        self.db.execute("DELETE FROM memo_chunks")
        self.db.commit()
        self._current = None
        return n

    # ── Gemini и консоль ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "memorize_start":
            return self.start(str(args.get("text", "")), str(args.get("title", "")))
        if name == "memorize_next":
            return self.next_step(str(args.get("title", "")))
        if name == "memorize_result":
            return self.result(str(args.get("correct", "")).strip().lower() in ("true", "1", "да"))
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "memo":
            return None
        arg = parts[1].lower() if len(parts) > 1 else "list"
        if arg == "list":
            rows = self.listing()
            return "\n".join(f"#{r['id']} {r['title']}: частей {r['parts']}, прочитано {r['read']}, "
                             f"ящик {r['avg_box']}, следующий повтор {r['next']}" for r in rows) or "ничего не заучиваем"
        if arg == "reset":
            return f"удалено текстов: {self.reset()}"
        return "memo list|reset"
