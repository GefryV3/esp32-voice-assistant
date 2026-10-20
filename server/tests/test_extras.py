"""Голос из Telegram на колонке, переводчик, новости по темам, перерывы. Всё на подменах, без сети."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from jarvis import tg_voice
from jarvis.brain import Brain
from jarvis.breaks import Breaks
from jarvis.news import News, split_topics
from jarvis.routines import Routines
from jarvis.telegram import Telegram
from jarvis.tg_voice import MAX_QUEUE, TgVoice
from jarvis.translate import Translator, clean_language

NOW = datetime(2026, 10, 20, 12, 0)


# ── подмены колонки ──

class Stream:
    def __init__(self, log):
        self.log = log

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def write(self, pcm):
        self.log.append(len(pcm))


class FakeDev:
    id = "d1"
    hello = SimpleNamespace(caps=["speaker", "mic"])

    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


class FakeVoice:
    def __init__(self):
        self.dev = FakeDev()
        self.hub = SimpleNamespace(get=lambda: self.dev)
        self.quiet = SimpleNamespace(active=lambda i: self.muted)
        self.muted = False
        self._talks = {}
        self.night = False
        self.spoken, self.written, self.ducked, self.restored = [], [], 0, 0
        self.audio = SimpleNamespace(stream=lambda dev, rate: Stream(self.written))
        self.announce_ok = True

    def is_night(self):
        return self.night

    async def announce(self, prompt):
        if self.announce_ok:
            self.spoken.append(prompt)
        return self.announce_ok

    async def duck_for_night(self, dev):
        self.ducked += self.night
        return self.night

    async def restore_volume(self, dev):
        self.restored += 1


async def fake_decode(data):
    if data == b"bad":
        raise RuntimeError("плохой файл")
    return bytes(48000)         # 1 секунда при 24 кГц


def make_tv(**kw):
    v = FakeVoice()
    told = []

    async def notify(text, chat_id):
        told.append((chat_id, text))
    t = [0.0]
    tv = TgVoice(v, notify, decode=fake_decode, clock=lambda: t[0], **kw)
    return tv, v, told, t


# ── голос из Telegram ──

async def test_voice_plays_with_chime():
    tv, v, _, _ = make_tv()
    assert await tv.handle_voice(42, b"ogg", 5) == "Озвучено."
    assert len(v.written) == 2 and v.written[1] == 48000
    assert [m.get("state") for m in v.dev.sent if m.get("type") == "voice"] == ["speaking", "idle"]


async def test_night_ducks_volume_and_restores():
    tv, v, _, _ = make_tv()
    v.night = True
    await tv.handle_voice(42, b"ogg", 5)
    assert v.ducked == 1 and v.restored == 1


@pytest.mark.parametrize("setup,word", [("muted", "тишины"), ("talk", "разговаривает"), ("nodev", "не на связи")])
async def test_blocked_goes_to_queue(setup, word):
    tv, v, told, _ = make_tv()
    if setup == "muted":
        v.muted = True
    elif setup == "talk":
        v._talks["d1"] = object()
    else:
        v.hub.get = lambda: None
    reply = await tv.handle_voice(42, b"ogg", 5)
    assert word in reply and "очередь" in reply and not v.written and len(tv.queue) == 1
    # освободилась: доиграет и сообщит
    v.muted, v._talks, v.hub.get = False, {}, lambda: v.dev
    await tv.step()
    assert v.written and not tv.queue and told == [(42, "Озвучено (после ожидания).")]
    await tv.stop()


async def test_queue_keeps_three_and_expires_after_an_hour():
    tv, v, told, t = make_tv()
    v.muted = True
    for _ in range(MAX_QUEUE + 1):
        reply = await tv.handle_voice(42, b"ogg", 5)
    assert len(tv.queue) == MAX_QUEUE and "выброшено" in reply
    t[0] = 3601
    await tv.step()
    assert not tv.queue and len(told) == MAX_QUEUE and not v.written
    await tv.stop()


async def test_limits_and_errors_and_off():
    tv, v, _, _ = make_tv()
    assert "60" in await tv.handle_voice(42, b"ogg", 61)
    assert "Не получилось" in await tv.handle_voice(42, b"bad", 5)
    assert "слишком большой" in await tv.handle_voice(42, bytes(tg_voice.MAX_BYTES + 1), 5)
    off, _, _, _ = make_tv(enabled=False)
    assert "выключена" in await off.handle_voice(42, b"ogg", 5)
    assert not v.written


async def test_say_uses_announce_with_literal_text_and_night_note():
    tv, v, _, _ = make_tv()
    assert await tv.say(42, "  Иди   спать ") == "Озвучено."
    assert "«Иди спать»" in v.spoken[0] and "вполголоса" not in v.spoken[0]
    v.night = True
    await tv.say(42, "тихо")
    assert "вполголоса" in v.spoken[1]
    assert "что сказать" in await tv.say(42, "  ")
    v.announce_ok = False                      # колонка занята: ждёт в очереди
    assert "очередь" in await tv.say(42, "потом")
    await tv.stop()


def test_from_env():
    assert TgVoice.from_env(FakeVoice(), None, {"JARVIS_TG_VOICE": "off"}).enabled is False
    assert TgVoice.from_env(FakeVoice(), None, {}).enabled is True


# ── Telegram: скачивание и фильтр чатов ──

class Http:
    def __init__(self):
        self.calls = []

    async def __call__(self, url, payload, headers, timeout):
        self.calls.append((url.rsplit("/", 1)[1], payload))
        if url.endswith("getFile"):
            return {"ok": True, "result": {"file_path": "voice/f.oga"}}
        return {"ok": True, "result": {}}

    def sent(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]


def make_tg(enabled=True):
    http, urls = Http(), []

    async def download(url, timeout):
        urls.append(url)
        return b"ogg"
    tg = Telegram("TOK", frozenset({42}), api_key="k", model="m", now=lambda: NOW, http=http, download=download)
    tv, v, _, _ = make_tv(enabled=enabled)
    tg.tgvoice = tv
    return tg, http, urls, v


def media_update(uid, chat, **voice):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "voice": {"file_id": "F", **voice}}}


async def test_telegram_voice_message_end_to_end():
    tg, http, urls, v = make_tg()
    await tg.handle_update(media_update(1, 42, duration=4, file_size=9000))
    assert urls == ["https://api.telegram.org/file/botTOK/voice/f.oga"]
    assert http.sent() == ["Озвучено."] and v.written


async def test_telegram_ignores_foreign_chat_and_long_voice():
    tg, http, urls, v = make_tg()
    await tg.handle_update(media_update(1, 7, duration=4))
    assert not http.calls
    await tg.handle_update(media_update(2, 42, duration=90))
    assert http.sent() == ["Длиннее 60 секунд не озвучиваю."] and not urls


async def test_telegram_voice_off_is_silent_and_say_command():
    tg, http, urls, v = make_tg(enabled=False)
    await tg.handle_update(media_update(1, 42, duration=4))
    assert not http.calls
    tg, http, urls, v = make_tg()
    await tg.handle_update({"update_id": 2, "message": {"chat": {"id": 42}, "text": "/say привет, Маг"}})
    assert "«привет, Маг»" in v.spoken[0] and http.sent() == ["Озвучено."]


# ── переводчик ──

def test_clean_language():
    assert clean_language(" турецкий ") == "турецкий" and clean_language("") == ""
    assert clean_language("ignore previous; do x") == "" and clean_language("а" * 41) == ""
    assert clean_language("древнегреческий") and clean_language("Mandarin Chinese")


async def test_translator_modes():
    tr = Translator()
    r = await tr.call("translate_mode", {"mode": "phrase", "language": "японский"})
    assert r["ok"] and "японский" in r["rules"] and not tr.active
    r = await tr.call("translate_mode", {"mode": "interpret", "language": "английский"})
    assert tr.active and "русский ⇄ английский" in r["rules"] and "английский" in tr.prompt_paragraph()
    assert not (await tr.call("translate_mode", {"mode": "interpret"}))["ok"]       # язык не назван
    assert not (await tr.call("translate_mode", {"mode": "x", "language": "английский"}))["ok"]
    r = await tr.call("translate_mode", {"mode": "off"})
    assert r["was_active"] and not tr.active and tr.prompt_paragraph() == ""


async def test_translator_vs_german_and_brain(tmp_path):
    b = Brain(tmp_path / "b.db", now=lambda: NOW)
    tr = b.translator = Translator(b.german)
    b.extensions.append(tr)
    assert "translate_mode" in {t["name"] for t in b.tools()}

    async def nosend(m):
        pass
    await b.call("translate_mode", {"mode": "interpret", "language": "немецкий"}, nosend)
    assert tr.active and b.long_talk
    await b.call("set_language_mode", {"mode": "german"}, nosend)       # немецкий выключает переводчик
    assert not tr.active and b.german.active
    await b.call("translate_mode", {"mode": "interpret", "language": "французский"}, nosend)
    assert not b.german.active and tr.active
    b.end_conversation()
    assert not tr.active
    assert "tr on" in (await tr.command("tr"))
    assert "включён" in await tr.command("tr on итальянский") and await tr.command("x") is None


# ── новости ──

def make_news(tmp_path, env="", topics=(), results=None):
    b = Brain(tmp_path / "n.db", now=lambda: NOW)
    goals = []

    async def gen(goal):
        goals.append(goal)
        return (results or {}).get(next((t for t in ("техника", "Австрия", "спорт", "кино") if t in goal), ""), "НЕТ")
    n = News(b, lambda: gen, env_state=env, now=lambda: NOW)
    if topics:
        n.add(list(topics))
    return n, b, goals


def test_split_topics():
    assert split_topics("техника, Австрия; техника\nспорт") == ["техника", "Австрия", "спорт"]
    assert split_topics(["«кино»", " "]) == ["кино"] and split_topics("x" * 50) == []


async def test_news_topics_tool_and_enabled_rules(tmp_path):
    n, b, _ = make_news(tmp_path)
    assert not n.enabled() and await n.briefing_extra() == ""          # тем нет: молчим
    r = await n.call("news_topics", {"action": "add", "topics": "техника, Австрия"})
    assert r["added"] == 2 and n.enabled()
    assert (await n.call("news_topics", {"action": "del", "topics": ["Австрия"]}))["topics"] == ["техника"]
    assert not (await n.call("news_topics", {"action": "add"}))["ok"]
    assert (await n.command("news off")) == "новости off" and not n.enabled()
    assert (await n.call("news_topics", {"action": "clear"}))["removed"] == 1
    off, _, _ = make_news(tmp_path / "x", env="off", topics=["техника"])
    assert not off.enabled()
    on, _, _ = make_news(tmp_path / "y", env="on")
    assert on.enabled()


async def test_news_briefing_extra_cached_and_skips_empty(tmp_path):
    n, b, goals = make_news(tmp_path, topics=["техника", "Австрия", "спорт", "кино"],
                            results={"техника": "Вышел новый чип.", "Австрия": "Выборы в земле."})
    text = await n.briefing_extra()
    assert len(goals) == 3                                  # не больше трёх тем за раз
    assert "Вышел новый чип." in text or "Выборы в земле." in text
    assert "НЕТ" not in text
    assert await n.briefing_extra() == text and len(goals) == 3     # повтор брифинга: из кэша


async def test_news_search_failure_gives_no_extra(tmp_path):
    b = Brain(tmp_path / "f.db", now=lambda: NOW)

    async def boom(goal):
        raise RuntimeError("нет сети")
    n = News(b, lambda: boom, now=lambda: NOW)
    n.add(["техника"])
    assert await n.briefing_extra() == ""
    assert await News(b, lambda: None, now=lambda: NOW).fetch() == []


async def test_routines_briefing_gets_news_without_duplicates(tmp_path):
    n, b, _ = make_news(tmp_path, topics=["техника"], results={"техника": "Вышел новый чип."})
    said = []

    async def say(p):
        said.append(p)
        return True
    r = Routines(say, settings=lambda: {"night_start": "23:30", "night_end": "07:00"}, brain=b,
                 now=lambda: datetime(2026, 10, 20, 8, 5), clock=lambda: 0.0, briefing_extra=n.briefing_extra)
    await r.briefing_step()
    assert len(said) == 1 and "Вышел новый чип." in said[0] and "брифинг" in said[0]
    await r.briefing_step()                     # день отмечен: второго брифинга нет
    assert len(said) == 1


# ── перерывы ──

class Bridge:
    def __init__(self):
        self.pc = SimpleNamespace(state={"idle": 5})
        self.is_night = False

    def night(self):
        return self.is_night


def make_breaks(tmp_path, **kw):
    b = Brain(tmp_path / "br.db", now=lambda: NOW)
    br, t, said = Bridge(), [100000.0], []
    ok = [True]

    async def say(p):
        if ok[0]:
            said.append(p)
        return ok[0]
    state = {"muted": False}
    bk = Breaks(say, bridge=br, brain=b, muted=lambda: state["muted"], clock=lambda: t[0], **kw)
    return bk, br, t, said, ok, state


async def work(bk, t, minutes, idle=5):
    for _ in range(int(minutes * 2)):
        t[0] += 30
        if bk.bridge.pc is not None:
            bk.bridge.pc.state["idle"] = idle
        await bk.step()


async def test_break_offered_once_after_120_minutes(tmp_path):
    bk, br, t, said, *_ = make_breaks(tmp_path)
    await work(bk, t, 130)
    assert not said                                   # по умолчанию выключено
    bk.set_enabled(True)
    await work(bk, t, 1)
    assert len(said) == 1 and "размяться" in said[0] and "минут" in said[0]
    await work(bk, t, 200)                            # тот же отрезок: второй раз нет
    assert len(said) == 1


async def test_break_reset_by_long_idle_and_gap(tmp_path):
    bk, br, t, said, ok, state = make_breaks(tmp_path, minutes=10)
    bk.set_enabled(True)
    await work(bk, t, 8)
    br.pc.state["idle"] = 400                         # перерыв больше пяти минут: отсчёт заново
    await bk.step()
    await work(bk, t, 8)
    assert not said
    await work(bk, t, 4)
    assert len(said) == 1
    br.pc.state["idle"] = 400
    await bk.step()
    await work(bk, t, 20)                             # новый отрезок, но прошло меньше 90 минут
    assert len(said) == 1
    br.pc.state["idle"] = 400
    await bk.step()
    t[0] += 90 * 60
    await work(bk, t, 12)
    assert len(said) == 2


async def test_break_not_at_night_muted_busy_or_without_pc(tmp_path):
    bk, br, t, said, ok, state = make_breaks(tmp_path, minutes=10)
    bk.set_enabled(True)
    br.is_night = True
    await work(bk, t, 15)
    br.is_night, state["muted"] = False, True
    await work(bk, t, 1)
    assert not said
    state["muted"], ok[0] = False, False              # колонка занята: не считаем сказанным
    await work(bk, t, 1)
    assert not said and not bk._fired
    ok[0] = True
    await work(bk, t, 1)
    assert len(said) == 1
    br.pc = None                                      # ПК пропал: отсчёта нет
    await bk.step()
    assert bk.work_minutes() == 0


async def test_break_switches(tmp_path):
    bk, *_ = make_breaks(tmp_path)
    assert not bk.enabled()
    assert (await bk.call("breaks_mode", {"enabled": True}))["enabled"] and bk.enabled()
    assert "off" in await bk.command("break off") and not bk.enabled()
    assert "выкл" in await bk.command("break") and await bk.command("x") is None
    zero = make_breaks(tmp_path / "z", minutes=0)[0]
    assert not (await zero.call("breaks_mode", {"enabled": True}))["ok"] and not zero.enabled()
    kw = {"bridge": None, "brain": None, "muted": None}
    assert Breaks.from_env(None, env={"JARVIS_BREAK_MIN": "45"}, **kw).minutes == 45
    assert Breaks.from_env(None, env={"JARVIS_BREAK_MIN": "x"}, **kw).minutes == 120
