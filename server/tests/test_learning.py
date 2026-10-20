import json
import random
from datetime import datetime, timedelta

import pytest

from jarvis.brain import Brain
from jarvis.learning import Learning, looks_secret, parse_items
from jarvis.voice import system_prompt

T0 = datetime(2026, 10, 20, 15, 0)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, answer="[]", **kw):
    c = Clock()
    brain = Brain(tmp_path / "b.db", now=c)
    sent, said = [], []

    async def ask(prompt):
        ask.prompts.append(prompt)
        return answer
    ask.prompts = []

    async def notify(text):
        sent.append(text)
        return True

    async def announce(prompt):
        said.append(prompt)
        return True

    kw.setdefault("rng", random.Random(1))
    lr = Learning(brain, ask=ask, notify=notify, announce=announce, now=c, **kw)
    brain.learning = lr
    return lr, brain, c, ask, sent, said


def talk(brain):
    brain.log_message("голос", "user", "Мага, короче отвечай по утрам")
    brain.log_message("голос", "assistant", "Ладно")


def test_secret_filter_and_parse():
    assert looks_secret("мой пароль 1234") and looks_secret("sk-abcdefghijklmnopqrstuvwx")
    assert not looks_secret("любит чай без сахара")
    raw = '```json\n[{"kind":"style","text":"отвечать короче","confidence":0.9},{"kind":"zzz","text":"x","confidence":1}]\n```'
    assert [i["text"] for i in parse_items(raw)] == ["отвечать короче"]
    assert parse_items("мусор") == [] and parse_items("[1,2") == []


@pytest.mark.asyncio
async def test_reflect_extract_apply_and_pending(tmp_path):
    ans = json.dumps([
        {"kind": "style", "text": "по утрам отвечать короче", "confidence": 0.9},
        {"kind": "habit", "text": "ложится около часа ночи", "confidence": 0.5},
        {"kind": "fact", "text": "его пароль от почты qwerty", "confidence": 0.95},
        {"kind": "fact", "text": "слабая догадка", "confidence": 0.2},
    ])
    lr, brain, c, ask, sent, _ = make(tmp_path, ans)
    talk(brain)
    added = await lr.reflect()
    assert len(added) == 2                           # секрет и слабое отброшены
    assert [t for _, _, t, _ in lr.items()] == ["по утрам отвечать короче"]
    assert [t for _, _, t, _ in lr.items("pending")] == ["ложится около часа ночи"]
    assert len(sent) == 1 and "Запомнить?" in sent[0]
    assert "по утрам отвечать короче" in lr.prompt_section()
    assert "около часа ночи" not in lr.prompt_section()
    # подмешивание в системную подсказку
    p = system_prompt(c(), learned=lr.prompt_section())
    assert "Что ты о нём понял" in p and "отвечать короче" in p
    # подтверждение
    assert lr.confirm(True)["ok"]
    assert "около часа ночи" in lr.prompt_section()
    # повторный прогон не плодит дубли
    assert await lr.reflect() == []


@pytest.mark.asyncio
async def test_reflect_empty_history_skips_model(tmp_path):
    lr, brain, c, ask, *_ = make(tmp_path)
    assert await lr.reflect() == [] and ask.prompts == []


def test_feedback_log_and_undo(tmp_path):
    lr, brain, c, *_ = make(tmp_path)
    assert lr.feedback("too_long")["new"]
    assert lr.feedback("wrong", "я просил свет в зале, а не в спальне")["ok"]
    assert not lr.feedback("wrong")["ok"] and not lr.feedback("абракадабра")["ok"]
    assert not lr.feedback("good", "пароль 12345678901234567")["ok"]
    sec = lr.prompt_section()
    assert "короче" in sec and "свет в зале" in sec
    assert "выучил" in lr.describe_changes()
    r = lr.undo()                                     # последняя правка
    assert r["ok"] and "свет в зале" in r["reverted"]
    assert "свет в зале" not in lr.prompt_section() and "короче" in lr.prompt_section()
    assert lr.undo("короче")["ok"]
    assert "короче" not in lr.prompt_section()
    assert not lr.undo()["ok"]
    assert "откатил" in lr.describe_changes()


def test_opposite_feedback_replaces(tmp_path):
    lr, *_ = make(tmp_path)
    lr.feedback("too_short")
    lr.feedback("too_long")
    sec = lr.prompt_section()
    assert "короче" in sec and "подробнее" not in sec


def test_forget_and_remember_unbroken(tmp_path):
    lr, brain, *_ = make(tmp_path)
    assert brain.remember("любит чай")
    lr.feedback("too_sarcastic")
    assert brain.forget("чай") == 1
    assert lr.forget("язвительн") == 1
    assert lr.items() == []


def test_prompt_section_bounded(tmp_path):
    lr, *_ = make(tmp_path)
    for i in range(30):
        lr.add("style", f"правило номер {i} " + "я" * 100, 0.9, "t")
    sec = lr.prompt_section()
    assert sec.count("\n- ") <= 8 and len(sec) < 1300
    assert "номер 29" in sec


@pytest.mark.asyncio
async def test_tools_via_brain(tmp_path):
    lr, brain, *_ = make(tmp_path)
    names = [t["name"] for t in brain.tools()]
    assert {"feedback", "what_changed", "undo_change"} <= set(names)

    async def send(m): pass
    out = await brain.call("feedback", {"kind": "too_long", "note": ""}, send)
    assert out["ok"]
    assert "Отвечай короче" in (await brain.call("what_changed", {}, send))["changes"]
    assert (await brain.call("undo_change", {}, send))["ok"]


def test_digest(tmp_path):
    lr, *_ = make(tmp_path)
    lr.feedback("too_long")
    lr.add("habit", "пьёт чай", 0.5, "reflection")
    lr.confirm(False)
    lr.undo()
    d = lr.digest()
    assert "принято правок 1" in d and "отклонено или откачено 2" in d


@pytest.mark.asyncio
async def test_initiative_limits(tmp_path):
    class Here:
        ok = True
        def here(self): return self.ok
    here = Here()
    night = {"v": False}
    lr, brain, c, _, _, said = make(tmp_path, presence=here, night=lambda: night["v"])
    c.t = datetime(2026, 10, 20, 8, 0)
    lr.add("followup", "завтра контрольная по математике", 0.9, "t", due="2026-10-20T07:00")
    assert await lr.maybe_speak(force=True)
    assert "контрольная" in said[0]
    assert not await lr.maybe_speak(force=True)       # слишком скоро
    c.t += timedelta(hours=3, minutes=1)
    here.ok = False
    assert not await lr.maybe_speak(force=True)       # нет на месте
    here.ok = True
    night["v"] = True
    assert not await lr.maybe_speak(force=True)       # ночь
    night["v"] = False
    assert await lr.maybe_speak(force=True)           # просто замечание
    c.t += timedelta(hours=3, minutes=1)
    assert await lr.maybe_speak(force=True)
    c.t += timedelta(hours=3, minutes=1)
    assert not await lr.maybe_speak(force=True)       # лимит 3 в сутки
    brain.set_pref("initiative", "off")
    c.t += timedelta(days=1)
    assert not await lr.maybe_speak(force=True)


def test_mood_by_time(tmp_path):
    lr, *_ = make(tmp_path)
    night = lr.mood(datetime(2026, 10, 20, 2, 0))[0]
    day = lr.mood(datetime(2026, 10, 20, 14, 0))[0]
    assert night < day
    assert "У тебя есть свои мнения" in lr.persona(T0)


@pytest.mark.asyncio
async def test_reflect_due_schedule(tmp_path):
    lr, brain, c, *_ = make(tmp_path)
    c.t = datetime(2026, 10, 20, 3, 0)
    assert not lr.reflect_due()
    c.t = datetime(2026, 10, 20, 4, 5)
    assert lr.reflect_due()
    brain.log_message("голос", "user", "привет")
    await lr.reflect()
    assert not lr.reflect_due()
