"""«Догадки»: Мага замечает повторяющиеся действия и предлагает их автоматизировать.

1. Журнал действий (таблица events в brain.db, хранится 60 дней): включение/выключение света и ламп
   (подписка на state_changed в HA на отдельном соединении, без опроса), «спокойной ночи», запуск музыки,
   установка будильника, начало и конец работы за компьютером. Автоматизации HA (у события есть
   context.parent_id) и собственные действия Маги (его вызовы HA, руки лампы WLED, сценарии света)
   в журнал не попадают: только то, что сделал человек (голосом, руками, из приложения).
2. Раз в сутки ночью: «в одно и то же время (±15 минут) одно и то же действие» — не меньше 5 дней из 7
   или 4 из последних 5 будней. Кандидаты лежат в learned (kind=hunch, status=candidate), их структура —
   в таблице hunches. Предлагаются только действия со светом: остальное (компьютер, будильник, музыка)
   остаётся «наблюдением» (status=insight), его HA автоматизировать не умеет.
3. Предложение: голосом (Voice.announce) или в Telegram. Не ночью, не при «не слушай», не когда
   пользователя нет в комнате, не больше одного в сутки и пока нет неотвеченного; после трёх
   активных автоматизаций от Маги сам больше не предлагает (дальше только по команде `hunch run N`).
   «Да» создаёт автоматизацию через REST HA (POST /api/config/automation/config/<id>, WebSocket-команды
   создания автоматизаций в HA нет) с именем «Мага: …». «Нет» запоминается навсегда.
4. Откат: hunch_undo / `hunch undo N` удаляют автоматизацию из HA. Трогаем только те, что создали сами
   (id из таблицы hunches, префикс maga_, имя с «Мага:»); чужие автоматизации не меняются и не удаляются.
   Действия пишутся в changelog обучения (видно в «что ты изменил в себе?»).
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from .ha import HAError

log = logging.getLogger(__name__)

KEEP_DAYS = 60
WINDOW_MIN = 15                 # ±15 минут
DAILY_MIN, DAILY_DAYS = 5, 7    # 5 дней из последних 7
WEEK_MIN, WEEK_DAYS = 4, 5      # 4 будня из последних 5
DAY_STARTS = 4                  # час, с которого начинаются «сутки» привычек
MERGE_MIN = 10                  # лампы одного момента сливаются в одну догадку
DEDUP_S = 60                    # одно и то же действие дважды за минуту — одно событие
CTX_DELAY_S = 3.0               # ждём ответ call_service, чтобы узнать «это наши руки»
OWN_KEEP = 500
SUPPRESS_S = 20.0               # после прямой команды Маги лампе WLED
GOODNIGHT_SUPPRESS_S = 120.0
PC_STABLE_S = 300               # состояние компьютера должно продержаться, чтобы стать событием
MAX_AUTO = 3                    # активных автоматизаций от Маги, дальше — только по команде
ANSWER_TTL_S = 36 * 3600        # неотвеченное предложение протухает
MAX_SNOOZES = 2                 # после стольких молчаний догадка считается отклонённой
PROPOSE_FROM, PROPOSE_TO = 11, 21
DETECT_AT = (3, 40)
TICK_S = 60.0
PREFIX = "Мага:"
ID_PREFIX = "maga_hunch_"
ACTIONABLE = ("light",)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
    action TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS hunches (
    id INTEGER PRIMARY KEY, spec TEXT NOT NULL, proposed_ts TEXT, snoozes INTEGER NOT NULL DEFAULT 0,
    auto_id TEXT, alias TEXT);
"""

TOOLS: list[dict[str, Any]] = [
    {"name": "hunch_list", "description": "Догадки Маги о привычках пользователя и автоматизации, которые он "
     "сам создал по ним («что ты заметил?», «какие автоматизации ты сделал?»).",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "hunch_undo", "description": "Откатить автоматизацию, которую Мага создал по догадке («отмени то, "
     "что ты сделал», «убери эту автоматизацию»): без id — последнюю. Удаляет её из Home Assistant.",
     "parameters": {"type": "OBJECT", "properties": {"id": {"type": "NUMBER"}}}},
    {"name": "hunch_answer", "description": "Пользователь ответил на предложение автоматизировать привычку: "
     "accept true — «да, сделай», false — «нет, не надо» (больше не предлагать). id нужен, только если "
     "речь не о последнем предложении.",
     "parameters": {"type": "OBJECT", "properties": {
         "accept": {"type": "BOOLEAN"}, "id": {"type": "NUMBER"}}, "required": ["accept"]}},
]

_USER: contextvars.ContextVar[bool] = contextvars.ContextVar("hunch_user", default=False)
WEEKDAYS_EN = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# ── поиск закономерностей (чистые функции) ──

def _off(d: int) -> int:
    """Разность минут суток в диапазоне [-720, 720): 23:50 и 00:05 рядом."""
    return (d + 720) % 1440 - 720


def best_cluster(by_day: dict[date, list[int]], window: int = WINDOW_MIN) -> tuple[int, int, set[date]] | None:
    """Самое людное «время суток»: (в скольких днях, центр в минутах, какие дни)."""
    best: tuple[int, int, set[date]] | None = None
    for ts in by_day.values():
        for t in ts:
            hits = {d: [x for x in xs if abs(_off(x - t)) <= window] for d, xs in by_day.items()}
            hits = {d: v for d, v in hits.items() if v}
            if best is None or len(hits) > best[0]:
                offs = sorted(_off(x - t) for v in hits.values() for x in v)
                best = (len(hits), (t + offs[len(offs) // 2]) % 1440, set(hits))
    return best


def find_patterns(events: list[tuple[str, str, str, datetime]], today: date) -> list[dict[str, Any]]:
    """events: (kind, target, action, время). Закономерности по каждой тройке; сегодня не считается.
    Сутки считаются с 4 утра: «в 00:10» относится к вечеру накануне."""
    groups: dict[tuple[str, str, str], dict[date, list[int]]] = {}
    for kind, target, action, ts in events:
        groups.setdefault((kind, target, action), {}).setdefault((ts - timedelta(hours=DAY_STARTS)).date(), []).append(ts.hour * 60 + ts.minute)
    days7 = [today - timedelta(days=i) for i in range(1, DAILY_DAYS + 1)]
    wk5: list[date] = []
    d = today
    while len(wk5) < WEEK_DAYS:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            wk5.append(d)
    out = []
    for (kind, target, action), grouped in groups.items():
        full = best_cluster({d: grouped[d] for d in days7 if d in grouped})
        wk = best_cluster({d: grouped[d] for d in wk5 if d in grouped})
        found = None
        if full and full[0] >= DAILY_MIN and any(d.weekday() >= 5 for d in full[2]):
            found = ("daily", full[0], DAILY_DAYS, full[1])
        elif wk and wk[0] >= WEEK_MIN:
            found = ("weekdays", wk[0], WEEK_DAYS, wk[1])
        elif full and full[0] >= DAILY_MIN:
            found = ("daily", full[0], DAILY_DAYS, full[1])
        if found:
            mode, n, total, center = found
            out.append({"kind": kind, "target": target, "action": action, "mode": mode, "n": n, "total": total,
                        "minute": center, "conf": round(min(0.99, n / total), 2)})
    return out


def merge_lights(patterns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Лампы, которые гасят/включают в один момент, — одна догадка со списком ламп."""
    merged: list[dict[str, Any]] = []
    for p in sorted(patterns, key=lambda p: (p["kind"], p["action"], p["mode"], p["minute"])):
        p = {**p, "entities": [p["target"]]}
        prev = merged[-1] if merged else None
        if (prev and p["kind"] in ACTIONABLE and prev["kind"] == p["kind"] and prev["action"] == p["action"]
                and prev["mode"] == p["mode"] and abs(_off(prev["minute"] - p["minute"])) <= MERGE_MIN):
            prev["entities"] += p["entities"]
            prev["conf"] = round((prev["conf"] + p["conf"]) / 2, 2)
            continue
        merged.append(p)
    return merged


def hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def part_of_day(minute: int) -> str:
    h = minute // 60
    return "каждую ночь" if h < 5 or h >= 23 else "каждое утро" if h < 12 else "каждый день" if h < 18 else "каждый вечер"


def describe(spec: dict[str, Any]) -> str:
    """«Ты каждый вечер гасишь свет в 23:30» — по спецификации догадки."""
    t = hhmm(spec["minute"])
    when = part_of_day(spec["minute"]) if spec["mode"] == "daily" else "по будням"
    kind, action = spec["kind"], spec["action"]
    if kind == "light":
        names = spec.get("names") or []
        what = ("гасишь" if action == "off" else "включаешь") + " свет"
        if 0 < len(names) <= 2:
            what += " (" + ", ".join(names) + ")"
        return f"Ты {when} {what} в {t}"
    what = {"pc": {"start": "садишься за компьютер", "stop": "заканчиваешь работу за компьютером"},
            "alarm": {"set": "заводишь будильник"}, "goodnight": {"say": "говоришь «спокойной ночи»"},
            "music": {"start": "запускаешь музыку"}}.get(kind, {}).get(action, f"делаешь {kind} {action}")
    return f"Ты {when} {what} около {t}"


def rest_base(ws_url: str) -> str:
    """Адрес REST API по адресу WebSocket: ws://supervisor/core/websocket → http://supervisor/core/api."""
    url = ws_url.rstrip("/").replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    url = url.removesuffix("/websocket").removesuffix("/api")
    return url + "/api"


def automation_config(spec: dict[str, Any], hid: int) -> dict[str, Any]:
    """Конфигурация автоматизации HA. Имя всегда с «Мага:»."""
    verb = "turn_off" if spec["action"] == "off" else "turn_on"
    cfg: dict[str, Any] = {
        "alias": f"{PREFIX} {'гасить' if spec['action'] == 'off' else 'включать'} свет в {hhmm(spec['minute'])}"
                 + (" по будням" if spec["mode"] == "weekdays" else ""),
        "description": f"Создано Магой по привычке (догадка {hid}). Удалить: «Мага, отмени автоматизацию» "
                       "или как обычную автоматизацию.",
        "mode": "single",
        "trigger": [{"platform": "time", "at": hhmm(spec["minute"]) + ":00"}],
        "condition": ([{"condition": "time", "weekday": list(WEEKDAYS_EN[:5])}] if spec["mode"] == "weekdays" else []),
        "action": [{"service": f"light.{verb}", "target": {"entity_id": list(spec["entities"])}}],
    }
    return cfg


async def default_http(method: str, url: str, token: str, body: Any = None) -> tuple[int, Any]:
    def run() -> tuple[int, Any]:
        req = urllib.request.Request(
            url, method=method, data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (OSError, ValueError) as e:
            raise HAError(f"нет связи с Home Assistant: {e!r}") from None
    return await asyncio.to_thread(run)


class Hunches:
    def __init__(self, brain, learning, ha, *, announce: Callable[[str], Awaitable[bool]] | None = None,
                 notify: Callable[[str], Awaitable[bool]] | None = None, presence=None,
                 night: Callable[[], bool] | None = None, muted: Callable[[], bool] | None = None,
                 bridge=None, now: Callable[[], datetime] | None = None,
                 http: Callable[..., Awaitable[tuple[int, Any]]] = default_http):
        self.brain, self.learning, self.ha, self.db = brain, learning, ha, brain.db
        self.announce, self.notify, self.presence, self.night = announce, notify, presence, night
        self.muted, self.bridge, self.http = muted, bridge, http
        self.now = now or brain.now
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.tool_names = frozenset(t["name"] for t in TOOLS)
        self.TOOLS = TOOLS
        self._own: list[str] = []          # context.id наших собственных вызовов HA
        self._voice: list[str] = []        # context.id команд пользователя через Магу
        self._suppress_until = 0.0
        self._pc: tuple[bool, float] | None = None       # (подтверждённое состояние, с какого момента)
        self._pc_seen: tuple[bool, float] | None = None
        self._alarm: int | None = None
        self._music = False
        self._tasks: list[asyncio.Task] = []

    def _clock(self) -> float:
        return self.now().timestamp()

    def _ts(self) -> str:
        return self.now().replace(tzinfo=None).isoformat(timespec="seconds")

    # ── журнал событий ──

    def record(self, kind: str, target: str, action: str, source: str = "user", ts: datetime | None = None) -> bool:
        """Записать событие. False — дубль за последнюю минуту или Мага сам трогал лампу."""
        if self._clock() < self._suppress_until:
            return False
        when = (ts or self.now()).replace(tzinfo=None)
        lo = (when - timedelta(seconds=DEDUP_S)).isoformat(timespec="seconds")
        hi = (when + timedelta(seconds=DEDUP_S)).isoformat(timespec="seconds")
        if self.db.execute("SELECT 1 FROM events WHERE kind=? AND target=? AND action=? AND ts BETWEEN ? AND ?",
                           (kind, target, action, lo, hi)).fetchone():
            return False
        self.db.execute("INSERT INTO events (ts, kind, target, action, source) VALUES (?,?,?,?,?)",
                        (when.isoformat(timespec="seconds"), kind, target, action, source))
        self.db.commit()
        return True

    def prune(self) -> None:
        cut = (self.now() - timedelta(days=KEEP_DAYS)).replace(tzinfo=None).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM events WHERE ts < ?", (cut,))
        self.db.commit()

    def process_state(self, event: dict[str, Any], when: datetime | None = None) -> bool:
        """Событие state_changed из HA → журнал, если это действие человека над светом."""
        data = event.get("data") or {}
        eid = str(data.get("entity_id", ""))
        new, old = data.get("new_state") or {}, data.get("old_state") or {}
        if eid.partition(".")[0] != "light" or new.get("state") not in ("on", "off") \
                or old.get("state") not in ("on", "off") or new["state"] == old["state"]:
            return False
        if (new.get("attributes") or {}).get("entity_id"):
            return False                      # группа ламп: её члены придут сами
        ctx = event.get("context") or new.get("context") or {}
        if ctx.get("parent_id"):
            return False                      # запустила автоматизация или сценарий
        cid = ctx.get("id")
        if cid in self._own:
            return False                      # Мага сам (будильник, «спокойной ночи», подсветка)
        source = "voice" if cid in self._voice else "app" if ctx.get("user_id") else "manual"
        return self.record("light", eid, new["state"], source, when)

    # ── подмена вызовов HA и света: узнаём «это не пользователь» ──

    def attach_ha(self) -> None:
        orig_request, orig_call = self.ha.request, self.ha.call

        async def request(type_: str, **fields: Any) -> Any:
            res = await orig_request(type_, **fields)
            if type_ == "call_service":
                cid = ((res or {}).get("context") or {}).get("id")
                if cid:
                    box = self._voice if _USER.get() else self._own
                    box.append(cid)
                    del box[:-OWN_KEEP]
            return res

        async def call(name: str, args: dict[str, Any], send: Any = None) -> dict:
            token = _USER.set(True)
            try:
                return await orig_call(name, args, send)
            finally:
                _USER.reset(token)

        self.ha.request, self.ha.call = request, call

    def attach_lights(self, lights) -> None:
        orig_goodnight, orig_media = lights.goodnight, lights.on_media

        async def goodnight() -> dict:
            self.record("goodnight", "", "say", "voice")
            self._suppress_until = self._clock() + GOODNIGHT_SUPPRESS_S
            return await orig_goodnight()

        async def on_media(msg: dict) -> None:
            playing = msg.get("state") == "playing"
            if playing and not self._music:
                self.record("music", "", "start", "phone")
            self._music = playing
            await orig_media(msg)

        lights.goodnight, lights.on_media = goodnight, on_media
        state = getattr(lights, "state", None)
        if state is not None and hasattr(state, "wled_post"):
            orig_wled = state.wled_post

            async def wled_post(*a: Any, **kw: Any) -> Any:
                self._suppress_until = max(self._suppress_until, self._clock() + SUPPRESS_S)
                return await orig_wled(*a, **kw)

            state.wled_post = wled_post

    # ── компьютер и будильник (опрос раз в тик, без нагрузки) ──

    def poll(self) -> None:
        t = self._clock()
        if self.bridge is not None:
            try:
                active = bool(self.bridge.pc_active())
            except Exception:
                active = None
            if active is not None:
                if self._pc_seen is None or self._pc_seen[0] != active:
                    self._pc_seen = (active, t)
                if self._pc is None:
                    self._pc = (active, t)
                elif self._pc[0] != active and t - self._pc_seen[1] >= PC_STABLE_S:
                    self._pc = (active, t)
                    self.record("pc", "", "start" if active else "stop", "pc",
                                self.now() - timedelta(seconds=PC_STABLE_S))
            alarm = int((getattr(self.bridge, "phone_state", {}) or {}).get("next_alarm", 0) or 0)
            if self._alarm is not None and alarm and alarm != self._alarm:
                self.record("alarm", "", "set", "phone")
            self._alarm = alarm

    # ── поиск закономерностей ──

    def _events(self) -> list[tuple[str, str, str, datetime]]:
        rows = self.db.execute("SELECT kind, target, action, ts FROM events ORDER BY id").fetchall()
        return [(k, tg, a, datetime.fromisoformat(ts)) for k, tg, a, ts in rows]

    async def _names(self, entity_ids: list[str]) -> list[str]:
        try:
            by_id = {e.entity_id: e.name for e in await self.ha.entities()}
        except HAError:
            by_id = {}
        return [by_id.get(i, i.partition(".")[2].replace("_", " ")) for i in entity_ids]

    def _known(self, spec: dict[str, Any]) -> bool:
        """Уже есть такая догадка (любого статуса, включая отклонённую)?"""
        for status, raw in self.db.execute(
                "SELECT l.status, h.spec FROM hunches h JOIN learned l ON l.id = h.id"):
            old = json.loads(raw)
            if (old["kind"], old["action"], old["mode"]) != (spec["kind"], spec["action"], spec["mode"]):
                continue
            if abs(_off(old["minute"] - spec["minute"])) > 45:
                continue
            if spec["kind"] not in ACTIONABLE or set(old["entities"]) & set(spec["entities"]):
                return True
        return False

    async def detect(self) -> list[int]:
        """Один ночной прогон: события → новые догадки. Возвращает id новых."""
        self.prune()
        patterns = merge_lights(find_patterns(self._events(), self.now().date()))
        added = []
        for p in patterns:
            spec = {k: p[k] for k in ("kind", "action", "mode", "minute", "n", "total", "entities")}
            if self._known(spec):
                continue
            actionable = p["kind"] in ACTIONABLE
            if actionable:
                spec["names"] = await self._names(spec["entities"])
            text = describe(spec)
            cur = self.db.execute(
                "INSERT INTO learned (kind, text, confidence, source, ts, applied, status) VALUES "
                "('hunch',?,?,?,?,0,?)", (text, p["conf"], "hunches", self._ts(), "candidate" if actionable else "insight"))
            self.db.execute("INSERT INTO hunches (id, spec) VALUES (?,?)", (cur.lastrowid, json.dumps(spec)))
            added.append(cur.lastrowid or 0)
        self.db.commit()
        self.brain.set_pref("hunch_last_detect", self.now().strftime("%Y-%m-%d"))
        log.info("догадки: новых %d", len(added))
        return added

    def detect_due(self) -> bool:
        n = self.now()
        return (self.brain.pref("hunch_last_detect") != n.strftime("%Y-%m-%d")
                and (n.hour, n.minute) >= DETECT_AT)

    # ── хранилище догадок ──

    def _row(self, hid: int) -> tuple[int, str, str, float, dict[str, Any], str | None, str | None] | None:
        r = self.db.execute(
            "SELECT l.id, l.text, l.status, l.confidence, h.spec, h.auto_id, h.proposed_ts FROM hunches h "
            "JOIN learned l ON l.id = h.id WHERE h.id = ?", (hid,)).fetchone()
        return (r[0], r[1], r[2], r[3], json.loads(r[4]), r[5], r[6]) if r else None

    def _by_status(self, *statuses: str) -> list[int]:
        q = ",".join("?" * len(statuses))
        return [r[0] for r in self.db.execute(
            f"SELECT l.id FROM hunches h JOIN learned l ON l.id = h.id WHERE l.status IN ({q}) ORDER BY l.id",
            statuses)]

    def _set(self, hid: int, status: str) -> None:
        self.db.execute("UPDATE learned SET status=?, applied=? WHERE id=?",
                        (status, 1 if status == "accepted" else 0, hid))

    def active_count(self) -> int:
        return len(self._by_status("accepted"))

    def waiting(self) -> bool:
        """Есть предложение, на которое ждём «да/нет»."""
        return bool(self._by_status("proposed"))

    # ── предложение ──

    def may_propose(self, force: bool = False) -> tuple[bool, str]:
        if self.brain.pref("hunches", "on") == "off":
            return False, "догадки выключены"
        if force:
            return True, ""
        n = self.now()
        if self.night is not None and self.night():
            return False, "ночь"
        if not PROPOSE_FROM <= n.hour < PROPOSE_TO:
            return False, "не время для предложений"
        if self.muted is not None and self.muted():
            return False, "режим «не слушай»"
        if self.presence is not None and not self.presence.here():
            return False, "пользователя нет в комнате"
        if self.waiting():
            return False, "жду ответа на прошлое предложение"
        today = n.strftime("%Y-%m-%d")
        if self.db.execute("SELECT 1 FROM hunches WHERE proposed_ts >= ?", (today,)).fetchone():
            return False, "сегодня уже предлагал"
        if self.active_count() >= MAX_AUTO:
            return False, f"уже {MAX_AUTO} автоматизации от меня, дальше только по команде"
        return True, ""

    def _expire(self) -> None:
        for hid in self._by_status("proposed"):
            row = self._row(hid)
            if row and row[6] and self._clock() - datetime.fromisoformat(row[6]).timestamp() > ANSWER_TTL_S:
                snoozes = self.db.execute("SELECT snoozes FROM hunches WHERE id=?", (hid,)).fetchone()[0] + 1
                self.db.execute("UPDATE hunches SET snoozes=? WHERE id=?", (snoozes, hid))
                self._set(hid, "candidate" if snoozes < MAX_SNOOZES else "rejected")
        self.db.commit()

    async def propose(self, force: bool = False) -> int | None:
        """Предложить лучшую догадку. Возвращает её id или None."""
        self._expire()
        ok, _ = self.may_propose(force)
        if not ok:
            return None
        cands = [self._row(i) for i in self._by_status("candidate")]
        cands = [c for c in cands if c]
        if not cands:
            return None
        hid, text, _, _, _, _, _ = max(cands, key=lambda c: c[3])
        said = False
        if self.announce is not None:
            said = await self.announce(
                f"Ты заметил закономерность: {text}. Сам заговори и коротко предложи: «{text}. Сделать, чтобы "
                f"я делал это сам?» Жди ответа. На «да» вызови hunch_answer(accept=true, id={hid}), на «нет» "
                f"hunch_answer(accept=false, id={hid}). Одна-две фразы, без нажима.")
        if not said and self.notify is not None:
            said = bool(await self.notify(f"{text}. Сделать это само (автоматизация «Мага:» в Home Assistant)? "
                                          "Ответь «да» или «нет»."))
        if not said:
            return None
        self.db.execute("UPDATE hunches SET proposed_ts=? WHERE id=?", (self._ts(), hid))
        self._set(hid, "proposed")
        self.db.commit()
        log.info("догадки: предложил %d (%s)", hid, text)
        return hid

    # ── HA: создание и удаление автоматизации ──

    def _url(self, auto_id: str) -> str:
        return f"{rest_base(self.ha.url)}/config/automation/config/{auto_id}"

    async def accept(self, hid: int | None = None) -> dict[str, Any]:
        if hid is None:
            ids = self._by_status("proposed")
            if not ids:
                return {"ok": False, "error": "нечего подтверждать"}
            hid = ids[-1]
        row = self._row(int(hid))
        if row is None or row[2] not in ("proposed", "candidate"):
            return {"ok": False, "error": "такой догадки нет или она уже обработана"}
        _, text, _, _, spec, _, _ = row
        if spec["kind"] not in ACTIONABLE:
            return {"ok": False, "error": "это наблюдение, автоматизировать его Home Assistant не умеет"}
        auto_id = f"{ID_PREFIX}{hid}_{int(self._clock())}"
        cfg = automation_config(spec, hid)
        try:
            status, _ = await self.http("POST", self._url(auto_id), self.ha.token, cfg)
        except HAError as e:
            return {"ok": False, "error": str(e)}
        if status != 200:
            return {"ok": False, "error": f"Home Assistant не создал автоматизацию (HTTP {status})"}
        self.db.execute("UPDATE hunches SET auto_id=?, alias=? WHERE id=?", (auto_id, cfg["alias"], hid))
        self._set(hid, "accepted")
        self.learning._log("автоматизация", f"{cfg['alias']} (по догадке: {text})", hid)
        self.db.commit()
        return {"ok": True, "id": hid, "automation": cfg["alias"]}

    async def reject(self, hid: int | None = None) -> dict[str, Any]:
        if hid is None:
            ids = self._by_status("proposed")
            if not ids:
                return {"ok": False, "error": "нечего отклонять"}
            hid = ids[-1]
        row = self._row(int(hid))
        if row is None or row[2] not in ("proposed", "candidate"):
            return {"ok": False, "error": "такой догадки нет или она уже обработана"}
        self._set(int(hid), "rejected")
        self.db.commit()
        return {"ok": True, "id": hid, "note": "больше не предложу"}

    async def undo(self, hid: int | None = None) -> dict[str, Any]:
        if hid is None:
            ids = self._by_status("accepted")
            if not ids:
                return {"ok": False, "error": "нечего откатывать: моих автоматизаций нет"}
            hid = ids[-1]
        row = self._row(int(hid))
        if row is None or row[2] != "accepted" or not row[5] or not row[5].startswith(ID_PREFIX):
            return {"ok": False, "error": "это не моя автоматизация или она уже откачена"}
        auto_id = row[5]
        try:
            status, cfg = await self.http("GET", self._url(auto_id), self.ha.token, None)
            if status == 200 and not str((cfg or {}).get("alias", "")).startswith(PREFIX):
                return {"ok": False, "error": "в Home Assistant под этим id чужая автоматизация, не трогаю"}
            if status == 200:
                status, _ = await self.http("DELETE", self._url(auto_id), self.ha.token, None)
            if status not in (200, 404):
                return {"ok": False, "error": f"Home Assistant не удалил автоматизацию (HTTP {status})"}
        except HAError as e:
            return {"ok": False, "error": str(e)}
        self._set(int(hid), "reverted")
        self.learning._log("откатил автоматизацию", row[1], int(hid))
        self.db.commit()
        return {"ok": True, "id": hid, "reverted": row[1]}

    # ── списки, инструменты, консоль ──

    def items(self) -> list[dict[str, Any]]:
        out = []
        for hid in self._by_status("candidate", "proposed", "accepted", "insight", "rejected", "reverted"):
            r = self._row(hid)
            if r:
                out.append({"id": hid, "text": r[1], "status": r[2], "confidence": r[3],
                            "automation": (self.db.execute("SELECT alias FROM hunches WHERE id=?", (hid,))
                                           .fetchone() or [None])[0]})
        return out

    def describe_list(self) -> str:
        names = {"candidate": "кандидат", "proposed": "предложено", "accepted": "автоматизация создана",
                 "insight": "наблюдение", "rejected": "отклонено", "reverted": "откачено"}
        rows = [f"[{i['id']}] {i['text']} ({i['confidence']:.2f}) — {names[i['status']]}"
                + (f", «{i['automation']}»" if i["status"] == "accepted" else "") for i in self.items()]
        return "\n".join(rows) or "догадок пока нет"

    def stats(self) -> str:
        c = Counter(k for (k,) in self.db.execute("SELECT kind FROM events"))
        return "события: " + (", ".join(f"{k} {n}" for k, n in sorted(c.items())) or "пусто")

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        hid = int(args["id"]) if args.get("id") is not None else None
        if name == "hunch_list":
            return {"ok": True, "hunches": [i for i in self.items() if i["status"] in
                                            ("candidate", "proposed", "accepted", "insight")]}
        if name == "hunch_undo":
            return await self.undo(hid)
        if name == "hunch_answer":
            return await (self.accept(hid) if args.get("accept") else self.reject(hid))
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    async def answer_text(self, accept: bool) -> str:
        """Ответ «да»/«нет» из Telegram."""
        r = await (self.accept() if accept else self.reject())
        if not r["ok"]:
            return r["error"]
        return f"Сделал: «{r['automation']}». Убрать: «hunch undo»." if accept else "Ладно, больше не предложу."

    async def command(self, line: str) -> str | None:
        """hunch list | run [N] | undo [N] | yes | no | detect | stats | on | off"""
        parts = line.split()
        if not parts or parts[0].lower() not in ("hunch", "hunches"):
            return None
        sub = parts[1].lower() if len(parts) > 1 else "list"
        num = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        if sub == "list":
            return self.describe_list()
        if sub == "stats":
            return self.stats()
        if sub in ("on", "off"):
            self.brain.set_pref("hunches", sub)
            return "догадки " + ("включены" if sub == "on" else "выключены")
        if sub == "detect":
            return f"новых догадок: {len(await self.detect())}"
        if sub == "undo":
            r = await self.undo(num)
            return ("откатил: " + r["reverted"]) if r["ok"] else r["error"]
        if sub in ("yes", "no"):
            return await self.answer_text(sub == "yes")
        if sub == "run":
            if num is not None:                       # команда пользователя: без лимитов
                r = await self.accept(num)
                return ("создал: " + r["automation"]) if r["ok"] else r["error"]
            await self.detect()
            hid = await self.propose(force=True)
            return f"предложил [{hid}]" if hid else "предлагать нечего (или некуда сказать)"
        return "hunch list | run [N] | undo [N] | yes | no | detect | stats | on | off"

    # ── жизненный цикл ──

    async def _tick(self) -> None:
        self.poll()
        if self.detect_due():
            await self.detect()
        await self.propose()

    async def _loop(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("догадки: шаг не вышел: %r", e)
            await asyncio.sleep(TICK_S)

    async def _later(self, event: dict[str, Any], when: datetime) -> None:
        await asyncio.sleep(CTX_DELAY_S)         # пусть call_service успеет зарегистрировать свой context.id
        self.process_state(event, when)

    async def session(self, ws: Any) -> None:
        """Одна сессия подписки: auth, subscribe_events, поток state_changed. Выход — исключением."""
        hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if hello.get("type") != "auth_required":
            raise HAError(f"HA ответил неожиданно: {hello}")
        await ws.send(json.dumps({"type": "auth", "access_token": self.ha.token}))
        if json.loads(await asyncio.wait_for(ws.recv(), 10)).get("type") != "auth_ok":
            raise HAError("Home Assistant не принял токен")
        await ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "event":
                self._tasks.append(asyncio.create_task(self._later(msg["event"], self.now())))
                self._tasks = [t for t in self._tasks if not t.done()]

    async def _watch(self) -> None:
        delay = 15.0
        while True:
            try:
                ws = await self.ha._connect(self.ha.url, max_size=None, open_timeout=10)
                delay = 15.0
                try:
                    await self.session(ws)
                finally:
                    await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("догадки: подписка на HA оборвалась: %r", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 600.0)

    def start(self) -> None:
        self._tasks += [asyncio.create_task(self._loop()), asyncio.create_task(self._watch())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
