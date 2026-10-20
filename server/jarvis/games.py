"""Голосовые игры: «Города», «20 вопросов», викторина. Счёт хранится в brain.db.

Игру ведёт сама модель (она придумывает города, загадывает предмет, задаёт вопросы). Сервер делает
то, чему модель не доверяем: помнит названные слова (повтор и «не на ту букву»), считает вопросы и
очки, хранит счёт между вечерами. Состояние выводим существующими сообщениями протокола: подпись
(caption reply) и эмоция, новых экранных типов нет.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from . import protocol
from .german import German

SCHEMA = """
CREATE TABLE IF NOT EXISTS game_scores (
    game TEXT PRIMARY KEY, user INTEGER NOT NULL DEFAULT 0, maga INTEGER NOT NULL DEFAULT 0,
    draws INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL DEFAULT '');
"""

GAMES = {"cities": "Города", "twenty": "20 вопросов", "quiz": "Викторина"}
ALIASES = {"города": "cities", "city": "cities", "cities": "cities", "20": "twenty", "twenty": "twenty",
           "20 вопросов": "twenty", "викторина": "quiz", "quiz": "quiz"}
SKIP_LETTERS = "ьъыйё"      # на что не бывает названий: берём предыдущую букву
MAX_QUESTIONS = 20
QUIZ_ROUNDS = 5

TOOLS: list[dict[str, Any]] = [
    {"name": "game_start", "description": "Начать голосовую игру. game: cities («Города»: по очереди называем "
     "города, каждый на последнюю букву предыдущего), twenty («20 вопросов»: ты загадываешь предмет, "
     "пользователь за 20 вопросов угадывает, да/нет; или наоборот), quiz (викторина, "
     f"{QUIZ_ROUNDS} вопросов). source=de — викторина по немецким словам пользователя (в ответе придут "
     "слова). Верни rules и следуй им.",
     "parameters": {"type": "OBJECT", "properties": {"game": {"type": "STRING"}, "source": {"type": "STRING"}},
                    "required": ["game"]}},
    {"name": "game_move", "description": "Записать ход: в «Городах» word — название города, by — user или maga "
     "(СВОЙ город тоже записывай); в «20 вопросов» word — вопрос пользователя, by=user. Сервер проверит "
     "повтор и нужную букву, посчитает вопросы. Если ok=false — объясни причину и попроси другой ход "
     "(свой ход придумай заново).",
     "parameters": {"type": "OBJECT", "properties": {"word": {"type": "STRING"}, "by": {"type": "STRING"}},
                    "required": ["word"]}},
    {"name": "game_answer", "description": "Викторина: итог вопроса. correct=true — пользователь ответил верно "
     "(очко ему), false — нет (очко тебе). Для вопроса по немецкому слову передай word: сервер учтёт "
     "это в повторении.",
     "parameters": {"type": "OBJECT", "properties": {"correct": {"type": "BOOLEAN"}, "word": {"type": "STRING"}},
                    "required": ["correct"]}},
    {"name": "game_stop", "description": "Игра закончена или пользователь сдался/передумал. winner: user, maga, "
     "draw или none (не записывать, если бросили). В викторине без winner победитель по очкам. "
     "В ответе общий счёт: озвучь его одной фразой.",
     "parameters": {"type": "OBJECT", "properties": {"winner": {"type": "STRING"}}}},
    {"name": "game_scores", "description": "Общий счёт по играм («какой счёт?»).",
     "parameters": {"type": "OBJECT", "properties": {}}},
]


def fold(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def last_letter(city: str) -> str:
    for ch in reversed(fold(city)):
        if ch.isalpha() and ch not in SKIP_LETTERS.replace("ё", ""):
            return ch
    return ""


def game_rules(game: str) -> str:
    if game == "cities":
        return ("Игра «Города». Первым ходит пользователь, если не сказал иначе. Следующий город начинается "
                "на последнюю букву предыдущего (ь, ъ, ы пропускай, бери предпоследнюю). Города не "
                "повторяются. Каждый ход (и пользователя, и свой) записывай game_move. Если game_move вернул "
                "ok=false у пользователя, скажи причину, дай ему ещё попытку; свой ход придумывай сам. Не "
                "можешь назвать город — признай поражение и вызови game_stop(winner=user). Если пользователь "
                "не может — game_stop(winner=maga). Говори коротко: город и подколка.")
    if game == "twenty":
        return ("Игра «20 вопросов». По умолчанию ты загадываешь предмет или животное (держи в голове, "
                "не называй), а пользователь задаёт вопросы, на которые отвечаешь только «да», «нет» или "
                "«не знаю». Каждый вопрос записывай game_move(by=user). Когда сервер сказал, что вопросов "
                "не осталось, или пользователь назвал догадку: верно — game_stop(winner=user), нет и "
                "вопросов не осталось — game_stop(winner=maga) и назови, что загадал. Если пользователь "
                "просит наоборот, он загадывает, а ты задаёшь до 20 вопросов (игра идёт без game_move).")
    return ("Викторина, пять вопросов по одному: задай вопрос, выслушай ответ, оцени и вызови "
            "game_answer(correct). После последнего вопроса вызови game_stop без winner и озвучь счёт. "
            "Вопросы живые и разной сложности, не заумные.")


class Games:
    def __init__(self, db: sqlite3.Connection, now: Callable[[], datetime], german: German):
        self.db = db
        self.now = now
        self.german = german
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.name = ""
        self.used: list[str] = []
        self.questions = 0
        self.points = {"user": 0, "maga": 0}
        self.rounds = 0

    names = {t["name"] for t in TOOLS}

    @property
    def active(self) -> bool:
        return bool(self.name)

    def reset(self) -> None:
        self.name = ""
        self.used = []
        self.questions = 0
        self.points = {"user": 0, "maga": 0}
        self.rounds = 0

    # ── счёт ──

    def record(self, game: str, winner: str) -> dict:
        col = {"user": "user", "maga": "maga", "draw": "draws"}[winner]
        self.db.execute("INSERT INTO game_scores (game, updated) VALUES (?, ?) ON CONFLICT(game) DO NOTHING",
                        (game, self.now().isoformat(timespec="seconds")))
        self.db.execute(f"UPDATE game_scores SET {col} = {col} + 1, updated = ? WHERE game = ?",
                        (self.now().isoformat(timespec="seconds"), game))
        self.db.commit()
        return self.score(game)

    def score(self, game: str) -> dict:
        row = self.db.execute("SELECT user, maga, draws FROM game_scores WHERE game = ?", (game,)).fetchone()
        u, m, d = row or (0, 0, 0)
        return {"game": GAMES.get(game, game), "user": u, "maga": m, "draws": d}

    def all_scores(self) -> list[dict]:
        return [self.score(g) for g in GAMES]

    # ── ходы ──

    def move(self, word: str, by: str) -> dict:
        if self.name == "cities":
            city = " ".join(word.split())
            if not city:
                return {"ok": False, "error": "пустое название"}
            if fold(city) in self.used:
                return {"ok": False, "error": "этот город уже называли", "used": len(self.used)}
            need = last_letter(self.used[-1]) if self.used else ""
            if need and fold(city)[0] != need:
                return {"ok": False, "error": f"нужен город на букву «{need.upper()}»", "need": need.upper()}
            self.used.append(fold(city))
            nxt = last_letter(city).upper()
            return {"ok": True, "city": city, "next_letter": nxt, "count": len(self.used), "by": by}
        if self.name == "twenty":
            self.questions += 1
            left = max(MAX_QUESTIONS - self.questions, 0)
            return {"ok": True, "asked": self.questions, "left": left,
                    **({"note": "вопросы кончились: пусть называет догадку"} if left == 0 else {})}
        return {"ok": False, "error": "game_move нужен только в «Городах» и «20 вопросов»"}

    def caption(self, res: dict) -> str:
        if self.name == "cities" and res.get("ok"):
            return f"Города: {res['city']} → {res['next_letter']}"[:120]
        if self.name == "twenty" and res.get("ok"):
            return f"20 вопросов: {res['asked']}/{MAX_QUESTIONS}"
        return ""

    # ── инструменты Gemini ──

    async def call(self, name: str, args: dict[str, Any],
                   send: Callable[[dict], Awaitable[None]]) -> dict:
        if name == "game_scores":
            return {"ok": True, "scores": self.all_scores()}
        if name == "game_start":
            game = ALIASES.get(str(args.get("game", "")).strip().casefold())
            if game is None:
                return {"ok": False, "error": f"игры: {', '.join(GAMES.values())}"}
            self.reset()
            self.name = game
            out: dict[str, Any] = {"ok": True, "game": GAMES[game], "rules": game_rules(game),
                                   "score": self.score(game)}
            if game == "quiz" and str(args.get("source", "")).strip().casefold() in ("de", "german", "немецкий"):
                words = self.german.quiz_words(QUIZ_ROUNDS)
                out["words"] = words
                out["rules"] += (" Вопросы по немецким словам пользователя: спрашивай перевод слова или "
                                 "слово по переводу, каждое слово передавай в game_answer(word=...). "
                                 + ("Слова: " + "; ".join(f"{w['word']} — {w['translation']}" for w in words)
                                    if words else "Слов в словаре пока нет: задай вопросы по общим немецким словам."))
            await send(protocol.caption("reply", GAMES[game]))
            return out
        if not self.active:
            return {"ok": False, "error": "игра не начата, сначала game_start"}
        if name == "game_move":
            res = self.move(str(args.get("word", "")), "maga" if str(args.get("by", "")).strip().casefold() in
                            ("maga", "мага", "ты", "я") else "user")
            text = self.caption(res)
            if text:
                await send(protocol.caption("reply", text))
            return res
        if name == "game_answer":
            correct = bool(args.get("correct"))
            self.points["user" if correct else "maga"] += 1
            self.rounds += 1
            word = str(args.get("word", "")).strip()
            if word:
                self.german.review(word, correct)
            await send(protocol.caption("reply", f"Викторина {self.points['user']}:{self.points['maga']}"))
            await send(protocol.emotion("happy" if not correct else "surprise", 1500))
            return {"ok": True, "points": dict(self.points), "round": self.rounds,
                    "last_round": self.rounds >= QUIZ_ROUNDS}
        if name == "game_stop":
            game = self.name
            winner = str(args.get("winner", "")).strip().casefold()
            winner = {"пользователь": "user", "я": "user", "мага": "maga", "ты": "maga", "ничья": "draw"}.get(winner, winner)
            if not winner and game == "quiz" and self.rounds:
                p = self.points
                winner = "user" if p["user"] > p["maga"] else "maga" if p["maga"] > p["user"] else "draw"
            points = dict(self.points)
            self.reset()
            if winner not in ("user", "maga", "draw"):
                return {"ok": True, "recorded": False}
            score = self.record(game, winner)
            await send(protocol.caption("reply", f"{score['game']}: {score['user']}:{score['maga']}"))
            await send(protocol.emotion("sorry" if winner == "user" else "happy" if winner == "maga" else "side", 2500))
            return {"ok": True, "recorded": True, "winner": winner, "score": score, "points": points}
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── консоль ──

    async def command(self, args: str) -> str:
        sub, _, rest = args.strip().partition(" ")
        match sub.lower():
            case "score" | "":
                return "\n".join(f"{s['game']}: ты {s['user']}, Мага {s['maga']}, ничьих {s['draws']}"
                                 for s in self.all_scores())
            case "reset":
                self.db.execute("DELETE FROM game_scores")
                self.db.commit()
                self.reset()
                return "счёт обнулён"
            case "win" | "lose" | "draw":
                g = ALIASES.get(rest.strip().casefold())
                if g is None:
                    return "game win|lose|draw cities|twenty|quiz — записать результат вручную"
                s = self.record(g, {"win": "user", "lose": "maga", "draw": "draw"}[sub.lower()])
                return f"{s['game']}: ты {s['user']}, Мага {s['maga']}, ничьих {s['draws']}"
        return "game score | reset | win|lose|draw <игра> — счёт игр (играть: голосом «давай в города»)"
