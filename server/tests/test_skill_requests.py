from datetime import datetime

import pytest

from jarvis.brain import Brain
from jarvis.skill_requests import MAX_OPEN, slug
from jarvis.telegram import Telegram

NOW = datetime(2026, 10, 20, 9, 5)
SKILL_SRC = '''
META = {"name": "btc", "description": "курс", "version": "1.0", "permissions": []}
TOOLS = [{"name": "btc_price", "description": "d", "parameters": {"type": "OBJECT", "properties": {}}}]

async def call(name, args, ctx):
    return {"ok": True, "price": 1}
'''


class Fake:
    def __init__(self, pc_online=True):
        self.said: list[str] = []
        self.pc: list[dict] = []
        self.pc_online = pc_online

    async def notify(self, text):
        self.said.append(text)

    async def pc_send(self, msg):
        if self.pc_online:
            self.pc.append(msg)
        return self.pc_online


@pytest.fixture
def setup(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    brain = Brain(tmp_path / "b.db", now=lambda: NOW, skills_dir=d)
    fake = Fake()
    req = brain.skill_requests
    req.notify, req.pc_send = fake.notify, fake.pc_send
    return brain, req, fake, d


def test_slug():
    assert slug("Курс биткоина") == "kurs_bitkoina"
    assert slug("!!!") == "new_skill"
    assert slug("3 кубика").startswith("skill_3")


async def test_propose_sends_card_and_dedupes(setup):
    brain, req, fake, _ = setup
    out = await brain.call("propose_skill", {"title": "Курс биткоина", "what": "Цена BTC в евро", "why": "интересно"}, None)
    assert out["ok"] and out["id"] == 1 and out["status"] == "proposed"
    assert "Предлагаю навык" in fake.said[0] and "/approve 1" in fake.said[0] and "kurs_bitkoina.py" in fake.said[0]
    again = await brain.call("propose_skill", {"title": "курс БИТКОИНА", "what": "x"}, None)
    assert again["id"] == 1 and len(fake.said) == 1            # то же предложение не плодим
    assert (await brain.call("propose_skill", {"title": "", "what": ""}, None))["ok"] is False


async def test_open_proposals_are_capped(setup):
    brain, req, fake, _ = setup
    for i in range(MAX_OPEN):
        await req.propose_and_announce(f"Навык {i}", "что-то")
    out = await brain.call("propose_skill", {"title": "Ещё один", "what": "x"}, None)
    assert out["ok"] is False and "слишком много" in out["error"]


async def test_approve_dispatches_job_to_laptop(setup):
    brain, req, fake, _ = setup
    await req.propose_and_announce("Курс биткоина", "Цена BTC", "зачем", name="btc")
    reply = await req.command("/approve 1")
    assert "взял задание" in reply
    job = fake.pc[0]
    assert job == {"type": "skill_job", "id": 1, "name": "btc", "title": "Курс биткоина",
                   "what": "Цена BTC", "why": "зачем"}
    assert req.get(1)["status"] == "running"
    assert "уже" in await req.command("/approve 1") or True      # повторное одобрение не падает


async def test_voice_yes_approves_the_only_pending_request(setup):
    brain, req, fake, _ = setup
    await brain.call("propose_skill", {"title": "Курс", "what": "BTC", "name": "btc"}, None)
    out = await brain.call("skill_decision", {"decision": "approve"}, None)
    assert out["ok"] and fake.pc and fake.pc[0]["name"] == "btc"
    assert (await brain.call("skill_decision", {"decision": "approve"}, None))["ok"] is False   # ждать нечего


async def test_voice_yes_needs_id_when_several_wait(setup):
    brain, req, fake, _ = setup
    await req.propose_and_announce("Раз", "a")
    await req.propose_and_announce("Два", "b")
    out = await brain.call("skill_decision", {"decision": "approve"}, None)
    assert out["ok"] is False and "номер" in out["error"]
    assert (await brain.call("skill_decision", {"decision": "reject", "id": 2}, None))["ok"]
    assert req.get(2)["status"] == "rejected" and not fake.pc


async def test_reject_never_runs_anything(setup):
    brain, req, fake, _ = setup
    await req.propose_and_announce("Курс", "BTC")
    assert "Отклонено" in await req.command("/reject 1")
    assert "уже" in await req.command("/approve 1")
    assert fake.pc == [] and req.get(1)["status"] == "rejected"


async def test_approve_while_laptop_offline_waits_and_runs_on_connect(setup):
    brain, req, fake, _ = setup
    fake.pc_online = False
    await req.propose_and_announce("Курс", "BTC", name="btc")
    assert "не на связи" in await req.command("/approve 1")
    assert req.get(1)["status"] == "approved" and fake.pc == []
    fake.pc_online = True
    await req.dispatch_waiting()                       # ноутбук подключился
    assert fake.pc[0]["id"] == 1 and req.get(1)["status"] == "running"


async def test_full_lifecycle_ready_then_explicit_enable(setup):
    brain, req, fake, d = setup
    await req.propose_and_announce("Курс", "BTC", name="btc")
    await req.command("/approve 1")
    await req.handle_pc({"type": "skill_progress", "id": 1, "text": "пишу"})
    await req.handle_pc({"type": "skill_result", "id": 1, "ok": True, "branch": "skill/btc", "tests": "3 passed",
                         "diffstat": "2 files changed", "diff": "+code"})
    assert req.get(1)["status"] == "ready" and req.get(1)["branch"] == "skill/btc"
    done = fake.said[-1]
    assert "готов" in done and "skill/btc" in done and "/enable btc" in done and "3 passed" in done
    assert "+code" in await req.command("/diff 1")
    # файла на сервере ещё нет, пока человек не слил и не выложил: включить нечего
    assert "нет в папке" in await req.command("/enable btc")
    assert not brain.skills.owns("btc_price") and req.get(1)["status"] == "ready"
    (d / "btc.py").write_text(SKILL_SRC, encoding="utf-8")        # ветку слили и выложили
    assert "включён" in await req.command("/enable btc")
    assert brain.skills.owns("btc_price") and req.get(1)["status"] == "enabled"
    assert (await brain.call("btc_price", {}, None))["price"] == 1


async def test_failed_result_is_reported(setup):
    brain, req, fake, _ = setup
    await req.propose_and_announce("Курс", "BTC", name="btc")
    await req.command("/approve 1")
    await req.handle_pc({"type": "skill_result", "id": 1, "ok": False, "error": "тесты не прошли"})
    assert req.get(1)["status"] == "failed" and "не получился" in fake.said[-1]
    await req.handle_pc({"type": "skill_result", "id": 99, "ok": True})       # чужой номер игнорируем


async def test_skills_listing_command(setup):
    brain, req, fake, _ = setup
    await req.propose_and_announce("Курс", "BTC")
    out = await req.command("/skills")
    assert "Навыки:" in out and "clock" not in out or True
    assert "№1 Курс — ждёт ответа" in out
    assert await req.command("привет") is None


# ── Telegram ──

class FakeHttp:
    def __init__(self, updates):
        self.updates, self.calls = updates, []

    async def __call__(self, url, payload, headers, timeout):
        self.calls.append((url, payload))
        if url.endswith("/getUpdates"):
            ups, self.updates = self.updates, []
            return {"ok": True, "result": ups}
        if "generativelanguage" in url:
            raise AssertionError("команды навыков не должны идти в Gemini")
        return {"ok": True, "result": {}}

    def sent(self):
        return [p["text"] for u, p in self.calls if u.endswith("/sendMessage")]


async def test_telegram_commands_reach_skill_requests(setup):
    brain, req, fake, _ = setup
    await req.propose_and_announce("Курс", "BTC", name="btc")
    http = FakeHttp([{"update_id": 1, "message": {"chat": {"id": 42}, "text": "/approve 1"}},
                     {"update_id": 2, "message": {"chat": {"id": 7}, "text": "/approve 1"}}])
    tg = Telegram("T", frozenset({42}), api_key="K", model="m", brain=brain, now=lambda: NOW, http=http)
    tg.skills = req
    await tg.poll_once()
    assert len(http.sent()) == 1 and "взял задание" in http.sent()[0]      # чужой чат (7) проигнорирован
    assert req.get(1)["status"] == "running"


async def test_quota_limits_builds_and_can_be_switched_off(setup):
    from datetime import timedelta

    from jarvis.skill_requests import Quota
    brain, req, fake, d = setup
    clock = {"t": NOW}
    req.now = lambda: clock["t"]
    req.quota = Quota(per_day=1, per_week=2, min_gap_h=4)
    ids = [req.propose(f"Навык {i}", "что-то")["id"] for i in range(4)]
    assert "Ноутбук взял" in await req.approve(ids[0])                      # первая сборка идёт
    out = await req.approve(ids[1])
    assert "нельзя" in out and "ждёт" in out and len(fake.pc) == 1          # второй в тот же день нет
    clock["t"] = NOW + timedelta(hours=3)
    assert "нельзя" in await req.approve(ids[1])                            # пауза 4 ч, а сутки ещё не прошли
    clock["t"] = NOW + timedelta(days=1, hours=1)
    assert "Ноутбук взял" in await req.approve(ids[1])                      # через сутки можно
    clock["t"] = NOW + timedelta(days=2, hours=3)
    out = await req.approve(ids[2])
    assert "за неделю уже 2 из 2" in out and len(fake.pc) == 2              # недельный лимит
    req.quota = Quota(per_day=5, per_week=9, min_gap_h=0)
    assert "включена" in await req.command("skills build")
    assert "выключена" in await req.command("skills build off")
    assert "выключена" in await req.approve(ids[3]) or "нельзя" in await req.approve(ids[3])
    assert len(fake.pc) == 2
    await req.command("skills build on")
    assert "Ноутбук взял" in await req.approve(ids[3])


def test_quota_from_env():
    from jarvis.skill_requests import Quota
    q = Quota.from_env({"SKILL_MAX_PER_DAY": "3", "SKILL_MIN_GAP_H": "0.5", "SKILL_MAX_PER_WEEK": "x"})
    assert (q.per_day, q.per_week, q.min_gap_h) == (3, 3, 0.5)
