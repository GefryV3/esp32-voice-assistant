import importlib.util
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jarvis import routines as rt
from jarvis.brain import Brain
from jarvis.config import ConfigError, load_settings
from jarvis.routines import Routines

SETTINGS = {"night_start": "23:30", "night_end": "07:00"}


class Speaker:
    def __init__(self, busy=0):
        self.busy, self.prompts = busy, []

    async def __call__(self, prompt):
        if self.busy > 0:
            self.busy -= 1
            return False
        self.prompts.append(prompt)
        return True


class Clock:
    def __init__(self, now):
        self.now, self.t = now, 0.0

    def advance(self, **kw):
        d = timedelta(**kw)
        self.now += d
        self.t += d.total_seconds()


class FakeHA:
    def __init__(self):
        self.persons = {"person.mag": "home"}

    async def request(self, type_, **kw):
        assert type_ == "get_states"
        return [{"entity_id": k, "state": v} for k, v in self.persons.items()] + [
            {"entity_id": "light.x", "state": "on"}]


def make(start, tmp_path=None, **kw):
    c = Clock(start)
    brain = Brain(tmp_path / "b.db", now=lambda: c.now) if tmp_path else None
    say = kw.pop("say", None) or Speaker()
    r = Routines(say, settings=lambda: SETTINGS, brain=brain, now=lambda: c.now, clock=lambda: c.t, **kw)
    return r, say, c


# ── брифинг ──

async def test_briefing_after_alarm_once_a_day(tmp_path):
    r, say, c = make(datetime(2026, 10, 19, 7, 30), tmp_path)
    assert await r.briefing_step() == rt.TICK_S and not say.prompts   # будильника не было, 08:00 не пришло
    r.wake_signal()
    await r.briefing_step()
    assert len(say.prompts) == 1 and "брифинг" in say.prompts[0]
    c.advance(hours=1)
    await r.briefing_step()
    assert len(say.prompts) == 1                                      # второй раз за день — нет
    r2 = Routines(say, settings=lambda: SETTINGS, brain=r.brain, now=lambda: c.now, clock=lambda: c.t)
    await r2.briefing_step()                                          # и после перезапуска тоже нет
    assert len(say.prompts) == 1
    c.advance(days=1)
    r.wake_signal()
    await r.briefing_step()
    assert len(say.prompts) == 2


async def test_briefing_by_time_when_no_alarm():
    r, say, c = make(datetime(2026, 10, 19, 7, 59), briefing_at="08:00")
    await r.briefing_step()
    assert not say.prompts
    c.advance(minutes=2)
    await r.briefing_step()
    assert len(say.prompts) == 1


async def test_briefing_by_time_not_too_late():
    r, say, _ = make(datetime(2026, 10, 19, 15, 0))      # сервер поднялся днём — утро уже прошло
    await r.briefing_step()
    assert not say.prompts


async def test_briefing_not_before_night_end():
    r, say, c = make(datetime(2026, 10, 19, 5, 0))
    r.wake_signal()                                       # будильник в 5 утра, ночь до 07:00
    await r.briefing_step()
    assert not say.prompts
    c.advance(hours=2, minutes=1)
    await r.briefing_step()
    assert len(say.prompts) == 1


async def test_briefing_retries_when_busy_then_gives_up():
    r, say, c = make(datetime(2026, 10, 19, 7, 30), say=Speaker(busy=2))
    r.wake_signal()
    assert await r.briefing_step() == rt.RETRY_S
    assert await r.briefing_step() == rt.RETRY_S
    await r.briefing_step()
    assert len(say.prompts) == 1

    r, say, c = make(datetime(2026, 10, 19, 7, 30), say=Speaker(busy=10**6))
    r.wake_signal()
    await r.briefing_step()
    c.advance(seconds=rt.GIVE_UP_S + 1)
    await r.briefing_step()
    c.advance(minutes=5)
    await r.briefing_step()
    assert not say.prompts and r._briefed == "2026-10-19"      # сдались и до завтра не пристаём


# ── возвращение домой ──

async def test_home_greets_after_long_absence():
    ha = FakeHA()
    r, say, c = make(datetime(2026, 10, 19, 14, 0), ha=ha)
    assert not await r.poll_home()                       # первый опрос: просто запомнили
    ha.persons["person.mag"] = "not_home"
    assert not await r.poll_home()
    c.advance(hours=2)
    ha.persons["person.mag"] = "home"
    assert await r.poll_home()
    assert await r.greet() and "вернулся домой" in say.prompts[0]


async def test_home_short_absence_and_rate_limit_and_night():
    ha = FakeHA()
    r, say, c = make(datetime(2026, 10, 19, 14, 0), ha=ha)
    await r.poll_home()
    ha.persons["person.mag"] = "not_home"
    await r.poll_home()
    c.advance(minutes=10)
    ha.persons["person.mag"] = "home"
    assert not await r.poll_home()                        # вышел за хлебом — не здороваемся

    assert await r.greet()
    c.advance(hours=1)
    assert not await r.greet() and len(say.prompts) == 1  # не чаще раза в 3 часа
    c.advance(hours=3)
    c.now = c.now.replace(hour=2)
    assert not await r.greet() and len(say.prompts) == 1  # ночью нет


async def test_home_without_ha_is_skipped(caplog):
    r, _, _ = make(datetime(2026, 10, 19, 14, 0))
    assert not await r.poll_home()
    with caplog.at_level("INFO"):
        r.start()
        await r.stop()
    assert "приветствие при возвращении домой пропущено" in caplog.text


# ── консоль и настройки ──

async def test_console_command():
    r, say, _ = make(datetime(2026, 10, 19, 3, 0))        # ночью тоже: это ручная проверка
    assert await r.command("ask привет") is None
    assert "briefing|home" in await r.command("routine")
    assert "заговорила" in await r.command("routine briefing")
    await r.command("routine home")
    assert len(say.prompts) == 2
    r.announce = Speaker(busy=1)
    assert "занята" in await r.command("routine home")


def test_settings():
    base = {"JARVIS_DEVICE_TOKEN": "t"}
    s = load_settings(".nope", base)
    assert s.routines and s.briefing_at == "08:00"
    s = load_settings(".nope", {**base, "JARVIS_ROUTINES": "off", "JARVIS_BRIEFING_AT": "07:45"})
    assert not s.routines and s.briefing_at == "07:45"
    with pytest.raises(ConfigError, match="JARVIS_BRIEFING_AT"):
        load_settings(".nope", {**base, "JARVIS_BRIEFING_AT": "25:00"})


# ── перенос памяти ──

def tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "import_memory.py"
    spec = importlib.util.spec_from_file_location("import_memory", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


OLD = {
    "profile": {"имя": "Inokentiy", "часовой_пояс": "Europe/Vienna", "заметки": ["не любит звонки"]},
    "facts": [{"text": "любит чай", "added": "2026-01-01T10:00:00"}, {"text": "Любит ЧАЙ"}],
    "reminders": [
        {"text": "врач", "due": "2026-10-25T10:00:00", "done": False},
        {"text": "старое", "due": "2026-10-01T10:00:00", "done": False},
        {"text": "сделано", "due": "2026-10-30T10:00:00", "done": True},
    ],
    "conversation_summary": "",
}


def test_import_memory(tmp_path):
    brain = Brain(tmp_path / "b.db", now=lambda: datetime(2026, 10, 19, 9, 0))
    mod = tool()
    s = mod.import_memory(brain, OLD)
    assert s == {"facts": 4, "dup_facts": 1, "reminders": 1, "dup_reminders": 0, "expired": 1, "done": 1}
    assert "имя: Inokentiy" in brain.facts() and "часовой пояс: Europe/Vienna" in brain.facts()
    assert [t for _, t, _ in brain.pending()] == ["врач"]
    s2 = mod.import_memory(brain, OLD)                    # повторный запуск ничего не задваивает
    assert s2["facts"] == 0 and s2["reminders"] == 0 and s2["dup_reminders"] == 1


def test_import_memory_template(tmp_path):
    template = json.loads((Path(__file__).resolve().parents[2] / "legacy" / "memory" / "memory_template.json")
                          .read_text(encoding="utf-8"))
    brain = Brain(tmp_path / "b.db")
    s = tool().import_memory(brain, template)
    assert s["facts"] == 2 and s["reminders"] == 0



async def test_phone_on_home_wifi_ignores_gps_flapping():
    # GPS-метка HA прыгает «дома / не дома» без причины: колонка говорила «возьми зонт» и «с возвращением» дома
    ha = FakeHA()
    on_wifi = [True]
    r, say, c = make(datetime(2026, 10, 19, 14, 0), ha=ha, phone_home=lambda: on_wifi[0])
    assert not await r.poll_home()
    ha.persons["person.mag"] = "not_home"                 # скачок GPS, телефон на домашнем Wi-Fi
    assert not await r.poll_home() and not r._left
    c.advance(hours=2)
    ha.persons["person.mag"] = "home"
    assert not await r.poll_home()                        # «вернулся» тоже не выдумываем
    on_wifi[0] = False                                    # телефон реально ушёл из сети
    ha.persons["person.mag"] = "not_home"
    assert not await r.poll_home() and r._left            # настоящий уход виден
    c.advance(hours=2)
    on_wifi[0] = True                                     # телефон снова в домашней сети: человек дома
    assert await r.poll_home()
