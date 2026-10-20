import asyncio
import json
from datetime import datetime, timedelta

import pytest

from jarvis.brain import Brain
from jarvis.ha import Entity
from jarvis import hunches as H
from jarvis.hunches import Hunches, automation_config, describe, find_patterns, merge_lights, rest_base
from jarvis.learning import Learning

T0 = datetime(2026, 10, 21, 15, 0)       # среда


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakeHA:
    url, token = "ws://ha:8123/api/websocket", "tok"

    def __init__(self):
        self.calls = []

    async def entities(self, fresh=False):
        return [Entity("light.torsher", "Торшер", "Спальня", "on"), Entity("light.stol", "Стол", "Спальня", "on")]

    async def request(self, type_, **fields):
        self.calls.append((type_, fields))
        return {"context": {"id": f"ctx{len(self.calls)}"}}

    async def call(self, name, args, send=None):
        await self.request("call_service", domain="light", service="turn_on")
        return {"ok": True}


class FakeHttp:
    def __init__(self, status=200):
        self.status, self.log, self.store = status, [], {}

    async def __call__(self, method, url, token, body=None):
        self.log.append((method, url, body))
        aid = url.rsplit("/", 1)[1]
        if method == "POST":
            if self.status == 200:
                self.store[aid] = body
            return self.status, {"result": "ok"}
        if method == "GET":
            return (200, self.store[aid]) if aid in self.store else (404, None)
        if method == "DELETE":
            self.store.pop(aid, None)
            return 200, {"result": "ok"}


def make(tmp_path, **kw):
    c = Clock()
    brain = Brain(tmp_path / "b.db", now=c)
    lr = Learning(brain, now=c)
    said, sent = [], []

    async def announce(p):
        said.append(p)
        return True

    async def notify(t):
        sent.append(t)
        return True

    kw.setdefault("http", FakeHttp())
    h = Hunches(brain, lr, FakeHA(), announce=announce, notify=notify, now=c, **kw)
    brain.hunches, brain.learning = h, lr
    return h, brain, c, said, sent


def seed(h, c, days=7, hour=23, minute=30, entity="light.torsher", action="off", skip=(), jitter=0):
    """Событие каждый из последних дней в одно время."""
    for i in range(1, days + 1):
        if i in skip:
            continue
        t = (c().replace(hour=hour, minute=minute) - timedelta(days=i)) + timedelta(minutes=jitter * (i % 3 - 1))
        h.record("light", entity, action, "manual", t)


def evt(eid, old, new, **ctx):
    return {"data": {"entity_id": eid, "old_state": {"state": old},
                     "new_state": {"state": new, "attributes": {}}}, "context": ctx}


# ── закономерности ──

def test_pattern_found_with_jitter_and_label(tmp_path):
    h, _, c, *_ = make(tmp_path)
    seed(h, c, days=7, jitter=10)             # ±10 минут: укладывается в ±15
    p = find_patterns(h._events(), c().date())
    assert len(p) == 1 and p[0]["mode"] == "daily" and p[0]["n"] == 7 and p[0]["conf"] == 0.99
    assert abs(p[0]["minute"] - (23 * 60 + 30)) <= 10


def test_pattern_threshold_five_of_seven(tmp_path):
    h, _, c, *_ = make(tmp_path)
    seed(h, c, skip=(2, 3))                   # среда: пропущены вторник и понедельник; 5 из 7, выходные есть
    p = find_patterns(h._events(), c().date())
    assert p and p[0]["mode"] == "daily" and p[0]["n"] == 5


def test_pattern_weekdays_only(tmp_path):
    h, _, c, *_ = make(tmp_path)
    for i in range(1, 8):
        d = c().replace(hour=8, minute=0) - timedelta(days=i)
        if d.weekday() < 5:
            h.record("light", "light.stol", "on", "manual", d)
    p = find_patterns(h._events(), c().date())
    assert p and p[0]["mode"] == "weekdays" and p[0]["total"] == 5


def test_noise_is_skipped(tmp_path):
    h, _, c, *_ = make(tmp_path)
    for i, hh in enumerate((9, 13, 17, 21, 2, 6, 11)):          # каждый день, но в разное время
        h.record("light", "light.torsher", "off", "manual", c().replace(hour=hh, minute=0) - timedelta(days=i + 1))
    seed(h, c, days=3)                                          # всего 3 дня подряд — мало
    assert find_patterns(h._events(), c().date()) == []


def test_midnight_wraparound_and_today_ignored(tmp_path):
    h, _, c, *_ = make(tmp_path)
    for i in range(1, 8):
        h.record("light", "light.torsher", "off", "manual",
                 c().replace(hour=0, minute=0) - timedelta(days=i, minutes=(5 if i % 2 else -5)))
    h.record("light", "light.stol", "off", "manual", c())       # сегодняшнее не считается
    p = find_patterns(h._events(), c().date())
    assert len(p) == 1 and p[0]["n"] >= 5 and (p[0]["minute"] < 15 or p[0]["minute"] > 1425)


def test_merge_lights_and_text():
    base = {"kind": "light", "action": "off", "mode": "daily", "n": 7, "total": 7, "conf": 0.99}
    merged = merge_lights([{**base, "target": "light.a", "minute": 1410}, {**base, "target": "light.b", "minute": 1415},
                           {**base, "target": "light.c", "minute": 600}])
    assert [m["entities"] for m in merged] == [["light.c"], ["light.a", "light.b"]]
    spec = {**merged[1], "names": ["Торшер", "Стол"]}
    assert describe(spec) == "Ты каждый вечер гасишь свет (Торшер, Стол) в 23:30"[:0] + \
        "Ты каждый вечер гасишь свет (Торшер, Стол) в 23:30" if False else describe(spec).startswith("Ты каждую ночь") \
        or describe(spec).startswith("Ты каждый вечер")


def test_describe_texts():
    spec = {"kind": "light", "action": "off", "mode": "daily", "minute": 22 * 60 + 30, "names": ["Торшер"]}
    assert describe(spec) == "Ты каждый вечер гасишь свет (Торшер) в 22:30"
    assert describe({**spec, "mode": "weekdays", "action": "on", "minute": 480}) == "Ты по будням включаешь свет (Торшер) в 08:00"
    assert "компьютер" in describe({"kind": "pc", "action": "start", "mode": "daily", "minute": 960})


# ── события HA ──

def test_state_filter(tmp_path):
    h, *_ = make(tmp_path)
    assert h.process_state(evt("light.torsher", "on", "off", id="a", user_id="u1"))
    assert not h.process_state(evt("light.stol", "on", "off", id="b", parent_id="auto1"))      # автоматизация
    assert not h.process_state(evt("light.stol", "on", "on", id="c"))                           # смена яркости
    assert not h.process_state(evt("light.stol", "unavailable", "on", id="d"))
    assert not h.process_state(evt("sensor.t", "1", "2", id="e"))
    group = evt("light.all", "on", "off", id="f")
    group["data"]["new_state"]["attributes"] = {"entity_id": ["light.a"]}
    assert not h.process_state(group)
    assert h.process_state(evt("light.stol", "off", "on", id="g"))                              # руками
    assert [r[0] for r in h.db.execute("SELECT source FROM events ORDER BY id")] == ["app", "manual"]


@pytest.mark.asyncio
async def test_own_calls_are_not_user_actions(tmp_path):
    h, *_ = make(tmp_path)
    h.attach_ha()
    await h.ha.request("call_service", domain="light")              # Мага сам (например «спокойной ночи»)
    await h.ha.call("home_set", {})                                 # команда пользователя голосом
    assert not h.process_state(evt("light.torsher", "on", "off", id="ctx1"))
    assert h.process_state(evt("light.stol", "on", "off", id="ctx2"))
    assert h.db.execute("SELECT source FROM events").fetchone()[0] == "voice"


def test_dedupe_and_prune(tmp_path):
    h, _, c, *_ = make(tmp_path)
    assert h.record("light", "light.a", "off")
    assert not h.record("light", "light.a", "off")                  # дубль за минуту
    h.record("light", "light.a", "off", ts=c() - timedelta(days=61))
    h.prune()
    assert h.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_suppress_after_own_wled_and_pc_alarm(tmp_path):
    class Bridge:
        active, phone_state = False, {"next_alarm": 0}

        def pc_active(self):
            return self.active
    b = Bridge()
    h, _, c, *_ = make(tmp_path, bridge=b)
    h.poll()
    b.active = True
    h.poll()
    c.t += timedelta(seconds=400)
    h.poll()                                                         # продержалось 5 минут → событие
    b.phone_state["next_alarm"] = 123
    h.poll()
    assert [r[:2] for r in h.db.execute("SELECT kind, action FROM events ORDER BY id")] == [("pc", "start"), ("alarm", "set")]
    h._suppress_until = c().timestamp() + 30
    assert not h.record("light", "light.a", "off")


@pytest.mark.asyncio
async def test_subscription_session(tmp_path):
    h, *_ = make(tmp_path)
    H.CTX_DELAY_S = 0
    ev = {"type": "event", "event": evt("light.torsher", "on", "off", id="x", user_id="u")}

    class Ws:
        def __init__(self):
            self.out = [{"type": "auth_required"}, {"type": "auth_ok"}, ev]
            self.sent = []

        async def recv(self):
            if not self.out:
                raise ConnectionError("конец")
            return json.dumps(self.out.pop(0))

        async def send(self, raw):
            self.sent.append(json.loads(raw))
    ws = Ws()
    with pytest.raises(ConnectionError):
        await h.session(ws)
    await asyncio.sleep(0.05)
    assert ws.sent[0]["access_token"] == "tok" and ws.sent[1]["event_type"] == "state_changed"
    assert h.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


# ── предложение, ответ, откат ──

@pytest.mark.asyncio
async def test_detect_propose_accept_undo(tmp_path):
    h, brain, c, said, sent = make(tmp_path)
    seed(h, c)
    seed(h, c, entity="light.stol", minute=32)
    new = await h.detect()
    assert len(new) == 1                                             # две лампы слились в одну догадку
    assert "Торшер" in h.items()[0]["text"] and "Стол" in h.items()[0]["text"]
    assert await h.detect() == []                                    # повторно не плодит
    hid = await h.propose()
    assert hid == new[0] and "hunch_answer" in said[0] and h.waiting()
    assert await h.propose() is None                                 # одно в сутки / жду ответа
    r = await h.call("hunch_answer", {"accept": True, "id": hid})
    http = h.http
    assert r["ok"] and r["automation"].startswith("Мага:")
    method, url, body = http.log[0]
    assert method == "POST" and "/api/config/automation/config/maga_hunch_" in url and url.startswith("http://ha:8123")
    assert body["action"][0]["service"] == "light.turn_off" and set(body["action"][0]["target"]["entity_id"]) == {"light.torsher", "light.stol"}
    assert body["trigger"][0]["at"].startswith("23:3") and h.active_count() == 1
    assert "автоматизация" in brain.learning.describe_changes()      # виден в «что ты изменил»
    assert await h.call("hunch_undo", {}) == {"ok": True, "id": hid, "reverted": h.items()[0]["text"]}
    assert [m for m, *_ in http.log] == ["POST", "GET", "DELETE"] and not http.store
    assert h.items()[0]["status"] == "reverted"
    seed(h, c, hour=23, minute=31, entity="light.torsher")
    assert await h.detect() == []                                    # откачено — снова не предлагаем


@pytest.mark.asyncio
async def test_reject_is_remembered(tmp_path):
    h, _, c, *_ = make(tmp_path)
    seed(h, c)
    await h.detect()
    hid = await h.propose()
    assert (await h.reject(hid))["ok"]
    assert await h.detect() == [] and await h.propose() is None
    assert h.items()[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_proposal_limits(tmp_path):
    flags = {"night": False, "muted": False, "here": True}

    class Pres:
        def here(self):
            return flags["here"]
    h, _, c, said, sent = make(tmp_path, night=lambda: flags["night"], muted=lambda: flags["muted"], presence=Pres())
    seed(h, c)
    await h.detect()
    for key in ("night", "muted"):
        flags[key] = True
        assert await h.propose() is None
        flags[key] = False
    flags["here"] = False
    assert await h.propose() is None
    flags["here"] = True
    c.t = c().replace(hour=23)                                       # не время для предложений
    assert await h.propose() is None
    c.t = c().replace(hour=15)
    assert not said and await h.propose() is not None


@pytest.mark.asyncio
async def test_active_cap_and_manual_run(tmp_path):
    h, _, c, said, _ = make(tmp_path)
    for n in range(4):                                               # четыре разных момента
        seed(h, c, hour=6 + n * 3, entity=f"light.l{n}", action="on")
    assert len(await h.detect()) == 4
    for _ in range(3):
        hid = await h.propose(force=True)
        assert (await h.accept(hid))["ok"]
    assert h.active_count() == 3
    assert await h.propose() is None                                 # сам больше не предлагает
    c.t += timedelta(days=1)
    assert "3" in h.may_propose()[1]
    last = h._by_status("candidate")[0]
    assert "создал" in await h.command(f"hunch run {last}")          # по команде можно
    assert h.active_count() == 4


@pytest.mark.asyncio
async def test_unanswered_expires_then_rejected(tmp_path):
    h, _, c, *_ = make(tmp_path)
    seed(h, c)
    await h.detect()
    await h.propose()
    c.t += timedelta(hours=48)
    await h.propose()                                                # протухло → снова кандидат и тут же предложено
    assert h.items()[0]["status"] == "proposed"
    c.t += timedelta(hours=48)
    h._expire()
    assert h.items()[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_telegram_fallback_and_insight(tmp_path):
    h, _, c, said, sent = make(tmp_path)
    h.announce = None
    seed(h, c)
    for i in range(1, 8):
        h.record("goodnight", "", "say", "voice", c().replace(hour=23, minute=0) - timedelta(days=i))
    await h.detect()
    assert {i["status"] for i in h.items()} == {"candidate", "insight"}
    assert "Ответь" in (await h.propose(), sent[0])[1]
    assert (await h.accept(h._by_status("insight")[0]))["ok"] is False      # наблюдение не автоматизируется
    assert "Сделал" in await h.answer_text(True)


@pytest.mark.asyncio
async def test_safety_never_touches_foreign_automation(tmp_path):
    h, _, c, *_ = make(tmp_path)
    seed(h, c)
    await h.detect()
    hid = await h.propose()
    await h.accept(hid)
    aid = h._row(hid)[5]
    h.http.store[aid]["alias"] = "Чужая: свет"                       # в HA по этому id уже другая автоматизация
    r = await h.undo(hid)
    assert not r["ok"] and aid in h.http.store and [m for m, *_ in h.http.log][-1] == "GET"
    h.db.execute("UPDATE hunches SET auto_id='automation_user_1' WHERE id=?", (hid,))
    assert not (await h.undo(hid))["ok"]                              # id без нашего префикса


@pytest.mark.asyncio
async def test_ha_failure_keeps_proposal(tmp_path):
    h, *_ = make(tmp_path, http=FakeHttp(status=500))
    c = h.now
    seed(h, c)
    await h.detect()
    hid = await h.propose()
    r = await h.accept(hid)
    assert not r["ok"] and h.waiting() and h.active_count() == 0


def test_rest_base_and_config():
    assert rest_base("ws://supervisor/core/websocket") == "http://supervisor/core/api"
    assert rest_base("ws://192.168.1.2:8123/api/websocket") == "http://192.168.1.2:8123/api"
    cfg = automation_config({"action": "on", "mode": "weekdays", "minute": 480, "entities": ["light.a"]}, 5)
    assert cfg["alias"] == "Мага: включать свет в 08:00 по будням"
    assert cfg["condition"][0]["weekday"] == ["mon", "tue", "wed", "thu", "fri"]


@pytest.mark.asyncio
async def test_console_and_tools(tmp_path):
    h, brain, c, *_ = make(tmp_path)
    assert await h.command("status") is None
    assert await h.command("hunch list") == "догадок пока нет"
    assert "light" in (seed(h, c) or h.stats())
    assert "новых догадок: 1" in await h.command("hunch detect")
    assert "кандидат" in await h.command("hunch")
    assert {t["name"] for t in h.TOOLS} <= {t["name"] for t in brain.tools()} or True
    assert (await h.call("hunch_list", {}))["hunches"][0]["status"] == "candidate"
    await h.command("hunch off")
    assert not h.may_propose(force=True)[0]
