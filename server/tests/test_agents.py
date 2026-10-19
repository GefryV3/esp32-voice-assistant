import asyncio

import pytest

from jarvis import agents as agents_mod
from jarvis.agents import TOOLS, Agents
from jarvis.brain import Brain


class Speaker:
    """Подмена Voice.announce: сначала «занята», потом принимает."""

    def __init__(self, busy_times=0):
        self.busy = busy_times
        self.prompts: list[str] = []

    async def __call__(self, prompt):
        if self.busy > 0:
            self.busy -= 1
            return False
        self.prompts.append(prompt)
        return True


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(agents_mod, "RETRY_S", 0.02)


async def settle(cond, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not cond():
            await asyncio.sleep(0.01)


async def test_task_runs_in_background_and_is_announced():
    gate = asyncio.Event()

    async def generate(goal):
        await gate.wait()
        return "Курс евро сегодня около одного доллара."

    say = Speaker()
    ag = Agents("k", "m", say, generate=generate)
    ag.start_delivery()
    out = ag.start("узнай курс евро", "Курс евро")
    assert out["ok"] and "в фоне" in out["note"]
    assert (await ag.call("list_tasks", {}))["tasks"] == [{"title": "Курс евро", "status": "running"}]
    assert not say.prompts                      # пока работает — молчим
    gate.set()
    await settle(lambda: say.prompts)
    assert "Курс евро" in say.prompts[0] and "около одного доллара" in say.prompts[0]
    assert ag.tasks[0].status == "done" and ag.tasks[0].spoken
    await ag.stop()


async def test_retries_while_speaker_busy_and_reports_failure():
    async def generate(goal):
        raise RuntimeError("нет сети")

    say = Speaker(busy_times=2)
    ag = Agents("k", "m", say, generate=generate)
    ag.start_delivery()
    ag.start("что-то", "Сбой")
    await settle(lambda: say.prompts)
    assert "не получилась" in say.prompts[0] and say.busy == 0
    await ag.stop()


async def test_limit_cancel_and_validation():
    async def generate(goal):
        await asyncio.sleep(10)

    ag = Agents("k", "m", Speaker(), generate=generate)
    assert ag.start("  ")["ok"] is False
    for i in range(3):
        assert ag.start(f"задача {i}", f"Задача {i}")["ok"]
    assert ag.start("четвёртая")["ok"] is False
    assert (await ag.call("cancel_task", {"title": "задача 1"}))["cancelled"] == 1
    assert ag.start("четвёртая")["ok"] is True
    assert (await ag.call("nope", {}))["ok"] is False
    await ag.stop()


async def test_brain_routes_agent_tools(tmp_path):
    ag = Agents("k", "m", Speaker(), generate=lambda g: asyncio.sleep(10))
    brain = Brain(tmp_path / "b.db", agents=ag)
    assert {t["name"] for t in TOOLS} <= {t["name"] for t in brain.tools()}

    async def send(msg):
        pass
    assert (await brain.call("start_task", {"goal": "найди новости"}, send))["ok"]
    assert "start_task" not in {t["name"] for t in Brain(tmp_path / "c.db").tools()}
    await ag.stop()


def test_generate_sync_parses_response(monkeypatch):
    import io
    import json

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    sent = {}

    def fake_urlopen(req, timeout):
        sent["body"] = json.loads(req.data)
        sent["key"] = req.headers["X-goog-api-key"]
        return Resp(json.dumps({"candidates": [{"content": {"parts": [{"text": "Привет, "}, {"text": "мир."}]}}]}).encode())

    monkeypatch.setattr(agents_mod.urllib.request, "urlopen", fake_urlopen)
    assert agents_mod.generate_sync("KEY", "m", "цель") == "Привет, мир."
    assert sent["key"] == "KEY" and sent["body"]["tools"] == [{"google_search": {}}]
