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


def test_research_uses_web_results_and_reports_when_empty():
    asked = []

    def ask(key, model, prompt, system=""):
        asked.append(prompt)
        return "1. погода вена\n2. wien wetter" if len(asked) == 1 else "Сегодня в Вене около десяти градусов."

    def search(q):
        return [("Погода", "https://example.at/wetter", "10 °C, облачно")]
    out = agents_mod.research_sync("k", "m", "погода в Вене", search=search, ask=ask)
    assert out == "Сегодня в Вене около десяти градусов."
    assert "10 °C, облачно" in asked[1] and "example.at" in asked[1] and "Дай 1-2" in asked[0]
    with pytest.raises(RuntimeError):
        agents_mod.research_sync("k", "m", "x", search=lambda q: [], ask=lambda *a, **k: "запрос")


def test_web_search_parses_duckduckgo_html(monkeypatch):
    import io
    page = ('<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.at%2Fx">'
            'Заголовок <b>1</b></a><a class="result__snippet" href="#">Отрывок &amp; текст</a>').encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    monkeypatch.setattr(agents_mod.urllib.request, "urlopen", lambda req, timeout: Resp(page))
    assert agents_mod.web_search("q") == [("Заголовок 1", "https://a.at/x", "Отрывок & текст")]


async def test_failure_prompt_carries_reason():
    async def generate(goal):
        raise RuntimeError("HTTP Error 429: Too Many Requests")
    ag = Agents("k", "m", Speaker(), generate=generate)
    ag.start("x", "Поиск")
    await settle(lambda: ag.tasks[0].status == "failed")
    assert "квота" in ag.prompt_for(ag.tasks[0])
    await ag.stop()


def test_query_cleanup_and_providers(monkeypatch):
    asked = []

    def ask(key, model, prompt, system=""):
        asked.append(prompt)
        return "Вот 2 запроса:\n1. **главные новости мира**\n2. «world news today»" if len(asked) == 1 else "Готово."
    seen = []

    def search(q):
        seen.append(q)
        return [("T", "https://x.at/", "s")]
    agents_mod.research_sync("k", "m", "новости", search=search, ask=ask)
    assert seen == ["главные новости мира", "world news today"]
    assert agents_mod.make_search("brave", "KEY") is not agents_mod.web_search
    assert agents_mod.make_search("brave", "") is agents_mod.web_search       # нет ключа — запасной
    assert agents_mod.make_search("searxng", "", "http://h:8080") is not agents_mod.web_search


def test_brave_and_searxng_parse(monkeypatch):
    import io
    import json

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    seen = []

    def fake(req, timeout):
        seen.append(req.full_url)
        if "brave" in req.full_url:
            assert req.headers["X-subscription-token"] == "KEY"
            return Resp(json.dumps({"web": {"results": [{"title": "A <b>1</b>", "url": "https://a", "description": "d"}]}}).encode())
        return Resp(json.dumps({"results": [{"title": "B", "url": "https://b", "content": "c"}]}).encode())
    monkeypatch.setattr(agents_mod.urllib.request, "urlopen", fake)
    assert agents_mod.brave_search("KEY", "q") == [("A 1", "https://a", "d")]
    assert agents_mod.searxng_search("http://h:8080/", "q") == [("B", "https://b", "c")]
    assert "format=json" in seen[1] and seen[1].startswith("http://h:8080/search?")


def test_tavily_and_serper_parse(monkeypatch):
    import io
    import json

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    sent = []

    def fake(req, timeout):
        sent.append((req.full_url, json.loads(req.data), dict(req.headers)))
        if "tavily" in req.full_url:
            return Resp(json.dumps({"results": [{"title": "T", "url": "https://t", "content": "c" * 500}]}).encode())
        return Resp(json.dumps({"organic": [{"title": "S", "link": "https://s", "snippet": "sn"}]}).encode())
    monkeypatch.setattr(agents_mod.urllib.request, "urlopen", fake)
    t = agents_mod.tavily_search("TK", "q")
    assert t[0][:2] == ("T", "https://t") and len(t[0][2]) == 400
    assert sent[0][2]["Authorization"] == "Bearer TK" and sent[0][1]["query"] == "q"
    assert agents_mod.serper_search("SK", "q") == [("S", "https://s", "sn")]
    assert sent[1][2]["X-api-key"] == "SK"
    assert agents_mod.make_search("tavily", tavily_key="K") is not agents_mod.web_search
    assert agents_mod.make_search("serper", serper_key="") is agents_mod.web_search
