import json
from datetime import date, datetime, timedelta

import pytest

from jarvis import morning as mo
from jarvis.brain import Brain
from jarvis.morning import Morning, MorningConfig, make_plan
from jarvis.routines import Routines
from jarvis.transit import Transit, TransitConfig, parse_board
from jarvis.umbrella import Umbrella, UmbrellaConfig, rain_in
from jarvis.untis import Lesson, Untis, UntisConfig, UntisError, parse_ical, parse_timetable

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20261021T060000Z
DTEND:20261021T065000Z
SUMMARY:Mathematik
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=Europe/Vienna:20261021T095000
DTEND;TZID=Europe/Vienna:20261021T104000
SUMMARY:Deutsch
 (Vertretung)
END:VEVENT
BEGIN:VEVENT
DTSTART:20261021T050000Z
DTEND:20261021T055000Z
SUMMARY:Physik
STATUS:CANCELLED
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:20261021
DTEND;VALUE=DATE:20261022
SUMMARY:Tag der offenen Tür
END:VEVENT
END:VCALENDAR
"""
D = date(2026, 10, 21)


def fetcher(responses):
    calls = []

    async def fetch(url, body, headers):
        calls.append((url, body))
        r = responses(url, body) if callable(responses) else responses
        return r if isinstance(r, bytes) else r.encode()
    fetch.calls = calls
    return fetch


# ── WebUntis ──

def test_parse_ical_zones_cancel_and_allday():
    ls = parse_ical(ICS)
    assert [(x.start.strftime("%H:%M"), x.subject, x.cancelled, x.changed) for x in ls] == [
        ("07:00", "Physik", True, False),         # 05:00Z → 07:00 по Вене (CEST)
        ("08:00", "Mathematik", False, False),
        ("09:50", "Deutsch(Vertretung)", False, True)]


async def test_first_lesson_skips_cancelled_ical():
    u = Untis(UntisConfig(ical_url="https://x/ical"), fetch=fetcher(ICS))
    f = await u.first_lesson(D)
    assert f.subject == "Mathematik" and f.start == datetime(2026, 10, 21, 8, 0)
    assert await u.first_lesson(date(2026, 10, 22)) is None


async def test_untis_unconfigured_raises():
    with pytest.raises(UntisError):
        await Untis(UntisConfig()).lessons(D)


def test_config_modes():
    assert UntisConfig.from_env({"UNTIS_ICAL_URL": "u"}).mode == "ical"
    assert UntisConfig.from_env({"UNTIS_SERVER": "a", "UNTIS_SCHOOL": "b", "UNTIS_USER": "c"}).mode == ""
    assert UntisConfig.from_env({"UNTIS_SERVER": "a", "UNTIS_SCHOOL": "b", "UNTIS_USER": "c", "UNTIS_PASSWORD": "d"}).mode == "rpc"


async def test_rpc_flow_logout_and_codes():
    seen = []

    def resp(url, body):
        req = json.loads(body)
        seen.append(req["method"])
        if req["method"] == "authenticate":
            assert req["params"]["user"] == "ivan"
            return json.dumps({"result": {"sessionId": "S", "personId": 7, "personType": 5}})
        if req["method"] == "getTimetable":
            return json.dumps({"result": [
                {"date": 20261021, "startTime": 800, "endTime": 850, "su": [{"name": "M"}], "code": "cancelled"},
                {"date": 20261021, "startTime": 950, "endTime": 1040, "su": [{"longname": "Deutsch"}], "code": "irregular"}]})
        return json.dumps({"result": None})
    f = fetcher(resp)
    u = Untis(UntisConfig(server="mese.webuntis.com", school="htl", user="ivan", password="p"), fetch=f)
    first = await u.first_lesson(D)
    assert first.subject == "Deutsch" and first.changed
    assert seen == ["authenticate", "getTimetable", "logout"]
    await u.lessons(D)
    assert len(seen) == 3                       # кэш


def test_parse_timetable_ignores_garbage():
    assert parse_timetable([{"date": "x"}, {}]) == []


# ── подъём ──

def lesson(h, m):
    return Lesson(datetime(2026, 10, 21, h, m), datetime(2026, 10, 21, h + 1, m), "M")


def test_make_plan_defaults():
    p = make_plan(lesson(8, 0), MorningConfig())
    assert p.depart == datetime(2026, 10, 21, 7, 19) and p.wake == datetime(2026, 10, 21, 6, 15)   # 6:19 → вниз до 5 мин


def test_make_plan_late_start_and_floor():
    assert make_plan(lesson(9, 50), MorningConfig()).wake == datetime(2026, 10, 21, 8, 5)
    early = make_plan(lesson(6, 0), MorningConfig(prep_min=120))
    assert early.wake.strftime("%H:%M") == "05:00"             # раньше WAKE_EARLIEST не будим


def test_make_plan_uses_real_bus():
    from jarvis.transit import Departure
    bus = Departure("265", "Zentrum", datetime(2026, 10, 21, 7, 11))
    assert make_plan(lesson(8, 0), MorningConfig(), bus).wake == datetime(2026, 10, 21, 6, 10)


def test_config_from_env_bad_values():
    c = MorningConfig.from_env({"WAKE_PREP_MIN": "abc", "WAKE_BUS_DEPART": "25:99", "SOFT_WAKE": "off", "WAKE_ASK_AT": "20:15"})
    assert c.prep_min == 60 and c.bus_depart == "07:19" and not c.soft_wake and c.ask_at == "20:15"
    assert c.commute_min == 41


class Phone:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)
        return True


class FakeBridge:
    def __init__(self, phone=None, next_alarm=0):
        self.phone, self.phone_state = phone, {"next_alarm": next_alarm}


class Say:
    def __init__(self, busy=0):
        self.busy, self.prompts = busy, []

    async def __call__(self, prompt):
        if self.busy:
            self.busy -= 1
            return False
        self.prompts.append(prompt)
        return True


def untis_ical():
    return Untis(UntisConfig(ical_url="https://x/ical"), fetch=fetcher(ICS))


def make_morning(now, tmp_path=None, **kw):
    clock = {"now": now}
    brain = Brain(tmp_path / "b.db", now=lambda: clock["now"]) if tmp_path else None
    say = kw.pop("say", None) or Say()
    kw.setdefault("untis", untis_ical())
    m = Morning(kw.pop("cfg", MorningConfig()), say, brain=brain, now=lambda: clock["now"], clock=lambda: 0.0, **kw)
    return m, say, clock


async def test_evening_ask_once(tmp_path):
    m, say, c = make_morning(datetime(2026, 10, 20, 21, 40), tmp_path)
    assert await m.ask_step() == mo.TICK_S
    assert len(say.prompts) == 1 and "06:15" in say.prompts[0] and "set_wake_alarm" in say.prompts[0]
    await m.ask_step()
    assert len(say.prompts) == 1                      # второй раз за вечер не спрашиваем
    m2 = Morning(MorningConfig(), say, untis=untis_ical(), brain=m.brain, now=lambda: c["now"], clock=lambda: 0.0)
    await m2.ask_step()
    assert len(say.prompts) == 1                      # и после перезапуска (pref)


async def test_ask_outside_window_and_busy_retry():
    m, say, c = make_morning(datetime(2026, 10, 20, 12, 0), say=Say(busy=1))
    await m.ask_step()
    assert not say.prompts
    c["now"] = datetime(2026, 10, 20, 21, 40)
    assert await m.ask_step() == mo.RETRY_S           # колонка занята
    assert await m.ask_step() == mo.TICK_S and len(say.prompts) == 1


async def test_ask_skipped_if_alarm_already_there_or_no_lessons():
    t = datetime(2026, 10, 21, 6, 15).timestamp() * 1000
    m, say, _ = make_morning(datetime(2026, 10, 20, 21, 40), bridge=FakeBridge(Phone(), next_alarm=t))
    await m.ask_step()
    assert not say.prompts
    m, say, _ = make_morning(datetime(2026, 10, 21, 21, 40))      # на 22-е уроков нет
    await m.ask_step()
    assert not say.prompts


async def test_set_wake_alarm_phone_and_ha(tmp_path):
    ph = Phone()
    ha_calls = []

    class HA:
        async def request(self, t, **kw):
            ha_calls.append((t, kw))
    m, _, _ = make_morning(datetime(2026, 10, 20, 21, 50), tmp_path, bridge=FakeBridge(ph), ha=HA(),
                           cfg=MorningConfig(ha_entity="input_datetime.wake"))
    r = await m.call("set_wake_alarm", {"time": "6:40"})
    assert r["ok"] and r["time"] == "06:40"
    assert ph.sent == [{"type": "set_alarm", "hour": 6, "minute": 40, "label": "Мага: подъём"}]
    assert ha_calls[0][1]["service_data"] == {"time": "06:40:00"} and ha_calls[0][1]["target"] == {"entity_id": "input_datetime.wake"}
    assert m.brain.pref(mo.PREF_ALARM) == "2026-10-20 06:40"
    assert not (await m.call("set_wake_alarm", {"time": "abc"}))["ok"]


async def test_set_wake_alarm_no_phone():
    m, _, _ = make_morning(datetime(2026, 10, 20, 21, 50), bridge=FakeBridge(None))
    r = await m.call("set_wake_alarm", {"time": "06:40"})
    assert not r["ok"] and "телефон" in r["error"]


async def test_wake_plan_tool_finds_next_school_day():
    m, _, _ = make_morning(datetime(2026, 10, 19, 20, 0))
    r = await m.call("wake_plan", {})
    assert r["day"] == "2026-10-21" and r["wake"] == "06:15"      # 20-го уроков нет, ищем дальше


async def test_tools_depend_on_config():
    m, _, _ = make_morning(datetime(2026, 10, 20, 20, 0), untis=Untis(UntisConfig()))
    assert m.tools() == []
    m2, _, _ = make_morning(datetime(2026, 10, 20, 20, 0))
    assert {t["name"] for t in m2.tools()} == {"wake_plan", "set_wake_alarm"}


# ── автобус ──

def board(jnys, prods=None):
    return {"svcResL": [{"err": "OK", "res": {"common": {"prodL": prods or [
        {"name": "Bus 265", "number": "265"}, {"name": "Bus 260", "number": "260"}]}, "jnyL": jnys}}]}


def jny(prod, hhmm, real=None, cncl=False, day="20261021"):
    stop = {"dTimeS": hhmm + "00"}
    if real:
        stop["dTimeR"] = real + "00"
    if cncl:
        stop["dCncl"] = True
    return {"date": day, "prodX": prod, "dirTxt": "Zentrum", "stbStop": stop}


def test_parse_board_filter_delay_cancel():
    deps = parse_board(board([jny(0, "0719", "0725"), jny(1, "0716"), jny(0, "0749", cncl=True)]), "265")
    assert [(d.line, d.delay_min, d.cancelled) for d in deps] == [("265", 6, False), ("265", 0, True)]
    assert "опаздывает на 6" in deps[0].describe() and "отменён" in deps[1].describe()


def test_parse_board_error():
    with pytest.raises(Exception):
        parse_board({"svcResL": [{"err": "LOCATION", "errTxt": "x"}]})


def transit(jnys, now):
    def resp(url, body):
        req = json.loads(body)["svcReqL"][0]
        if req["meth"] == "LocMatch":
            return json.dumps({"svcResL": [{"err": "OK", "res": {"match": {"locL": [{"extId": "1234567", "name": "Musterdorf"}]}}}]})
        return json.dumps(board(jnys))
    f = fetcher(resp)
    return Transit(TransitConfig(stop="Musterdorf", line="265"), fetch=f, now=lambda: now), f


async def test_transit_bus_at_and_before():
    now = datetime(2026, 10, 21, 6, 40)
    t, f = transit([jny(0, "0649"), jny(0, "0719", "0726"), jny(0, "0749")], now)
    assert (await t.bus_at(datetime(2026, 10, 21, 7, 19))).delay_min == 7
    assert (await t.bus_before(datetime(2026, 10, 21, 7, 30))).planned.strftime("%H:%M") == "07:19"
    assert (await t.bus_before(datetime(2026, 10, 21, 7, 0))).planned.strftime("%H:%M") == "06:49"
    stops = [json.loads(b)["svcReqL"][0] for _, b in f.calls if b]
    assert sum(1 for s in stops if s["meth"] == "LocMatch") == 1        # остановку ищем один раз
    board_req = next(s for s in stops if s["meth"] == "StationBoard")["req"]
    assert board_req["stbLoc"] == {"lid": "A=1@L=1234567@"}


async def test_plan_uses_transit_bus():
    now = datetime(2026, 10, 20, 21, 40)
    t, _ = transit([jny(0, "0711", day="20261021")], now)
    m, _, _ = make_morning(now, transit=t)
    p = await m.plan(date(2026, 10, 21))
    assert p.bus and p.wake == datetime(2026, 10, 21, 6, 10)


async def test_bus_warning_late_and_dedupe():
    now = datetime(2026, 10, 21, 6, 50)
    t, _ = transit([jny(0, "0719", "0725")], now)
    m, say, c = make_morning(now, transit=t)
    assert await m.bus_step() == mo.BUS_POLL_S
    assert len(say.prompts) == 1 and "опаздывает на 6" in say.prompts[0]
    await m.bus_step()
    assert len(say.prompts) == 1                                     # то же опоздание повторно не говорим


async def test_bus_cancelled_names_next():
    now = datetime(2026, 10, 21, 6, 50)
    t, _ = transit([jny(0, "0719", cncl=True), jny(0, "0749")], now)
    m, say, _ = make_morning(now, transit=t)
    await m.bus_step()
    assert "отменён" in say.prompts[0] and "07:49" in say.prompts[0]


async def test_bus_quiet_when_on_time_not_in_window_or_asleep():
    now = datetime(2026, 10, 21, 6, 50)
    t, _ = transit([jny(0, "0719", "0720")], now)
    m, say, c = make_morning(now, transit=t)
    await m.bus_step()
    c["now"] = datetime(2026, 10, 21, 5, 0)                           # слишком рано
    await m.bus_step()
    assert not say.prompts

    class Asleep:
        def awake(self):
            return False
    t2, _ = transit([jny(0, "0719", "0730")], now)
    m2, say2, _ = make_morning(now, transit=t2)
    m2.routines = Asleep()
    await m2.bus_step()
    assert not say2.prompts


async def test_next_bus_tool():
    now = datetime(2026, 10, 21, 7, 0)
    t, _ = transit([jny(0, "0719", "0725"), jny(0, "0749")], now)
    m, _, _ = make_morning(now, transit=t, untis=Untis(UntisConfig()))
    r = await m.call("next_bus", {})
    assert r["buses"][0].startswith("265 в 07:19, опаздывает") and len(r["buses"]) == 2


async def test_wake_context_has_lesson_and_bus():
    now = datetime(2026, 10, 21, 6, 20)
    t, _ = transit([jny(0, "0719", "0725")], now)
    m, _, _ = make_morning(now, transit=t)
    ctx = await m.wake_context()
    assert "08:00" in ctx and "Mathematik" in ctx and "опаздывает на 6" in ctx


# ── мягкое пробуждение (в рутинах) ──

SETTINGS = {"night_start": "23:30", "night_end": "07:00"}


class T:
    def __init__(self, now):
        self.now, self.t = now, 0.0

    def advance(self, s):
        self.now += timedelta(seconds=s)
        self.t += s


async def test_soft_wake_merges_with_briefing(tmp_path):
    c = T(datetime(2026, 10, 21, 6, 20))             # ещё «ночь» до 07:00
    say = Say()

    async def ctx():
        return "Известно из расписания: первый урок в 08:00."
    r = Routines(say, settings=lambda: SETTINGS, brain=Brain(tmp_path / "b.db"), now=lambda: c.now, clock=lambda: c.t,
                 soft_wake_s=30, wake_context=ctx)
    r.wake_signal()
    await r.briefing_step()
    assert not say.prompts                           # 30 секунд ещё не прошло
    c.advance(31)
    await r.briefing_step()
    assert len(say.prompts) == 1 and "вполголоса" in say.prompts[0] and "первый урок в 08:00" in say.prompts[0]
    c.advance(4000)
    await r.briefing_step()
    assert len(say.prompts) == 1                     # обычный брифинг в 08:00 не дублируется
    assert r.briefing_due() is False


async def test_without_soft_wake_old_behaviour(tmp_path):
    c = T(datetime(2026, 10, 21, 6, 20))
    say = Say()
    r = Routines(say, settings=lambda: SETTINGS, now=lambda: c.now, clock=lambda: c.t)
    r.wake_signal()
    c.advance(60)
    await r.briefing_step()
    assert not say.prompts                           # ночь, как раньше


async def test_leave_hook_fires_on_home_to_away():
    class HA:
        def __init__(self):
            self.state = "home"

        async def request(self, t, **kw):
            return [{"entity_id": "person.mag", "state": self.state}]
    left = []

    async def on_leave():
        left.append(1)
    ha = HA()
    c = T(datetime(2026, 10, 21, 8, 0))
    r = Routines(Say(), settings=lambda: SETTINGS, ha=ha, now=lambda: c.now, clock=lambda: c.t, on_leave=on_leave)
    await r.poll_home()
    assert not r._left
    ha.state = "not_home"
    await r.poll_home()
    assert r._left


# ── зонт ──

RAIN = {"hourly": {"time": ["2026-10-21T08:00", "2026-10-21T09:00", "2026-10-21T10:00"],
                   "precipitation_probability": [10, 20, 70], "precipitation": [0, 0, 1.2]}}


def test_rain_in():
    assert "10:00" in rain_in(RAIN, 50, datetime(2026, 10, 21, 8, 10))
    assert rain_in(RAIN, 80, datetime(2026, 10, 21, 8, 10)).startswith("около 10:00")     # осадки 1.2 мм
    assert rain_in(RAIN, 50, datetime(2026, 10, 21, 10, 30)) != ""
    assert rain_in(RAIN, 50, datetime(2026, 10, 21, 11, 30)) == ""        # прогноз уже прошёл
    assert rain_in({"hourly": {"time": ["2026-10-21T08:00"], "precipitation_probability": [5], "precipitation": [0]}},
                   50, datetime(2026, 10, 21, 8, 0)) == ""


async def test_umbrella_on_leave():
    say = Say()
    clock = {"t": 0.0}
    u = Umbrella(UmbrellaConfig(), say, fetch=fetcher(json.dumps(RAIN)), now=lambda: datetime(2026, 10, 21, 8, 10),
                 clock=lambda: clock["t"])
    assert await u.on_leave() and "зонт" in say.prompts[0]
    assert not await u.on_leave()                                  # не чаще раза в 3 часа
    clock["t"] = 4 * 3600
    assert await u.on_leave()


async def test_umbrella_off_night_dry_and_error():
    say = Say()
    kw = dict(now=lambda: datetime(2026, 10, 21, 8, 10))
    assert not await Umbrella(UmbrellaConfig(enabled=False), say, fetch=fetcher(json.dumps(RAIN)), **kw).on_leave()
    assert not await Umbrella(UmbrellaConfig(), say, fetch=fetcher(json.dumps(RAIN)), night=lambda: True, **kw).on_leave()
    dry = {"hourly": {"time": ["2026-10-21T08:00"], "precipitation_probability": [0], "precipitation": [0]}}
    assert not await Umbrella(UmbrellaConfig(), say, fetch=fetcher(json.dumps(dry)), **kw).on_leave()

    async def boom(*a):
        raise OSError("нет сети")
    assert not await Umbrella(UmbrellaConfig(), say, fetch=boom, **kw).on_leave()
    assert not say.prompts


def test_umbrella_config():
    c = UmbrellaConfig.from_env({"UMBRELLA": "off", "WEATHER_LAT": "x", "UMBRELLA_PROB": "30"})
    assert not c.enabled and c.lat == 48.208 and c.prob == 30


async def test_fixed_schedule_and_fallback_give_620_wake():
    from datetime import date
    from jarvis.untis import FixedSchedule, Untis, UntisConfig, UntisError, WithFallback

    class Broken(Untis):
        @property
        def enabled(self):
            return True

        async def lessons(self, day):
            raise UntisError("WebUntis: bad credentials")

    fixed = FixedSchedule("08:00")
    assert await fixed.lessons(date(2026, 10, 24)) == []                       # суббота
    wed = await fixed.lessons(date(2026, 10, 21))
    assert [x.start.strftime("%H:%M") for x in wed] == ["08:00"]
    both = WithFallback(Broken(UntisConfig()), fixed)
    assert [x.start.strftime("%H:%M") for x in await both.lessons(date(2026, 10, 21))] == ["08:00"]
    assert both.enabled and await both.lessons(date(2026, 10, 25)) == []        # воскресенье
