from datetime import datetime

import pytest

from jarvis.brain import Brain
from jarvis.telegram import GEMINI, Telegram, parse_chat_ids, split_text

NOW = datetime(2026, 10, 19, 12, 0)


class FakeHttp:
    """Подмена HTTPS: Telegram отдаёт заготовленные обновления, Gemini — заготовленные ответы."""

    def __init__(self, updates=None, gemini=None):
        self.updates = updates or []
        self.gemini = list(gemini or [])
        self.calls = []

    async def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload, headers))
        if url.startswith("https://generativelanguage"):
            return self.gemini.pop(0)
        method = url.rsplit("/", 1)[1]
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return {"ok": True, "result": ups}
        return {"ok": True, "result": {}}

    def sent(self):
        return [(p["chat_id"], p["text"]) for u, p, _ in self.calls if u.endswith("/sendMessage")]


def text_reply(text):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}}]}


def update(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


@pytest.fixture
def brain(tmp_path):
    return Brain(tmp_path / "brain.db", now=lambda: NOW)


def make(http, brain=None, ids=frozenset({42})):
    return Telegram("TOKEN", ids, api_key="KEY", model="m", brain=brain, now=lambda: NOW, http=http)


def test_parse_chat_ids():
    assert parse_chat_ids("42, 7;x,") == {42, 7}
    assert parse_chat_ids("") == frozenset()


def test_split_text():
    assert split_text("a" * 10, 4) == ["aaaa", "aaaa", "aa"]
    assert split_text("   ") == []


async def test_allowed_chat_gets_answer(brain):
    http = FakeHttp([update(1, 42, "привет")], [text_reply("Здравствуй.")])
    tg = make(http, brain)
    await tg.poll_once()
    assert http.sent() == [(42, "Здравствуй.")]
    url, payload, headers = next(c for c in http.calls if c[0].startswith("https://generativelanguage"))
    assert url == GEMINI.format(model="m") and headers == {"x-goog-api-key": "KEY"}
    assert payload["contents"][-1] == {"role": "user", "parts": [{"text": "привет"}]}
    assert "Telegram" in payload["systemInstruction"]["parts"][0]["text"]
    assert any(t["name"] == "remember" for t in payload["tools"][0]["functionDeclarations"])
    assert tg._offset == 2


async def test_foreign_chat_ignored_completely(brain):
    http = FakeHttp([update(1, 999, "эй")], [])
    tg = make(http, brain)
    await tg.poll_once()
    assert http.sent() == []
    assert not any(c[0].startswith("https://generativelanguage") for c in http.calls)
    assert tg._offset == 2   # но обновление считается прочитанным


async def test_tool_call_runs_and_result_goes_back(brain):
    call = {"candidates": [{"content": {"role": "model", "parts": [
        {"functionCall": {"name": "remember", "args": {"fact": "любит кофе"}}}]}}]}
    http = FakeHttp([update(1, 42, "запомни, что я люблю кофе")], [call, text_reply("Запомнил.")])
    tg = make(http, brain)
    await tg.poll_once()
    assert brain.facts() == ["любит кофе"]
    assert http.sent() == [(42, "Запомнил.")]
    second = [c for c in http.calls if c[0].startswith("https://generativelanguage")][1][1]
    resp = second["contents"][-1]["parts"][0]["functionResponse"]
    assert resp == {"name": "remember", "response": {"ok": True, "new": True}}


async def test_gemini_failure_reported_not_raised(brain):
    class Broken(FakeHttp):
        async def __call__(self, url, payload, headers, timeout):
            if url.startswith("https://generativelanguage"):
                raise RuntimeError("HTTP 500")
            return await super().__call__(url, payload, headers, timeout)

    http = Broken([update(1, 42, "привет")])
    await make(http, brain).poll_once()
    assert len(http.sent()) == 1 and http.sent()[0][0] == 42


async def test_send_goes_to_all_allowed_chats():
    http = FakeHttp()
    tg = make(http, ids=frozenset({42, 7}))
    assert await tg.send("⏰ позвонить маме") is True
    assert sorted(http.sent()) == [(7, "⏰ позвонить маме"), (42, "⏰ позвонить маме")]


async def test_send_failure_returns_false():
    async def down(url, payload, headers, timeout):
        return {"ok": False, "description": "Forbidden"}

    assert await make(down).send("x") is False


async def test_console_command(brain):
    http = FakeHttp(gemini=[text_reply("Тест пройден.")])
    tg = make(http, brain)
    assert await tg.command("ask что-то") is None
    assert await tg.command("tg привет") == "Тест пройден."
    assert http.sent() == []

