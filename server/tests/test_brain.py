from datetime import datetime, timedelta

import pytest

from jarvis.brain import TOOLS, Brain
from jarvis.gemini_live import parse, setup_message

NOW = datetime(2026, 10, 19, 9, 0)


@pytest.fixture
def brain(tmp_path):
    clock = {"now": NOW}
    b = Brain(tmp_path / "b.db", now=lambda: clock["now"])
    b.clock = clock
    return b


@pytest.fixture
def sent():
    out = []

    async def send(msg):
        out.append(msg)
    send.out = out
    return send


def test_facts_dedupe_and_forget(brain):
    assert brain.remember("любит чай")
    assert not brain.remember("Любит ЧАЙ")
    assert brain.forget("чай") == 1
    assert brain.facts() == []


async def test_reminder_flow(brain, sent):
    out = await brain.call("add_reminder", {"text": "позвонить маме", "minutes": 30}, sent)
    assert out == {"ok": True, "when": "сегодня в 09:30"}
    assert sent.out[-1] == {"type": "reminders", "count": 1}
    assert brain.due() == []
    brain.clock["now"] = NOW + timedelta(minutes=31)
    assert [t for _, t in brain.due()] == ["позвонить маме"]
    brain.finish(brain.due()[0][0])
    assert brain.due() == []


async def test_reminder_at_and_past(brain, sent):
    out = await brain.call("add_reminder", {"text": "зубы", "at": "2026-10-20T08:15"}, sent)
    assert out["when"] == "завтра в 08:15"
    out = await brain.call("add_reminder", {"text": "вчера", "at": "2026-10-18T08:15"}, sent)
    assert out["ok"] is False
    assert (await brain.call("add_reminder", {"text": "x"}, sent))["ok"] is False


async def test_cancel_and_list(brain, sent):
    await brain.call("add_reminder", {"text": "купить хлеб", "minutes": 10}, sent)
    assert (await brain.call("list_reminders", {}, sent))["reminders"][0]["text"] == "купить хлеб"
    assert (await brain.call("cancel_reminder", {"text": "хлеб"}, sent))["cancelled"] == 1
    assert brain.pending() == []


async def test_timer_and_unknown(brain, sent):
    out = await brain.call("set_timer", {"minutes": 2.5, "label": "чай"}, sent)
    assert out["seconds"] == 150
    assert sent.out[-1] == {"type": "timer", "sec": 150, "label": "чай"}
    assert (await brain.call("nope", {}, sent))["ok"] is False


async def test_scheduler_retries_when_busy(brain, sent, monkeypatch):
    import asyncio
    from jarvis import brain as brain_mod
    monkeypatch.setattr(brain_mod, "POLL_S", 0.02)
    monkeypatch.setattr(brain_mod, "RETRY_S", 0.02)
    await brain.call("add_reminder", {"text": "чай", "minutes": 1}, sent)
    brain.clock["now"] = NOW + timedelta(minutes=2)
    said, answers = [], iter([False, True])

    async def remind(text):
        said.append(text)
        return next(answers)
    brain.start(remind)
    await asyncio.sleep(0.2)
    await brain.stop()
    assert said == ["чай", "чай"] and brain.due() == []


def test_tool_declarations_and_parse():
    assert {t["name"] for t in TOOLS} >= {"remember", "add_reminder", "set_timer"}
    assert setup_message("m", "v", "s", TOOLS)["setup"]["tools"][0]["functionDeclarations"] == TOOLS
    assert "tools" not in setup_message("m", "v", "s")["setup"]
    ev = parse({"toolCall": {"functionCalls": [{"id": "1", "name": "remember", "args": {"fact": "x"}}]}})[0]
    assert ev.kind == "tool_call" and ev.calls == (("1", "remember", {"fact": "x"}),)


async def test_current_time(brain, sent):
    out = await brain.call("current_time", {}, sent)
    assert out["time"] == "09:00" and out["date"] == "19.10.2026"


def test_messages_log_and_recent(brain):
    brain.log_message("голос", "user", "  привет  ")
    brain.log_message("telegram", "assistant", "здравствуй")
    brain.log_message("telegram", "user", "   ")      # пустое не пишем
    assert brain.recent_messages() == [(NOW, "голос", "user", "привет"),
                                       (NOW, "telegram", "assistant", "здравствуй")]


def test_messages_limit_and_age(brain):
    brain.log_message("голос", "user", "старое")
    brain.clock["now"] = NOW + timedelta(hours=30)
    for i in range(5):
        brain.log_message("telegram", "user", f"м{i}")
    assert [m[3] for m in brain.recent_messages(limit=3)] == ["м2", "м3", "м4"]     # старые первыми
    assert "старое" not in [m[3] for m in brain.recent_messages(limit=50)]          # старше суток
    assert "старое" in [m[3] for m in brain.recent_messages(limit=50, max_age_h=48)]


async def test_voice_prefs_survive_reopen(brain, sent, tmp_path):
    assert (await brain.call("set_voice", {"name": "puck"}, sent)) == {
        "ok": True, "voice": "Puck", "note": "новый голос со следующего разговора"}
    bad = await brain.call("set_voice", {"name": "Робот"}, sent)
    assert bad["ok"] is False and "Puck" in bad["available"]
    await brain.call("set_affective", {"enabled": "on"}, sent)
    again = Brain(tmp_path / "b.db")
    assert again.pref("voice") == "Puck" and again.pref("affective") == "1"
    assert again.pref("нет", "запас") == "запас"


async def test_generic_light_request_goes_to_the_room_not_to_name_matching(tmp_path):
    from jarvis.brain import Brain, is_room_query

    assert is_room_query("свет") and is_room_query("весь свет в комнате") and is_room_query("all")
    assert not is_room_query("торшер") and not is_room_query("подсветка стола") and not is_room_query("")

    class Lights:
        def __init__(self):
            self.calls = []

        async def room(self, action, source=""):
            self.calls.append((action, source))
            return {"ok": True, "note": "Свет выключен"}

        async def toggle_room(self):
            self.calls.append(("toggle", ""))
            return "погасил"

    class Ha:
        def __init__(self):
            self.seen = []

        async def call(self, name, args, send):
            self.seen.append((name, args))
            return {"ok": True}

    brain = Brain(tmp_path / "b.db")
    brain.ha, brain.lights = Ha(), Lights()

    async def send(_):
        return None

    assert (await brain.call("home_set", {"name": "all", "action": "off"}, send))["ok"]
    assert (await brain.call("home_set", {"name": "свет", "action": "toggle"}, send))["note"] == "погасил"
    assert brain.lights.calls == [("off", "инструмент home_set"), ("toggle", "")]
    assert brain.ha.seen == []                                       # HA name matching was not needed
    await brain.call("home_set", {"name": "торшер", "action": "off"}, send)
    await brain.call("home_set", {"name": "свет", "action": "on", "color": "красный"}, send)
    assert [n for n, _ in brain.ha.seen] == ["home_set", "home_set"]   # specific devices and colors go to HA
