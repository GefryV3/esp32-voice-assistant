from datetime import datetime, timedelta

import pytest

from jarvis.brain import Brain
from jarvis.games import QUIZ_ROUNDS, last_letter
from jarvis.german import INTERVAL_DAYS, RETRY_MIN, rules
from jarvis.voice import system_prompt

NOW = datetime(2026, 10, 20, 9, 0)


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


# ── слова и интервальное повторение ──

def test_save_word_dedupes_case_insensitive(brain):
    g = brain.german
    assert g.save_word("der Bahnhof", "вокзал")
    assert not g.save_word("DER bahnhof", "вокзал (ж/д)")
    assert len(g.words()) == 1 and g.words()[0]["translation"] == "вокзал (ж/д)"
    assert g.save_word("Äpfel", "яблоки") and not g.save_word("äpfel")


def test_new_word_is_due_immediately(brain):
    brain.german.save_word("die Schule", "школа")
    assert [w["word"] for w in brain.german.due_words()] == ["die Schule"]


def test_review_correct_climbs_boxes_and_postpones(brain):
    g = brain.german
    g.save_word("die Schule", "школа")
    days = []
    for _ in range(len(INTERVAL_DAYS) + 1):
        res = g.review("die schule", True)
        days.append(res["days"])
    assert days == [1, 3, 7, 14, 30, 30, 30]
    assert g.due_words() == []
    brain.clock["now"] = NOW + timedelta(days=31)
    assert len(g.due_words()) == 1


def test_review_wrong_resets_box_and_retries_soon(brain):
    g = brain.german
    g.save_word("der Zug", "поезд")
    g.review("der Zug", True)
    g.review("der Zug", True)
    res = g.review("der Zug", False)
    assert res["box"] == 0
    assert g.words()[0]["misses"] == 1
    assert g.due_words() == []
    brain.clock["now"] = NOW + timedelta(minutes=RETRY_MIN + 1)
    assert [w["word"] for w in g.due_words()] == ["der Zug"]


def test_confused_words_come_first(brain):
    g = brain.german
    for w in ("a", "b", "c"):
        g.save_word(w, w)
    g.review("c", False)
    g.review("c", False)
    g.review("b", False)
    brain.clock["now"] = NOW + timedelta(minutes=30)
    assert [w["word"] for w in g.due_words(3)] == ["c", "b", "a"]


def test_review_unknown_word_creates_it(brain):
    assert brain.german.review("das Fenster", False)["box"] == 0
    assert brain.german.words()[0]["misses"] == 1
    assert brain.german.review("", True) is None


def test_words_survive_restart(tmp_path):
    b = Brain(tmp_path / "x.db", now=lambda: NOW)
    b.german.save_word("der Hund", "собака")
    b.db.close()
    b2 = Brain(tmp_path / "x.db", now=lambda: NOW)
    assert b2.german.words()[0]["word"] == "der Hund"


def test_word_of_day_once_per_day(brain):
    g = brain.german
    assert not g.word_of_day_given()
    g.save_word("die Aufgabe", "задание", word_of_day=True)
    assert g.word_of_day_given()
    brain.clock["now"] = NOW + timedelta(days=1)
    assert not g.word_of_day_given()


# ── режим ──

async def test_mode_on_returns_rules_and_off_resets(brain, sent):
    brain.german.save_word("der Zug", "поезд")
    out = await brain.call("set_language_mode", {"mode": "german", "situation": "магазин"}, sent)
    assert out["ok"] and brain.german.active and brain.long_talk
    assert "ПО-НЕМЕЦКИ" in out["rules"] and "магазин" in out["rules"]
    assert "der Zug" in out["rules"] and "Слово дня" in out["rules"] or "«Слово дня»" in out["rules"]
    await brain.call("de_word_review", {"word": "der Zug", "correct": True}, sent)
    off = await brain.call("set_language_mode", {"mode": "off"}, sent)
    assert off["mode"] == "russian" and off["right"] == 1 and not brain.german.active


async def test_level_default_and_custom(brain, sent):
    assert brain.german.level == "A2-B1"
    out = await brain.call("set_language_mode", {"mode": "german", "level": "b1"}, sent)
    assert out["level"] == "B1" and "B1" in out["rules"]
    bad = await brain.call("set_language_mode", {"mode": "german", "level": "C9"}, sent)
    assert bad["ok"] is False
    assert (await brain.call("set_language_mode", {"mode": "klingon"}, sent))["ok"] is False


async def test_end_conversation_resets_mode_and_game(brain, sent):
    await brain.call("set_language_mode", {"mode": "german"}, sent)
    await brain.call("game_start", {"game": "города"}, sent)
    assert brain.long_talk
    brain.end_conversation()
    assert not brain.german.active and not brain.games.active and not brain.long_talk


def test_rules_mention_google_task_only_with_google():
    assert "google_tasks_done" in rules(google=True)
    assert "google_tasks_done" not in rules(google=False)
    assert "Слово дня" not in rules(word_of_day_given=True).replace("«Слово дня»", "")


async def test_tools_registered(brain):
    names = {t["name"] for t in brain.tools()}
    assert {"set_language_mode", "de_word_save", "de_word_review", "de_words_due",
            "game_start", "game_move", "game_answer", "game_stop", "game_scores"} <= names


# ── подсказка ──

def test_prompt_default_is_russian_only():
    p = system_prompt(NOW, tools=True)
    assert "Говори только по-русски" in p
    assert "set_language_mode" in p
    assert "set_language_mode" not in system_prompt(NOW)


def test_prompt_in_german_mode_swaps_language_rule(brain):
    brain.german.start()
    p = system_prompt(NOW, tools=True, language=brain.german.prompt_paragraph())
    assert "Говори только по-русски" not in p
    assert "ПО-НЕМЕЦКИ" in p and "Объяснения правил" in p
    assert "Тебя зовут Мага" in p


async def test_voice_prompt_picks_up_active_mode(brain, tmp_path):
    from types import SimpleNamespace

    from jarvis.config import Settings
    from jarvis.voice import Voice
    hub = SimpleNamespace(bridge=None, on_connect=lambda f: None, on_audio=lambda f: None,
                          on_message=lambda f: None)
    prompts = []
    v = Voice(hub, None, Settings(device_token="t"), brain=brain, now=lambda: NOW,
              live_factory=lambda system: prompts.append(system))
    v._new_live()
    assert "Говори только по-русски" in prompts[-1]
    brain.german.start()
    v._new_live()
    assert "ПО-НЕМЕЦКИ" in prompts[-1] and "Говори только по-русски" not in prompts[-1]


# ── консоль ──

async def test_console_commands(brain):
    assert "добавлено" in await brain.command("de add der Bahnhof = вокзал")
    assert "der Bahnhof" in await brain.command("de words")
    assert "ящик 1" not in await brain.command("de words")
    assert "der Bahnhof" in await brain.command("de due")
    await brain.command("de miss der Bahnhof")
    assert "ошибок 1" in await brain.command("de words")
    assert "включён" in await brain.command("de on B1")
    assert brain.german.active and brain.german.level == "B1"
    assert "выключен" in await brain.command("de off")
    assert await brain.command("ask hi") is None


# ── игры ──

def test_last_letter_skips_soft_sign():
    assert last_letter("Москва") == "а"
    assert last_letter("Орёл") == "л"
    assert last_letter("Нью-Йорк") == "к"
    assert last_letter("Йошкар-Ола") == "а"
    assert last_letter("Тверь") == "р"


async def test_cities_rules_and_repeat(brain, sent):
    start = await brain.call("game_start", {"game": "Города"}, sent)
    assert start["ok"] and sent.out[-1]["type"] == "caption"
    assert (await brain.call("game_move", {"word": "Москва", "by": "user"}, sent))["next_letter"] == "А"
    wrong = await brain.call("game_move", {"word": "Берлин", "by": "maga"}, sent)
    assert wrong["ok"] is False and wrong["need"] == "А"
    assert (await brain.call("game_move", {"word": "Астана", "by": "maga"}, sent))["ok"]
    assert (await brain.call("game_move", {"word": "москва", "by": "user"}, sent))["ok"] is False
    assert "уже" in (await brain.call("game_move", {"word": "Астана", "by": "user"}, sent))["error"]
    assert (await brain.call("game_move", {"word": "Анапа", "by": "user"}, sent))["count"] == 3
    assert sent.out[-1] == {"type": "caption", "kind": "reply", "text": "Города: Анапа → А", "icon": "none"}


async def test_twenty_questions_counter(brain, sent):
    await brain.call("game_start", {"game": "20 вопросов"}, sent)
    for i in range(19):
        res = await brain.call("game_move", {"word": f"вопрос {i}", "by": "user"}, sent)
    assert res["left"] == 1
    res = await brain.call("game_move", {"word": "последний", "by": "user"}, sent)
    assert res["left"] == 0 and "note" in res


async def test_scores_persist_and_accumulate(brain, sent, tmp_path):
    await brain.call("game_start", {"game": "cities"}, sent)
    out = await brain.call("game_stop", {"winner": "user"}, sent)
    assert out["recorded"] and out["score"]["user"] == 1
    assert sent.out[-1]["type"] == "emotion" and sent.out[-1]["name"] == "sorry"
    await brain.call("game_start", {"game": "cities"}, sent)
    out = await brain.call("game_stop", {"winner": "maga"}, sent)
    assert (out["score"]["user"], out["score"]["maga"]) == (1, 1)
    await brain.call("game_start", {"game": "cities"}, sent)
    assert (await brain.call("game_stop", {"winner": "none"}, sent))["recorded"] is False
    scores = (await brain.call("game_scores", {}, sent))["scores"]
    assert scores[0] == {"game": "Города", "user": 1, "maga": 1, "draws": 0}
    b2 = Brain(tmp_path / "b.db", now=lambda: NOW)
    assert b2.games.score("cities")["maga"] == 1


async def test_quiz_by_points_and_german_words(brain, sent):
    brain.german.save_word("der Zug", "поезд")
    brain.german.save_word("die Schule", "школа")
    start = await brain.call("game_start", {"game": "quiz", "source": "de"}, sent)
    assert {w["word"] for w in start["words"]} == {"der Zug", "die Schule"}
    assert "der Zug" in start["rules"]
    await brain.call("game_answer", {"correct": True, "word": "der Zug"}, sent)
    r = await brain.call("game_answer", {"correct": False, "word": "die Schule"}, sent)
    await brain.call("game_answer", {"correct": True}, sent)
    assert r["points"] == {"user": 1, "maga": 1}
    assert sent.out[-2]["text"] == "Викторина 2:1"
    words = {w["word"]: w for w in brain.german.words()}
    assert words["die Schule"]["misses"] == 1 and words["der Zug"]["hits"] == 1
    out = await brain.call("game_stop", {}, sent)
    assert out["winner"] == "user" and out["score"]["game"] == "Викторина"
    assert QUIZ_ROUNDS == 5


async def test_game_errors(brain, sent):
    assert (await brain.call("game_move", {"word": "x"}, sent))["ok"] is False
    assert (await brain.call("game_start", {"game": "шахматы"}, sent))["ok"] is False
    assert "ты 0" in await brain.command("game score")
    await brain.command("game win города")
    assert "ты 1" in await brain.command("game score")
