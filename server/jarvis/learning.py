"""Обучение на пользователе и «живость» Маги.

Три части:
  1. Ночная рефлексия: Gemini читает историю дня (brain.db, messages) и достаёт факты, предпочтения
     по стилю, исправления, повторяющиеся просьбы, времена привычек и «хвосты» (события, о которых
     потом можно спросить). Уверенное (>= CONFIDENT) сразу попадает в подсказку, неуверенное
     (>= TENTATIVE) ждёт подтверждения в Telegram, остальное выбрасываем.
  2. Обратная связь: функция feedback(kind, note) правит правила мгновенно. Каждое изменение
     пишется в журнал (changelog) и откатывается: что_изменил / откати.
  3. Инициатива и настроение: редкие спонтанные реплики с лимитом частоты (тишина ночью, не при
     отсутствии пользователя) и «настроение» (энергия, любопытство) для подсказки.

Секреты (ключи, токены, пароли, номера карт) в «выученное» не попадают: фильтр looks_secret.
Всё хранится в brain.db, таблицы learned и changelog. Правила подмешиваются в подсказку
ограниченным блоком (prompt_section), «забудь это» работает и на них (forget).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

CONFIDENT = 0.7            # от этого уверенность: применяем сразу
TENTATIVE = 0.4            # от этого до CONFIDENT: спрашиваем в Telegram
MAX_ACTIVE_SHOWN = 8       # правил в подсказке
SECTION_CHARS = 700        # и не больше столько знаков
MAX_ITEM_CHARS = 140
MAX_PER_REFLECTION = 12
REFLECT_AT = "04:00"
QUIET_AFTER_S = 3 * 3600   # «после долгой тишины»: не было реплик столько и ночь уже прошла
TICK_S = 60.0
INIT_PER_DAY = 3           # спонтанных реплик в сутки не больше
INIT_GAP_S = 3 * 3600      # и не чаще, чем раз в 3 часа
INIT_CHANCE = 0.25         # на каждый тик в подходящее окно (раз в минуту): ~ редко
KINDS = ("fact", "style", "correction", "habit", "request", "followup")
FEEDBACK_KINDS = {
    "wrong": ("correction", "Пользователь поправил: «{note}». Учитывай это."),
    "too_sarcastic": ("style", "Меньше язвительности и подколов{note}."),
    "too_long": ("style", "Отвечай короче{note}."),
    "too_short": ("style", "Отвечай подробнее{note}."),
    "good": ("style", "Пользователю понравилось, продолжай так: {note}."),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS learned (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, confidence REAL NOT NULL,
    source TEXT NOT NULL, ts TEXT NOT NULL, applied INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active', due TEXT);
CREATE TABLE IF NOT EXISTS changelog (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, action TEXT NOT NULL, learned_id INTEGER,
    text TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "feedback", "description": "Пользователь оценил твоё поведение. kind: wrong («не так», "
     "поправка; в note что именно), too_sarcastic («слишком язвительно»), too_long («слишком длинно»), "
     "too_short («слишком коротко»), good («молодец, так и делай»; в note что именно понравилось). "
     "Правило начинает действовать сразу. Потом коротко подтверди словами.",
     "parameters": {"type": "OBJECT", "properties": {
         "kind": {"type": "STRING"}, "note": {"type": "STRING"}}, "required": ["kind"]}},
    {"name": "what_changed", "description": "Что ты изменил в себе: журнал последних правок поведения "
     "(«что ты изменил в себе?», «чему ты научился?»).",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "undo_change", "description": "Откатить правку поведения: последнюю или, если указан "
     "text, ту, в тексте которой есть подстрока («верни как было», «отмени это правило»).",
     "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING"}}}},
    {"name": "confirm_learning", "description": "Пользователь ответил на вопрос «Запомнить?»: "
     "accept true — да, false — нет. id не нужен, если вопрос был один.",
     "parameters": {"type": "OBJECT", "properties": {
         "accept": {"type": "BOOLEAN"}, "id": {"type": "NUMBER"}}, "required": ["accept"]}},
]

REFLECT_PROMPT = """\
Ты анализируешь переписку голосового ассистента Маги с пользователем за последние сутки, чтобы
Мага лучше подстраивался. Реплики: «user» — пользователь, «assistant» — Мага.
Выдели только то, что явно следует из текста, ничего не выдумывай. Типы:
- fact: новый факт о пользователе (люди, учёба, вкусы, планы);
- style: предпочтение по стилю («короче», «не шути утром», «без подколов»);
- correction: пользователь поправил Магу («нет, не так»), что именно надо делать иначе;
- habit: время или повторение привычки («ложится около часа ночи», «по утрам просит погоду»);
- request: повторяющаяся просьба;
- followup: событие, о котором уместно спросить позже («завтра контрольная»); due — когда
  спросить, формат YYYY-MM-DDTHH:MM, если понятно, иначе пусто.
Никаких паролей, ключей, номеров карт, адресов и прочих секретов. Текст каждого пункта — одна
короткая фраза по-русски, в третьем лице или как правило для Маги. confidence от 0 до 1.
Ответь ТОЛЬКО JSON-массивом вида
[{"kind":"style","text":"...","confidence":0.8,"due":""}]. Нет ничего достойного — [].

Переписка:
"""

_SECRET = re.compile(
    r"(password|passwd|пароль|парол[июя]|token|токен|api[_ -]?key|секрет|secret|pin\b|пин-?код|"
    r"cvv|cvc|\biban\b|\b(?:\d[ -]?){13,19}\b|AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{16,}|"
    r"\b[0-9A-Fa-f]{32,}\b|[A-Za-z0-9_-]{30,})", re.I)


def looks_secret(text: str) -> bool:
    return bool(_SECRET.search(text))


def parse_items(raw: str) -> list[dict[str, Any]]:
    """Достать JSON-массив из ответа модели (бывает в ```-блоке); мусор → пустой список."""
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict):
            continue
        try:
            conf = float(it.get("confidence", 0))
        except (TypeError, ValueError):
            continue
        kind, text = str(it.get("kind", "")).strip(), " ".join(str(it.get("text", "")).split())
        if kind in KINDS and text:
            out.append({"kind": kind, "text": text[:MAX_ITEM_CHARS], "confidence": max(0.0, min(conf, 1.0)),
                        "due": str(it.get("due") or "").strip()})
    return out


def _valid_time(s: str) -> bool:
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", s)) and int(s.split(":")[0]) < 24 and int(s.split(":")[1]) < 60


class Learning:
    def __init__(self, brain, *, ask: Callable[[str], Awaitable[str]] | None = None,
                 notify: Callable[[str], Awaitable[bool]] | None = None,
                 announce: Callable[[str], Awaitable[bool]] | None = None,
                 presence=None, night: Callable[[], bool] | None = None,
                 now: Callable[[], datetime] | None = None, rng: random.Random | None = None,
                 reflect_at: str = REFLECT_AT):
        """ask(prompt) → текст ответа Gemini; notify(text) → отправить в Telegram;
        announce(prompt) → колонка заговорит сама (Voice.announce); night() → сейчас ночь."""
        self.brain = brain
        self.db = brain.db
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.ask, self.notify, self.announce = ask, notify, announce
        self.presence, self.night = presence, night
        self.now = now or brain.now
        self.rng = rng or random.Random()
        self.reflect_at = reflect_at if _valid_time(reflect_at) else REFLECT_AT
        self._tasks: list[asyncio.Task] = []
        self.stats = {"added": 0, "accepted": 0, "rejected": 0}

    def _ts(self) -> str:
        return self.now().isoformat(timespec="seconds")

    # ── хранение ──

    def _log(self, action: str, text: str, learned_id: int | None = None) -> None:
        self.db.execute("INSERT INTO changelog (ts, action, learned_id, text) VALUES (?,?,?,?)",
                        (self._ts(), action, learned_id, text))

    def _exists(self, text: str) -> bool:
        low = text.casefold()
        if any(low == r[0].casefold() for r in self.db.execute(
                "SELECT text FROM learned WHERE status IN ('active','pending')")):
            return True
        return low in (f.casefold() for f in self.brain.facts())

    def add(self, kind: str, text: str, confidence: float, source: str, due: str = "") -> int | None:
        """Положить пункт. Возвращает id или None (секрет, дубль, мало уверенности)."""
        text = " ".join(text.split())[:MAX_ITEM_CHARS]
        if not text or looks_secret(text) or confidence < TENTATIVE or self._exists(text):
            return None
        status = "active" if confidence >= CONFIDENT else "pending"
        cur = self.db.execute(
            "INSERT INTO learned (kind, text, confidence, source, ts, applied, status, due) VALUES (?,?,?,?,?,?,?,?)",
            (kind, text, confidence, source, self._ts(), 1 if status == "active" else 0, status, due or None))
        lid = cur.lastrowid or 0
        if status == "active":
            self._log("выучил", text, lid)
        self.db.commit()
        self.stats["added"] += 1
        return lid

    def items(self, status: str = "active", kind: str | None = None) -> list[tuple[int, str, str, float]]:
        q, args = "SELECT id, kind, text, confidence FROM learned WHERE status = ?", [status]
        if kind:
            q += " AND kind = ?"
            args.append(kind)
        return list(self.db.execute(q + " ORDER BY id", args))

    # ── подсказка ──

    def prompt_section(self) -> str:
        """Блок «что я о тебе понял» для подсказки: ограничен по числу и по длине, без «хвостов»."""
        rows = [r for r in self.items() if r[1] != "followup"][-MAX_ACTIVE_SHOWN:]
        lines, used = [], 0
        for _, _, text, _ in reversed(rows):         # свежие важнее: при нехватке места режем старые
            if used + len(text) + 3 > SECTION_CHARS:
                break
            lines.insert(0, f"- {text}")
            used += len(text) + 3
        head = ("\nЧто ты о нём понял (выучено из разговоров; следуй этому, но просьба в моменте главнее):\n"
                + "\n".join(lines)) if lines else ""
        return head + self.persona(self.now())

    # ── настроение и «человеческие мелочи» ──

    def mood(self, now: datetime | None = None) -> tuple[float, float]:
        """(энергия, любопытство) 0..1: от времени суток, числа сегодняшних реплик и тишины."""
        now = now or self.now()
        h = now.hour + now.minute / 60
        energy = 0.25 if h < 7 or h >= 23 else 0.55 if h < 10 else 0.8 if h < 18 else 0.6
        today = self.db.execute("SELECT COUNT(*) FROM messages WHERE ts >= ?",
                                (now.strftime("%Y-%m-%d"),)).fetchone()[0]
        energy = min(1.0, energy + min(today, 20) * 0.01)
        pending_followups = self.db.execute(
            "SELECT COUNT(*) FROM learned WHERE kind='followup' AND status='active'").fetchone()[0]
        curiosity = min(1.0, 0.3 + 0.1 * pending_followups + (0.2 if today < 3 else 0.0))
        return round(energy, 2), round(curiosity, 2)

    def persona(self, now: datetime) -> str:
        energy, curiosity = self.mood(now)
        feel = ("сонный и неторопливый" if energy < 0.4 else "бодрый" if energy > 0.7 else "в ровном настроении")
        tail = ", тебе любопытно, как у него дела" if curiosity > 0.6 else ""
        return (f"\nСейчас ты {feel}{tail}. У тебя есть свои мнения и вкусы (можешь сказать, что тебе "
                "нравится или нет, и не соглашаться из вежливости), и ты помнишь прошлые темы: "
                "можно к ним вернуться, если к месту. Не навязывай это.")

    # ── обратная связь и откат ──

    def feedback(self, kind: str, note: str = "") -> dict[str, Any]:
        kind = kind.strip().casefold()
        if kind not in FEEDBACK_KINDS:
            return {"ok": False, "error": f"kind: {', '.join(FEEDBACK_KINDS)}"}
        note = " ".join(note.split())[:MAX_ITEM_CHARS]
        if looks_secret(note):
            return {"ok": False, "error": "секреты в правила не кладу"}
        lk, tmpl = FEEDBACK_KINDS[kind]
        if kind in ("wrong", "good") and not note:
            return {"ok": False, "error": "скажи, что именно (note)"}
        text = tmpl.format(note=f" ({note})" if note and kind not in ("wrong", "good") else note)
        if kind in ("too_long", "too_short", "too_sarcastic"):     # противоположная правка снимает прежнюю
            opposite = {"too_long": "подробнее", "too_short": "короче", "too_sarcastic": ""}[kind]
            if opposite:
                for lid, _, t, _ in self.items(kind="style"):
                    if opposite in t:
                        self._retire(lid, "заменено")
        lid = self.add(lk, text, 1.0, "feedback:" + kind)
        if lid is None:
            return {"ok": True, "new": False, "note": "такое правило уже есть"}
        self.stats["accepted"] += 1
        return {"ok": True, "new": True, "rule": text}

    def _retire(self, lid: int, why: str) -> None:
        self.db.execute("UPDATE learned SET status='reverted', applied=0 WHERE id=?", (lid,))
        text = self.db.execute("SELECT text FROM learned WHERE id=?", (lid,)).fetchone()
        self._log(why, text[0] if text else "", lid)
        self.db.commit()

    def changes(self, limit: int = 10) -> list[tuple[int, str, str, str, bool]]:
        rows = self.db.execute("SELECT id, ts, action, text, undone FROM changelog ORDER BY id DESC LIMIT ?",
                               (limit,))
        return [(i, ts, a, t, bool(u)) for i, ts, a, t, u in rows]

    def describe_changes(self, limit: int = 8) -> str:
        rows = self.changes(limit)
        if not rows:
            return "Пока ничего не менял в себе."
        return "\n".join(f"{ts[5:10].replace('-', '.')} {a}: {t}" + (" (откачено)" if u else "")
                         for _, ts, a, t, u in rows)

    def undo(self, needle: str = "") -> dict[str, Any]:
        """Откат одной правки: последней действующей или с подстрокой в тексте."""
        rows = [r for r in self.changes(200) if not r[4] and r[2] in ("выучил", "принял")]
        pick = next((r for r in rows if not needle or needle.casefold() in r[3].casefold()), None)
        if pick is None:
            return {"ok": False, "error": "нечего откатывать"}
        cid, _, _, text, _ = pick
        self.db.execute("UPDATE changelog SET undone=1 WHERE id=?", (cid,))
        self.db.execute("UPDATE learned SET status='reverted', applied=0 WHERE status='active' AND text=?", (text,))
        self._log("откатил", text)
        self.db.commit()
        self.stats["rejected"] += 1
        return {"ok": True, "reverted": text}

    def forget(self, needle: str) -> int:
        """Для «забудь это»: убрать выученное с подстрокой."""
        needle = needle.strip().casefold()
        n = 0
        for lid, _, text, _ in self.items() + self.items("pending"):
            if needle and needle in text.casefold():
                self._retire(lid, "забыл")
                n += 1
        return n

    # ── подтверждения ──

    def confirm(self, accept: bool, lid: int | None = None) -> dict[str, Any]:
        pend = self.items("pending")
        if lid is not None:
            pend = [p for p in pend if p[0] == int(lid)]
        if not pend:
            return {"ok": False, "error": "нечего подтверждать"}
        pid, _, text, _ = pend[-1] if lid is None else pend[0]
        if accept:
            self.db.execute("UPDATE learned SET status='active', applied=1 WHERE id=?", (pid,))
            self._log("принял", text, pid)
            self.stats["accepted"] += 1
        else:
            self.db.execute("UPDATE learned SET status='rejected' WHERE id=?", (pid,))
            self._log("отклонил", text, pid)
            self.stats["rejected"] += 1
        self.db.commit()
        return {"ok": True, "accepted": accept, "text": text}

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "feedback":
            return self.feedback(str(args.get("kind", "")), str(args.get("note", "")))
        if name == "what_changed":
            return {"ok": True, "changes": self.describe_changes()}
        if name == "undo_change":
            return self.undo(str(args.get("text", "")))
        if name == "confirm_learning":
            lid = args.get("id")
            return self.confirm(bool(args.get("accept")), int(lid) if lid is not None else None)
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── рефлексия ──

    def day_transcript(self, hours: float = 24, max_chars: int = 12000) -> str:
        since = (self.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        rows = self.db.execute("SELECT ts, source, role, text FROM messages WHERE ts >= ? ORDER BY id",
                               (since,)).fetchall()
        lines = [f"{ts[5:16]} {src} {role}: {' '.join(text.split())[:300]}" for ts, src, role, text in rows]
        out = "\n".join(lines)
        return out[-max_chars:]          # если много, берём конец дня

    async def reflect(self) -> list[int]:
        """Один прогон: история дня → новые пункты. Возвращает id добавленных."""
        if self.ask is None:
            return []
        text = self.day_transcript()
        if not text.strip():
            return []
        raw = await self.ask(REFLECT_PROMPT + text)
        added, asked = [], []
        for it in parse_items(raw)[:MAX_PER_REFLECTION]:
            lid = self.add(it["kind"], it["text"], it["confidence"], "reflection", it["due"])
            if lid is None:
                continue
            added.append(lid)
            if it["confidence"] < CONFIDENT:
                asked.append(it["text"])
        self.brain.set_pref("learning_last_reflect", self.now().strftime("%Y-%m-%d"))
        for t in asked:
            await self.ask_user(t)
        log.info("обучение: рефлексия, новых пунктов %d (на подтверждение %d)", len(added), len(asked))
        return added

    async def ask_user(self, text: str) -> bool:
        if self.notify is None:
            return False
        return await self.notify(f"Заметил: {text}. Запомнить? Ответь «да» или «нет» "
                                 "(или скажи Маге).")

    def reflect_due(self) -> bool:
        now = self.now()
        if self.brain.pref("learning_last_reflect") == now.strftime("%Y-%m-%d"):
            return False
        h, m = map(int, self.reflect_at.split(":"))
        if now.hour * 60 + now.minute >= h * 60 + m:
            return True
        # после долгой тишины: вчера что-то было, сейчас никто не говорил QUIET_AFTER_S — можно разобрать
        last = self.db.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
        return bool(last and (now - datetime.fromisoformat(last)).total_seconds() > QUIET_AFTER_S)

    # ── инициатива ──

    def _initiative_state(self) -> tuple[str, int, float]:
        day = self.brain.pref("init_day")
        n = int(self.brain.pref("init_count", "0") or 0)
        last = float(self.brain.pref("init_last", "0") or 0)
        if day != self.now().strftime("%Y-%m-%d"):
            n = 0
        return day, n, last

    def may_speak(self) -> tuple[bool, str]:
        """Можно ли сейчас заговорить первым: ночь, присутствие, лимиты."""
        if self.brain.pref("initiative", "on") == "off":
            return False, "инициатива выключена"
        if self.night is not None and self.night():
            return False, "ночь"
        if self.presence is not None and not self.presence.here():
            return False, "пользователя нет на месте"
        _, n, last = self._initiative_state()
        if n >= INIT_PER_DAY:
            return False, "лимит на сегодня"
        if self.now().timestamp() - last < INIT_GAP_S:
            return False, "слишком скоро после прошлой"
        return True, ""

    def pick_topic(self) -> tuple[int | None, str]:
        """Что сказать: созревший «хвост» (событие из памяти) либо лёгкое замечание."""
        now = self.now().isoformat(timespec="minutes")
        row = self.db.execute(
            "SELECT id, text FROM learned WHERE kind='followup' AND status='active' AND "
            "(due IS NULL OR due <= ?) ORDER BY id LIMIT 1", (now,)).fetchone()
        if row:
            return row[0], ("Ты помнишь: " + row[1] + ". Сам заговори и коротко, по-человечески спроси об этом "
                            "(«как прошло…?», «получилось…?»). Одна-две фразы, без допроса.")
        return None, ("Скажи одну короткую уместную мелочь по своей инициативе: замечание о погоде (home_weather, "
                      "если есть), либо вернись к теме из недавнего разговора, либо поделись своим мнением. "
                      "Одна фраза, без вопросов-допросов. Ничего не выдумывай.")

    async def maybe_speak(self, force: bool = False) -> bool:
        """Один шаг инициативы. True — колонка заговорила."""
        if self.announce is None:
            return False
        ok, why = self.may_speak()
        if not ok:
            return False
        energy, curiosity = self.mood()
        if not force and self.rng.random() > INIT_CHANCE * (0.5 + energy):
            return False
        lid, prompt = self.pick_topic()
        if lid is None and not force and self.rng.random() > curiosity:
            return False         # просто так — ещё реже, чем по поводу
        if not await self.announce(prompt):
            return False
        _, n, _ = self._initiative_state()
        self.brain.set_pref("init_day", self.now().strftime("%Y-%m-%d"))
        self.brain.set_pref("init_count", str(n + 1))
        self.brain.set_pref("init_last", str(self.now().timestamp()))
        if lid is not None:
            self.db.execute("UPDATE learned SET status='done' WHERE id=?", (lid,))
            self.db.commit()
        log.info("обучение: спонтанная реплика (%s)", "хвост" if lid else "замечание")
        return True

    # ── сводка ──

    def digest(self, days: int = 7) -> str:
        """Для еженедельной сводки: что выучил, сколько правок принято и отклонено."""
        since = (self.now() - timedelta(days=days)).isoformat(timespec="seconds")
        learned = [r[0] for r in self.db.execute(
            "SELECT text FROM learned WHERE ts >= ? AND status='active' AND kind != 'followup'", (since,))]
        acts = dict(self.db.execute(
            "SELECT action, COUNT(*) FROM changelog WHERE ts >= ? GROUP BY action", (since,)).fetchall())
        accepted = acts.get("принял", 0) + acts.get("выучил", 0)
        rejected = acts.get("отклонил", 0) + acts.get("откатил", 0)
        head = f"За {days} дн.: принято правок {accepted}, отклонено или откачено {rejected}."
        if not learned:
            return head + " Нового не выучил."
        return head + " Выучил: " + "; ".join(learned[:5]) + ("…" if len(learned) > 5 else "")

    # ── жизненный цикл ──

    async def _loop(self) -> None:
        while True:
            try:
                if self.reflect_due():
                    await self.reflect()
                await self.maybe_speak()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("обучение: шаг не вышел: %r", e)
            await asyncio.sleep(TICK_S)

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def command(self, line: str) -> str | None:
        """Консоль/Telegram: learned | learned reflect | learned undo [текст] | learned digest."""
        parts = line.split(maxsplit=2)
        if not parts or parts[0].lower() not in ("learned", "learn"):
            return None
        sub = parts[1].lower() if len(parts) > 1 else ""
        if sub == "reflect":
            added = await self.reflect()
            return f"рефлексия: новых пунктов {len(added)}"
        if sub == "undo":
            r = self.undo(parts[2] if len(parts) > 2 else "")
            return ("откатил: " + r["reverted"]) if r["ok"] else r["error"]
        if sub == "digest":
            return self.digest()
        if sub in ("yes", "no"):
            r = self.confirm(sub == "yes")
            return r.get("text", r.get("error", ""))
        if sub == "speak":
            return "заговорил" if await self.maybe_speak(force=True) else "не вышло (лимит, ночь, нет на месте)"
        act = self.items()
        pend = self.items("pending")
        out = [f"[{i}] {k}: {t} ({c:.1f})" for i, k, t, c in act] or ["выученного пока нет"]
        out += [f"ждёт подтверждения [{i}]: {t}" for i, _, t, _ in pend]
        return "\n".join(out + ["— журнал —", self.describe_changes()])
