import asyncio
import json
import sys
from pathlib import Path

import pytest

from jarvis import protocol
from jarvis.claude_status import STALE_S, ClaudeStatus, todo_pct

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import claude_hook  # noqa: E402


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cs():
    sent, said, prefs = [], [], {}

    async def send(m):
        sent.append(m)

    async def announce(t):
        said.append(t)
        return True

    clock = Clock()
    st = ClaudeStatus(send, announce, lambda k, d: prefs.get(k, d), prefs.__setitem__, clock)
    st.sent, st.said, st.prefs, st.clock_obj = sent, said, prefs, clock
    return st


def ev(name, **kw):
    return {"event": name, "session": "s1", "project": "jarvis", **kw}


def test_task_status_message_format():
    assert protocol.task_status("working", "Telegram-бот", 40) == {
        "type": "task_status", "state": "working", "label": "Telegram-бот", "pct": 40}
    assert protocol.task_status("done")["pct"] == -1
    assert len(protocol.task_status("working", "я" * 40)["label"].encode()) <= 31
    with pytest.raises(ValueError):
        protocol.task_status("sleeping")
    with pytest.raises(ValueError):
        protocol.task_status("working", "x", 101)


def test_todo_pct():
    assert todo_pct([{"status": "completed"}, {"status": "pending"}, {"status": "in_progress"}, {"status": "completed"}]) == 50
    assert todo_pct([]) == -1 and todo_pct(None) == -1


async def test_working_with_progress_then_done(cs):
    await cs.handle(ev("SessionStart"))
    assert cs.sent == []                                         # пустая открытая сессия на экране не нужна
    await cs.handle(ev("UserPromptSubmit"))
    assert cs.sent[-1] == protocol.task_status("working", "jarvis", -1)
    await cs.handle(ev("PreToolUse", todos=[{"status": "completed"}, {"status": "pending"}]))
    assert cs.sent[-1] == protocol.task_status("working", "jarvis", 50)
    n = len(cs.sent)
    await cs.handle(ev("PreToolUse"))                            # тот же статус не шлём повторно
    assert len(cs.sent) == n
    await cs.handle(ev("Stop", last="Готово."))
    assert cs.sent[-1] == protocol.task_status("done", "jarvis", 100)


async def test_permission_and_question_states(cs):
    await cs.handle(ev("UserPromptSubmit"))
    await cs.handle(ev("Notification", ntype="permission_prompt", message="Claude needs your permission to use Bash"))
    assert cs.sent[-1]["state"] == "needs_permission"
    await cs.handle(ev("PreToolUse"))                            # разрешили, работа пошла
    assert cs.sent[-1]["state"] == "working"
    await cs.handle(ev("Notification", ntype="elicitation_dialog"))
    assert cs.sent[-1]["state"] == "needs_input"
    await cs.handle(ev("Stop", last="Какой вариант выбрать?"))
    assert cs.sent[-1]["state"] == "needs_input"                 # закончил вопросом
    await cs.handle(ev("Notification", ntype="idle_prompt"))
    assert cs.sent[-1]["state"] == "needs_input"


async def test_idle_prompt_after_done_stays_done(cs):
    await cs.handle(ev("UserPromptSubmit"))
    await cs.handle(ev("Stop", last="Готово."))
    n = len(cs.sent)
    await cs.handle(ev("Notification", ntype="idle_prompt", message="Claude is waiting for your input"))
    assert len(cs.sent) == n and cs.sent[-1]["state"] == "done"


async def test_notification_without_type_falls_back_to_text(cs):
    await cs.handle(ev("Notification", message="Claude needs your permission to use Edit"))
    assert cs.sent[-1]["state"] == "needs_permission"


async def test_session_waiting_for_you_wins_over_newer_working_one(cs):
    await cs.handle(ev("UserPromptSubmit"))
    await cs.handle(ev("Notification", ntype="permission_prompt"))
    cs.clock_obj.t += 5
    await cs.handle({**ev("UserPromptSubmit"), "session": "s2", "project": "other"})
    assert cs.sent[-1] == protocol.task_status("needs_permission", "jarvis", -1)
    await cs.handle(ev("PreToolUse"))                            # s1 снова работает: показываем свежую
    assert cs.sent[-1]["state"] == "working"


async def test_stale_session_clears_screen(cs):
    await cs.handle(ev("UserPromptSubmit"))
    cs.clock_obj.t += STALE_S + 1
    await cs.handle({**ev("SessionStart"), "session": "s9"})
    assert cs.sent[-1] == protocol.task_status("idle")


async def test_speak_only_when_enabled_and_long_enough(cs):
    await cs.handle(ev("UserPromptSubmit"))
    cs.clock_obj.t += 120
    await cs.handle(ev("Stop", last="ok"))
    assert cs.said == []                                         # настройка выключена
    assert "вкл" in await cs.command("claude speak on")
    await cs.handle(ev("UserPromptSubmit"))
    cs.clock_obj.t += 5
    await cs.handle(ev("Stop", last="ok"))
    assert cs.said == []                                         # слишком коротко
    await cs.handle(ev("UserPromptSubmit"))
    cs.clock_obj.t += 120
    await cs.handle(ev("Stop", last="ok"))
    assert len(cs.said) == 1 and "jarvis" in cs.said[0] and "закончила" in cs.said[0]
    await cs.handle(ev("UserPromptSubmit"))
    cs.clock_obj.t += 120
    await cs.handle(ev("Stop", last="Что дальше?"))
    assert len(cs.said) == 1                                     # вопрос — не «закончила»
    assert "выкл" in await cs.command("claude speak off")


async def test_console_commands(cs):
    out = await cs.command("claude_event " + json.dumps(ev("UserPromptSubmit")))
    assert "jarvis: working" in out
    assert "jarvis: working" in await cs.command("claude")
    assert "JSON" in await cs.command("claude_event {не json")
    assert "не учитываю" in await cs.handle({"event": "SessionEnd"})
    assert await cs.command("что-то другое") is None


# ── сам скрипт хука ──

def test_hook_builds_compact_events():
    base = {"session_id": "abc", "cwd": "C:\\dev\\jarvis"}
    e = claude_hook.build_event({**base, "hook_event_name": "UserPromptSubmit", "prompt": "секретный промпт"})
    assert e == {"event": "UserPromptSubmit", "session": "abc", "project": "jarvis"}      # промпт не уходит
    e = claude_hook.build_event({**base, "hook_event_name": "PreToolUse", "tool_name": "TodoWrite", "tool_input": {
        "todos": [{"content": "секрет", "status": "completed"}, {"content": "x", "status": "pending"}]}})
    assert e["todos"] == [{"status": "completed"}, {"status": "pending"}]                 # тексты задач не уходят
    assert "todos" not in claude_hook.build_event({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash"})
    e = claude_hook.build_event({**base, "hook_event_name": "Notification", "notification_type": "permission_prompt",
                                 "message": "Claude needs your permission"})
    assert e["ntype"] == "permission_prompt"
    e = claude_hook.build_event({**base, "hook_event_name": "Stop", "last_assistant_message": "a" * 500 + "?"})
    assert len(e["last"]) == 200 and e["last"].endswith("?")
    assert claude_hook.build_event({**base, "hook_event_name": "SessionEnd"}) is None


async def test_hook_end_to_end_through_admin_route(cs):
    """Хук → /admin по настоящему WebSocket → консоль → ClaudeStatus."""
    from websockets.asyncio.server import serve

    async def handle(ws):
        assert await ws.recv() == "TOKEN"
        line = await ws.recv()
        await ws.send(await cs.command(line))

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        await claude_hook.send(f"ws://127.0.0.1:{port}/admin", "TOKEN",
                               claude_hook.build_event({"hook_event_name": "UserPromptSubmit", "session_id": "q",
                                                        "cwd": "/home/me/proj"}))
    assert cs.sent[-1] == protocol.task_status("working", "proj", -1)


def test_hook_never_fails_without_server(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO('{"hook_event_name": "Stop", "cwd": "/x"}'))
    monkeypatch.setattr(claude_hook, "read_env", lambda p: {"JARVIS_SERVER": "127.0.0.1", "JARVIS_PORT": "1"})
    claude_hook.main()                                           # порт закрыт: тихо возвращаемся
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("не json"))
    claude_hook.main()
    assert capsys.readouterr().out == ""
