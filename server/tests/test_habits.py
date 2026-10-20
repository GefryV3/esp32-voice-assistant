import random
from datetime import datetime, timedelta

from jarvis.brain import Brain
from jarvis.habits import Habits, parse_keywords, parse_windows, sharpness

SETTINGS = {"night_start": "23:30", "night_end": "07:00", "sarcasm": 60}


class Clock:
    def __init__(self, now):
        self.now, self.t = now, 0.0

    def advance(self, **kw):
        d = timedelta(**kw)
        self.now += d
        self.t += d.total_seconds()


class FakeGoogle:
    def __init__(self, tasks):
        self.tasks, self.done = tasks, []

    async def tasks_list(self):
        return {"tasks": self.tasks}

    async def tasks_done(self, title):
        self.done.append(title)
        return {"ok": True, "done": [title]}


class Speaker:
    def __init__(self, busy=0):
        self.busy, self.prompts = busy, []

    async def __call__(self, prompt):
        if self.busy > 0:
            self.busy -= 1
            return False
        self.prompts.append(prompt)
        return True


class FixedRng(random.Random):
    """random() всегда возвращает roll (бросок «спрашивать ли сегодня»), остальное как обычно."""

    def __init__(self, roll):
        super().__init__(1)
        self.roll = roll

    def random(self):
        return self.roll


def make(tmp_path, tasks, roll=0.0, busy=0, settings=None):
    c = Clock(datetime(2026, 10, 20, 12, 0))
    brain = Brain(tmp_path / "b.db", now=lambda: c.now)
    g, s = FakeGoogle(tasks), Speaker(busy)
    h = Habits(s, google=g, brain=brain, settings=lambda: settings or SETTINGS, prob=0.5,
               windows=[("18:00", "18:10")], now=lambda: c.now, rng=FixedRng(roll), clock=lambda: c.t)
    return h, c, g, s, brain


async def test_parsing():
    assert parse_windows("18:00-21:30, 12:00-12:30; кривое, 20:00-19:00") == [("18:00", "21:30"), ("12:00", "12:30")]
    assert parse_keywords("Тренировк, бассейн,") == ("тренировк", "бассейн")


async def test_asks_once_inside_window_and_prefers_habits(tmp_path):
    h, c, g, s, _ = make(tmp_path, ["Купить хлеб", "Тренировка"])
    await h.ask_step()                       # 12:00, ещё рано
    assert s.prompts == []
    c.advance(hours=6, minutes=15)           # 18:15: окно 18:00–18:10 прошло, срок наступил
    await h.ask_step()
    assert len(s.prompts) == 1 and "Тренировка" in s.prompts[0] and "habit_answer" in s.prompts[0]
    await h.ask_step()
    assert len(s.prompts) == 1               # один вопрос в день


async def test_no_question_when_dice_say_no(tmp_path):
    h, c, g, s, _ = make(tmp_path, ["Тренировка"], roll=0.99)
    c.advance(hours=8)
    await h.ask_step()
    assert s.prompts == []
    assert "вопроса нет" in await h.command("habits")


async def test_retries_when_speaker_busy(tmp_path):
    h, c, g, s, _ = make(tmp_path, ["Бассейн"], busy=2)
    c.advance(hours=6, minutes=15)
    assert await h.ask_step() == 30.0        # занята
    assert await h.ask_step() == 30.0
    assert await h.ask_step() == 60.0 and len(s.prompts) == 1   # освободилась


async def test_gives_up_after_an_hour(tmp_path):
    h, c, g, s, _ = make(tmp_path, ["Бассейн"], busy=10**6)
    c.advance(hours=6, minutes=15)
    await h.ask_step()
    c.advance(minutes=61)
    s.busy = 0
    await h.ask_step()
    assert s.prompts == []                   # час прошёл, вопрос уже не нужен


async def test_not_at_night(tmp_path):
    h, c, g, s, _ = make(tmp_path, ["Бассейн"], settings={**SETTINGS, "night_start": "17:00", "night_end": "19:00"})
    c.advance(hours=6, minutes=15)
    await h.ask_step()
    assert s.prompts == []


async def test_no_tasks_closes_day_quietly(tmp_path):
    h, c, g, s, _ = make(tmp_path, [])
    c.advance(hours=6, minutes=15)
    await h.ask_step()
    g.tasks = ["Бассейн"]
    await h.ask_step()
    assert s.prompts == []


async def test_answer_done_marks_google_and_resets_streak(tmp_path):
    h, c, g, s, brain = make(tmp_path, ["Тренировка"])
    assert (await h.call("habit_answer", {"task": "Тренировка", "done": False}))["misses_in_row"] == 1
    assert (await h.call("habit_answer", {"task": "тренировка", "done": False}))["misses_in_row"] == 2
    out = await h.call("habit_answer", {"task": "Тренировка", "done": True})
    assert out["marked_in_google"] == ["Тренировка"] and g.done == ["Тренировка"]
    assert h.misses("Тренировка") == 0
    assert h.week_stats() == {"done": ["Тренировка"], "missed": ["Тренировка", "тренировка"]}
    assert (await h.call("habit_answer", {"task": "", "done": True}))["ok"] is False


async def test_jibe_scales_with_sarcasm_slider_and_misses():
    assert sharpness(90, 1) == "" and sharpness(10, 5) == ""
    mild, sharp = sharpness(50, 3), sharpness(95, 3)
    assert mild and sharp and mild != sharp and "3-й" in mild


async def test_prompt_contains_jibe_only_after_misses(tmp_path):
    h, c, g, s, _ = make(tmp_path, ["Хомяк"], settings={**SETTINGS, "sarcasm": 95})
    assert "подколи" not in h.prompt("Покормить хомяка").casefold()
    for _ in range(3):
        h.log_answer("Покормить хомяка", False)
    assert "подколи" in h.prompt("Покормить хомяка").casefold()


async def test_pick_weights_toward_missed_habit(tmp_path):
    h, *_ = make(tmp_path, [], roll=0.3)       # при равных весах 0.3 выбрало бы первую
    assert h.pick(["Тренировка", "Бассейн"]) == "Тренировка"
    for _ in range(50):
        h.log_answer("Бассейн", False)
    assert h.pick(["Тренировка", "Бассейн"]) == "Бассейн"


async def test_brain_routes_habit_tool(tmp_path):
    h, c, g, s, brain = make(tmp_path, [])
    brain.extensions.append(h)
    assert "habit_answer" in {t["name"] for t in brain.tools()}

    async def send(m):
        pass
    assert (await brain.call("habit_answer", {"task": "Бассейн", "done": True}, send))["done"] is True
