"""Тренинг немецкого: режим языкового партнёра, слова и интервальное повторение.

Разговор ведёт сама модель. Сервер держит то, что модель не удержит: таблицу слов `de_words`
(что путалось, когда спросить снова), «слово дня сегодня уже было» и правила режима.

Подсказку Gemini Live после открытия сессии не поменять, поэтому режим включается так: в обычной
подсказке одна фраза («по просьбе вызови set_language_mode»), а сами правила режима модель
получает в РЕЗУЛЬТАТЕ функции и следует им до конца разговора. Если сессия открывается уже с
включённым режимом (консоль `de on`), те же правила попадают в системную подсказку
(`system_prompt(language=...)`), а «только по-русски» заменяется двуязычным абзацем.
Режим живёт на один разговор: `reset()` зовёт голос, когда разговор кончился.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS de_words (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, word TEXT NOT NULL, translation TEXT NOT NULL DEFAULT '',
    misses INTEGER NOT NULL DEFAULT 0, hits INTEGER NOT NULL DEFAULT 0, box INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT NOT NULL, due TEXT NOT NULL);
"""

# Ящики Лейтнера: после верного ответа слово вернётся через столько дней (ящик 0 — новое/путаное)
INTERVAL_DAYS = (0, 1, 3, 7, 14, 30)
RETRY_MIN = 10          # ошибся — спросим ещё раз в этом же занятии, через ~10 минут
LEVELS = ("A1", "A2", "A2-B1", "B1", "B2")
DEFAULT_LEVEL = "A2-B1"
SITUATIONS = ("школа", "магазин", "транспорт", "телефон", "кафе", "врач", "знакомство")
TASK_NAME = "Поучить немецкий"
MAX_WORD_LEN = 60

TOOLS: list[dict[str, Any]] = [
    {"name": "set_language_mode", "description": "Режим языкового партнёра. mode=german — пользователь сказал "
     "«давай по-немецки», «тренируем немецкий», «поговорим на немецком»: вызови и дальше строго следуй "
     "rules из ответа. mode=off — «хватит немецкого», «давай по-русски» (это НЕ конец разговора, "
     "end_conversation не зови). level: A1, A2, A2-B1, B1, B2 (если просили проще/сложнее). "
     "situation: школа, магазин, транспорт, телефон, кафе, врач (тренировка диалога по ситуации).",
     "parameters": {"type": "OBJECT", "properties": {
         "mode": {"type": "STRING"}, "level": {"type": "STRING"}, "situation": {"type": "STRING"}},
         "required": ["mode"]}},
    {"name": "de_word_save", "description": "Сохранить немецкое слово в словарь пользователя: слово дня или "
     "слово, в котором он ошибся. word — по-немецки (существительное с артиклем: der Bahnhof), "
     "translation — по-русски. word_of_day=true, если это слово дня.",
     "parameters": {"type": "OBJECT", "properties": {
         "word": {"type": "STRING"}, "translation": {"type": "STRING"}, "word_of_day": {"type": "BOOLEAN"}},
         "required": ["word"]}},
    {"name": "de_word_review", "description": "Записать итог проверки слова: correct=true — вспомнил, "
     "false — ошибся или не вспомнил. Сервер сам назначит, когда спросить снова.",
     "parameters": {"type": "OBJECT", "properties": {
         "word": {"type": "STRING"}, "correct": {"type": "BOOLEAN"}}, "required": ["word", "correct"]}},
    {"name": "de_words_due", "description": "Слова, которые пора повторить (сначала самые путаные). "
     "Спроси их по одному: слово по-немецки, пусть переведёт, или наоборот.",
     "parameters": {"type": "OBJECT", "properties": {"limit": {"type": "NUMBER"}}}},
]


def rules(level: str = DEFAULT_LEVEL, situation: str = "", due: list[dict] | None = None,
          word_of_day_given: bool = False, google: bool = False) -> str:
    """Правила режима для модели: уходят в результат set_language_mode и в системную подсказку."""
    parts = [
        f"Режим языкового партнёра: немецкий, уровень {level}. Пока режим включён, говори с пользователем "
        "ПО-НЕМЕЦКИ короткими простыми фразами (одна-две, слова из школьной программы). Пользователь "
        "учит язык для школы, русский знает. Говори чётко и не слишком быстро.",
        "Ошибку исправляй мягко и коротко: повтори фразу правильно по-немецки и в одном предложении по-русски "
        "объясни правило (артикль, порядок слов, падеж, спряжение). Не исправляй каждую мелочь подряд: "
        "самую важную ошибку за реплику. Хвали за удачные фразы коротко.",
        "Если пользователь не понял, переспроси проще, а при необходимости переведи на русский. Если он "
        "просит перевод или объяснение по-русски, ответь по-русски и вернись к немецкому.",
        "Колкость и характер остаются, но шути просто и по-немецки.",
    ]
    if not word_of_day_given:
        parts.append("«Слово дня» сегодня ещё не было: в подходящий момент дай одно полезное слово "
                     "(для школы или быта) с переводом и примером и сохрани его функцией de_word_save "
                     "с word_of_day=true.")
    if due:
        listing = "; ".join(f"{w['word']} — {w['translation'] or '?'}" + (f" (путал {w['misses']} р.)" if w["misses"] else "")
                            for w in due)
        parts.append(f"Пора повторить слова: {listing}. Вплети их в разговор по одному (спроси слово или "
                     "его перевод) и после ответа вызови de_word_review с correct true/false. Если он "
                     "ошибся в другом слове, сохрани его de_word_save.")
    else:
        parts.append("Слов на повторение пока нет; новые слова, в которых он ошибся, сохраняй de_word_save, "
                     "а проверенные записывай de_word_review.")
    if situation:
        parts.append(f"Сейчас тренируем диалог: {situation}. Играй роль собеседника (продавец, кондуктор, "
                     "учитель, оператор), начни реплику первым, держи сценку 5-8 реплик, в конце коротко "
                     "разбери главные ошибки по-русски.")
    else:
        parts.append("Можно предложить сценку: школа, магазин, транспорт, телефон. Если пользователь "
                     "согласен, играй роль собеседника.")
    if google:
        parts.append(f"В Google Задачах у пользователя бывает задача «{TASK_NAME}». В начале можно проверить "
                     "google_tasks_list; по итогам тренировки (минут десять разговора, пара слов повторена) "
                     "скажи, что задачу можно закрыть, и ТОЛЬКО если он согласился, вызови "
                     "google_tasks_done с text «немецк».")
    parts.append("Выход: «хватит немецкого», «давай по-русски» — вызови set_language_mode с mode=off, "
                 "коротко подведи итог по-русски и дальше говори только по-русски.")
    return " ".join(parts)


def prompt_paragraph(level: str = DEFAULT_LEVEL, situation: str = "") -> str:
    """Абзац системной подсказки вместо «Говори только по-русски» (сессия открыта уже в режиме)."""
    return rules(level, situation)


def _norm(word: str) -> str:
    return " ".join(word.split()).strip()[:MAX_WORD_LEN]


class German:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], datetime],
                 pref: Callable[[str, str], str], set_pref: Callable[[str, str], None],
                 has_google: Callable[[], bool] = lambda: False):
        self.db = db
        self.now = now
        self._pref = pref
        self._set_pref = set_pref
        self._has_google = has_google
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.active = False
        self.situation = ""
        self.session = {"saved": 0, "right": 0, "wrong": 0}

    names = {t["name"] for t in TOOLS}

    # ── режим ──

    @property
    def level(self) -> str:
        return self._pref("de_level", DEFAULT_LEVEL) or DEFAULT_LEVEL

    def set_level(self, level: str) -> str | None:
        wanted = next((l for l in LEVELS if l.casefold() == level.strip().casefold()), None)
        if wanted:
            self._set_pref("de_level", wanted)
        return wanted

    def start(self, level: str = "", situation: str = "") -> str:
        if level:
            self.set_level(level)
        self.active = True
        self.situation = situation.strip()
        self.session = {"saved": 0, "right": 0, "wrong": 0}
        return self.rules()

    def stop(self) -> dict:
        was, self.active, self.situation = self.active, False, ""
        return {"was_active": was, **self.session}

    reset = stop

    def rules(self) -> str:
        return rules(self.level, self.situation, self.due_words(3), self.word_of_day_given(), self._has_google())

    def prompt_paragraph(self) -> str:
        return prompt_paragraph(self.level, self.situation) if self.active else ""

    # ── слова ──

    def _stamp(self, when: datetime) -> str:
        return when.replace(tzinfo=None).isoformat(timespec="seconds")

    def word_of_day_given(self) -> bool:
        return self._pref("de_wotd", "") == f"{self.now():%Y-%m-%d}"

    def save_word(self, word: str, translation: str = "", word_of_day: bool = False) -> bool:
        """Новое слово (True) или обновить перевод у известного (False). Новое сразу «к повторению»."""
        word = _norm(word)
        if not word:
            return False
        now = self._stamp(self.now())
        key = word.casefold()
        row = self.db.execute("SELECT id, translation FROM de_words WHERE key = ?", (key,)).fetchone()
        if word_of_day:
            self._set_pref("de_wotd", f"{self.now():%Y-%m-%d}")
        if row:
            tr = _norm(translation) or row[1]
            self.db.execute("UPDATE de_words SET translation = ?, last_seen = ? WHERE id = ?", (tr, now, row[0]))
            self.db.commit()
            return False
        self.db.execute("INSERT INTO de_words (key, word, translation, last_seen, due) VALUES (?, ?, ?, ?, ?)",
                        (key, word, _norm(translation), now, now))
        self.db.commit()
        self.session["saved"] += 1
        return True

    def review(self, word: str, correct: bool) -> dict | None:
        """Итог проверки: верно — слово уходит в следующий ящик, ошибка — в нулевой и спросим скоро."""
        word = _norm(word)
        row = self.db.execute("SELECT id, box, misses, hits FROM de_words WHERE key = ?", (word.casefold(),)).fetchone()
        if row is None:
            if not word:
                return None
            self.save_word(word)
            row = self.db.execute("SELECT id, box, misses, hits FROM de_words WHERE key = ?", (word.casefold(),)).fetchone()
        wid, box, misses, hits = row
        now = self.now()
        if correct:
            box = min(box + 1, len(INTERVAL_DAYS) - 1)
            due = now + timedelta(days=INTERVAL_DAYS[box])
            hits += 1
            self.session["right"] += 1
        else:
            box = 0
            due = now + timedelta(minutes=RETRY_MIN)
            misses += 1
            self.session["wrong"] += 1
        self.db.execute("UPDATE de_words SET box = ?, misses = ?, hits = ?, last_seen = ?, due = ? WHERE id = ?",
                        (box, misses, hits, self._stamp(now), self._stamp(due), wid))
        self.db.commit()
        return {"box": box, "next": self._stamp(due), "days": INTERVAL_DAYS[box]}

    def due_words(self, limit: int = 3) -> list[dict]:
        rows = self.db.execute(
            "SELECT word, translation, misses, box, due FROM de_words WHERE due <= ? "
            "ORDER BY misses DESC, due LIMIT ?", (self._stamp(self.now()), max(1, int(limit)))).fetchall()
        return [{"word": w, "translation": t, "misses": m, "box": b, "due": d} for w, t, m, b, d in rows]

    def words(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT word, translation, misses, hits, box, due FROM de_words "
                               "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"word": w, "translation": t, "misses": m, "hits": h, "box": b, "due": d}
                for w, t, m, h, b, d in rows]

    def quiz_words(self, count: int = 5) -> list[dict]:
        """Слова для викторины: сначала пора повторить, потом самые путаные, потом любые."""
        out = self.due_words(count)
        if len(out) < count:
            seen = {w["word"] for w in out}
            rows = self.db.execute("SELECT word, translation, misses, box, due FROM de_words "
                                   "ORDER BY misses DESC, last_seen LIMIT ?", (count * 3,)).fetchall()
            for w, t, m, b, d in rows:
                if w not in seen and len(out) < count:
                    out.append({"word": w, "translation": t, "misses": m, "box": b, "due": d})
        return out

    # ── инструменты Gemini ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "set_language_mode":
            mode = str(args.get("mode", "")).strip().casefold()
            if mode in ("off", "russian", "ru", "стоп", "выкл"):
                out = {"ok": True, "mode": "russian", **self.stop(),
                       "note": "режим выключен; подведи итог одной-двумя фразами по-русски и дальше говори "
                               "только по-русски"}
                if self._has_google() and (out["right"] or out["saved"]):
                    out["task_hint"] = (f"Если у пользователя есть задача «{TASK_NAME}», предложи закрыть её "
                                        "(google_tasks_done, text «немецк») и сделай это только после согласия")
                return out
            if mode not in ("german", "de", "deutsch", "on", "немецкий", "вкл"):
                return {"ok": False, "error": "mode: german или off"}
            level = str(args.get("level", "")).strip()
            if level and self.set_level(level) is None:
                return {"ok": False, "error": f"уровень: {', '.join(LEVELS)}"}
            situation = str(args.get("situation", "")).strip()
            return {"ok": True, "mode": "german", "level": self.level, "rules": self.start("", situation)}
        if name == "de_word_save":
            word = str(args.get("word", ""))
            if not _norm(word):
                return {"ok": False, "error": "не сказано слово"}
            new = self.save_word(word, str(args.get("translation", "")), bool(args.get("word_of_day")))
            return {"ok": True, "new": new}
        if name == "de_word_review":
            res = self.review(str(args.get("word", "")), bool(args.get("correct")))
            if res is None:
                return {"ok": False, "error": "не сказано слово"}
            return {"ok": True, **res}
        if name == "de_words_due":
            return {"words": self.due_words(int(args.get("limit") or 3))}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── консоль ──

    HELP = ("de on [уровень] | off | level <A1..B2> | words | due | add <слово> = <перевод> | "
            "ok <слово> | miss <слово> | prompt — режим немецкого, словарь и повторение")

    async def command(self, args: str) -> str:
        sub, _, rest = args.strip().partition(" ")
        rest = rest.strip()
        match sub.lower():
            case "on":
                self.start(rest)
                return f"немецкий включён (уровень {self.level}); следующий разговор пойдёт в режиме. Правила:\n{self.rules()}"
            case "off":
                return f"немецкий выключен: {self.stop()}"
            case "level":
                return f"уровень {self.level}" if not rest else (
                    f"уровень {self.set_level(rest)}" if self.set_level(rest) else f"уровни: {', '.join(LEVELS)}")
            case "words":
                rows = self.words()
                return "\n".join(f"{w['word']} — {w['translation']}  (ошибок {w['misses']}, ящик {w['box']}, "
                                 f"снова {w['due']})" for w in rows) or "словарь пуст"
            case "due":
                rows = self.due_words(10)
                return "\n".join(f"{w['word']} — {w['translation']} (ошибок {w['misses']})" for w in rows) or "повторять нечего"
            case "add":
                word, _, tr = rest.partition("=")
                return "добавлено" if self.save_word(word, tr) else "уже было (перевод обновлён) или пусто"
            case "ok" | "miss":
                res = self.review(rest, sub.lower() == "ok")
                return str(res) if res else "нужно слово"
            case "prompt":
                return self.rules()
        return self.HELP
