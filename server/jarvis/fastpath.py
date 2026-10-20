"""Быстрый путь (#38): частые простые команды выполняются локально, без Gemini.

Как это устроено. После слова пробуждения звук идёт и в Gemini (как раньше), и в маленький локальный
распознаватель Vosk с грамматикой из слов-команд (`FastListener`). Если Vosk дослушал фразу до конца,
уверен в каждом слове и вся фраза в точности совпадает с одной из команд списка (`parse`), команда
выполняется сразу, а сессия Gemini закрывается без ответа. Всё сомнительное (незнакомое слово, [unk]
внутри фразы, низкая уверенность, фраза длиннее команды) уходит в Gemini как раньше: ложное срабатывание
хуже промаха. Если Gemini уже начал отвечать, быстрый путь молчит.

Выбор: берём только законченный результат Vosk (конец фразы по паузе), а не частичный. Частичный быстрее,
но «включи свет… (пауза) …в коридоре» сработал бы на половине фразы.

Подтверждение по умолчанию без голоса: короткий тон и мимика/подпись на экране. Голосом «Готово»
говорим только для таймеров и при ошибке. pref `fastpath_voice`: off (по умолчанию) | short | full.
Голос идёт отдельным коротким разговором по тексту (Gemini) уже после закрытия основного.

Список команд расширяемый: добавь фразу в таблицу PHRASES и ветку в `FastPath.execute`.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import protocol
from .wake_vosk import RATE, _models, ensure_model, normalize

log = logging.getLogger(__name__)

MIN_CONF = 0.7             # уверенность каждого слова (Vosk с SetWords), ниже — в Gemini
MIN_TIMER_S = 5
MAX_TIMER_S = 86_400
DIM_STEP = -25             # «потемнее/поярче»: шаг яркости, %
BRIGHT_STEP = 25
MODES = ("off", "short", "full")

WAKE_TAIL = ("мага", "джарвис", "[unk]")   # хвост слова пробуждения в начале фразы
FILLER = ("пожалуйста",)


@dataclass(frozen=True)
class Command:
    kind: str            # light_on light_off all_off dim bright timer timer_cancel stop time alarm voice_on voice_off
    arg: Any = None      # timer: секунды
    text: str = ""       # как распознано


PHRASES: dict[str, tuple[str, ...]] = {
    "light_on": ("включи свет", "зажги свет", "включи лампу", "включи лампы", "включи освещение", "свет включи"),
    "light_off": ("выключи свет", "погаси свет", "выруби свет", "потуши свет", "выключи лампу",
                  "выключи лампы", "свет выключи", "отключи свет"),
    "all_off": ("выключи все", "все выключи", "выруби все"),
    "dim": ("потемнее", "сделай потемнее", "темнее", "сделай темнее", "приглуши свет", "убавь свет",
            "притуши свет", "свет потемнее", "сделай свет потемнее"),
    "bright": ("поярче", "сделай поярче", "ярче", "сделай ярче", "добавь света", "прибавь свет",
               "посветлее", "сделай посветлее", "свет поярче", "сделай свет поярче"),
    "timer_cancel": ("отмени таймер", "останови таймер", "сбрось таймер", "убери таймер", "выключи таймер"),
    "stop": ("стоп", "тихо", "замолчи", "хватит", "перестань"),
    "time": ("который час", "сколько времени", "сколько сейчас времени", "который сейчас час", "скажи время"),
    "alarm": ("сколько до будильника", "сколько осталось до будильника", "когда будильник",
              "когда звонит будильник"),
    "voice_on": ("отвечай на команды вслух", "отвечай на команды голосом"),
    "voice_off": ("отвечай на команды молча", "отвечай на команды тихо"),
}
_STATIC = {phrase: kind for kind, phrases in PHRASES.items() for phrase in phrases}

TIMER_VERBS = ("поставь", "запусти", "засеки", "заведи", "включи", "сделай")
UNITS = {"секунду": 1, "секунды": 1, "секунд": 1, "минуту": 60, "минуты": 60, "минут": 60,
         "час": 3600, "часа": 3600, "часов": 3600}
ONES = {"ноль": 0, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
        "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
        "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
        "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
        "восемьдесят": 80, "девяносто": 90}
HUNDRED = "сто"
OTHER_TIMER_WORDS = ("таймер", "на", "полчаса", "четверть", "часа")


def vocabulary() -> list[str]:
    """Слова грамматики Vosk: всё, из чего собираются команды, плюс [unk] для остального."""
    words: set[str] = set(WAKE_TAIL[:2]) | set(FILLER) | set(TIMER_VERBS) | set(UNITS) | set(ONES) | set(TENS)
    words |= {HUNDRED, *OTHER_TIMER_WORDS}
    for phrase in _STATIC:
        words.update(phrase.split())
    return sorted(words) + ["[unk]"]


def parse_number(tokens: list[str]) -> int | None:
    """«двадцать пять» → 25; порядок «сто, десятки, единицы», иначе None."""
    if not tokens:
        return None
    total, stage = 0, 0         # stage: 0 — ждём сотни, 1 — десятки, 2 — единицы, 3 — всё
    for t in tokens:
        v = ONES.get(t, 0)
        if t == HUNDRED and stage == 0:
            total, stage = total + 100, 1
        elif t in TENS and stage <= 1:
            total, stage = total + TENS[t], 2
        elif 1 <= v <= 9 and stage <= 2:
            total, stage = total + v, 3
        elif v >= 10 and stage <= 1:
            total, stage = total + v, 3
        else:
            return None
    return total


def parse_duration(tokens: list[str]) -> int | None:
    """«пять минут», «полчаса», «час», «две минуты», «тридцать секунд» → секунды."""
    if tokens == ["полчаса"]:
        return 1800
    if tokens == ["четверть", "часа"]:
        return 900
    if not tokens or tokens[-1] not in UNITS:
        return None
    unit = UNITS[tokens[-1]]
    if len(tokens) == 1:
        count = 1 if tokens[0] in ("час", "минуту", "секунду") else None
    else:
        count = parse_number(tokens[:-1])
    if count is None or count <= 0:
        return None
    return count * unit


def parse(text: str) -> Command | None:
    """Распознанная фраза → команда, если она ВСЯ — одна из команд списка, иначе None (в Gemini)."""
    tokens = normalize(text)
    for _ in range(2):                              # хвост слова пробуждения в начале
        if tokens and tokens[0] in WAKE_TAIL:
            tokens = tokens[1:]
    while tokens and tokens[-1] in FILLER:
        tokens = tokens[:-1]
    if not tokens or "[unk]" in tokens:
        return None
    clean = " ".join(tokens)
    if kind := _STATIC.get(clean):
        return Command(kind, text=clean)
    if "таймер" in tokens:
        rest = tokens
        if rest[0] in TIMER_VERBS:
            rest = rest[1:]
        if len(rest) >= 3 and rest[0] == "таймер" and rest[1] == "на":
            sec = parse_duration(rest[2:])
            if sec is not None and MIN_TIMER_S <= sec <= MAX_TIMER_S:
                return Command("timer", sec, clean)
    return None


def words_ru(sec: int) -> str:
    """Для подписи и подсказки: 300 → «5 мин», 90 → «1 мин 30 с», 7200 → «2 ч»."""
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return " ".join(p for p in (f"{h} ч" if h else "", f"{m} мин" if m else "", f"{s} с" if s else "") if p)


class FastListener:
    """Локальный распознаватель одной колонки. feed(pcm) → Command, когда фраза закончилась и она команда."""

    def __init__(self, recognizer_factory, min_conf: float = MIN_CONF):
        self._factory = recognizer_factory
        self.min_conf = min_conf
        self.reset()

    def reset(self) -> None:
        self._rec = self._factory()

    def feed(self, pcm: bytes) -> Command | None:
        if not self._rec.AcceptWaveform(pcm):
            return None
        data = json.loads(self._rec.Result())
        text = str(data.get("text", ""))
        if not text:
            return None
        words = data.get("result")
        if words and min((float(w.get("conf", 1.0)) for w in words), default=1.0) < self.min_conf:
            log.info("быстрый путь: «%s» — низкая уверенность, отдаю Gemini", text)
            return None
        return parse(text)


def make_fast_factory(model_dir: Path):
    """Фабрика слушателей; нет модели или vosk — None (быстрого пути нет, всё идёт в Gemini)."""
    try:
        path = ensure_model(Path(model_dir))
        from vosk import KaldiRecognizer, Model, SetLogLevel
        SetLogLevel(-1)
        key = str(path)
        if key not in _models:
            _models[key] = Model(key)
        grammar = json.dumps(vocabulary(), ensure_ascii=False)

        def make() -> FastListener:
            def rec():
                r = KaldiRecognizer(_models[key], RATE, grammar)
                r.SetWords(True)
                return r
            return FastListener(rec)
        return make
    except Exception as e:
        log.warning("быстрый путь выключен: %s", e)
        return None


@dataclass
class Outcome:
    ok: bool = True
    note: str = ""             # короткая подпись на экране
    say: str = ""              # что сказать вслух в режиме full (без Gemini это только подсказка)
    icon: str = "none"
    voice: str = ""            # "" — по настройке; "always" — голосом в любом режиме (таймер, ошибка)
    silent: bool = False       # без тона и подписи (стоп)


class FastPath:
    """Настройки быстрого пути, исполнение команд и журнал. voice — Voice (brain, hub, audio, now)."""

    def __init__(self, voice: Any, factory=None, *, clock=time.monotonic):
        self.voice = voice
        self.factory = factory
        self.clock = clock
        self._prefs: dict[str, str] = {}      # когда мозга нет (тесты, голос без brain)
        self.history: deque[tuple[datetime, str, str, int]] = deque(maxlen=50)

    # ── настройки ──

    def _pref(self, key: str, default: str) -> str:
        brain = self.voice.brain
        return (brain.pref(key, default) if brain else self._prefs.get(key, default)) or default

    def _set(self, key: str, value: str) -> None:
        if self.voice.brain:
            self.voice.brain.set_pref(key, value)
        else:
            self._prefs[key] = value

    def available(self) -> bool:
        return self.factory is not None

    def enabled(self) -> bool:
        return self.available() and self._pref("fastpath", "on") != "off"

    def mode(self) -> str:
        m = self._pref("fastpath_voice", "off")
        return m if m in MODES else "off"

    def listener(self) -> FastListener | None:
        if not self.enabled():
            return None
        brain = self.voice.brain
        if brain is not None and brain.long_talk:
            return None            # немецкий и игры: фразы свободные, команды не перехватываем
        return self.factory()

    # ── исполнение ──

    async def execute(self, dev: Any, cmd: Command) -> Outcome | None:
        """None — быстрый путь не может (нет света/мозга): пусть отвечает Gemini."""
        v = self.voice
        brain, lights = v.brain, getattr(v.hub, "lights", None)
        if cmd.kind in ("light_on", "light_off", "all_off", "dim", "bright"):
            if lights is None:
                return None
            res = await lights.room({"all_off": "off"}.get(cmd.kind, cmd.kind.removeprefix("light_")))
            if not res.get("ok"):
                return Outcome(False, res.get("error", "не вышло"), voice="always")
            return Outcome(note=res["note"], say=res["note"], icon="light")
        if cmd.kind == "timer":
            if brain is None:
                return None
            out = await brain.call("set_timer", {"minutes": cmd.arg / 60}, dev.send)
            if not out.get("ok"):
                return Outcome(False, str(out.get("error", "не вышло")), voice="always")
            return Outcome(note=f"Таймер {words_ru(cmd.arg)}", say=f"таймер на {words_ru(cmd.arg)} запущен",
                           voice="always")
        if cmd.kind == "timer_cancel":
            if brain is None:
                return None
            had = bool(brain.timers)
            await brain.call("set_timer", {"minutes": 0}, dev.send)
            return Outcome(note="Таймер отменён" if had else "Таймеров нет",
                           say="таймер отменён" if had else "таймеров нет")
        if cmd.kind == "stop":
            await v.audio.stop(dev)
            return Outcome(silent=True)
        if cmd.kind == "time":
            now = v.now()
            return Outcome(note=f"{now:%H:%M}", say=f"сейчас {now:%H} часов {now:%M} минут")
        if cmd.kind == "alarm":
            return self._alarm(v.now())
        if cmd.kind in ("voice_on", "voice_off"):
            self._set("fastpath_voice", "short" if cmd.kind == "voice_on" else "off")
            return Outcome(note="Отвечаю вслух" if cmd.kind == "voice_on" else "Отвечаю молча",
                           say="буду отвечать на команды вслух" if cmd.kind == "voice_on"
                           else "буду отвечать молча")
        return None

    def _alarm(self, now: datetime) -> Outcome:
        bridge = self.voice.hub.bridge
        at = ((getattr(bridge, "phone_state", None) or {}).get("next_alarm", 0) or 0) / 1000
        left = int(at - now.timestamp()) if at else 0
        if left <= 0:
            return Outcome(note="Будильник не стоит", say="будильник не стоит")
        mins = left // 60
        text = f"{mins // 60} ч {mins % 60} мин" if mins >= 60 else f"{max(mins, 1)} мин"
        return Outcome(note=f"До будильника {text}", say=f"до будильника {text}")

    def voice_prompt(self, cmd: Command, out: Outcome) -> str:
        """Подсказка для короткого голосового ответа Gemini или '' (молчим, только тон/экран)."""
        if out.silent:
            return ""
        mode = self.mode()
        if not out.ok:
            return f"Не получилось: {out.note}. Скажи это одной очень короткой фразой."
        info = cmd.kind in ("time", "alarm")
        if mode == "off" and out.voice != "always":
            return ""
        if mode == "full" or (info and mode != "off"):
            return (f"Ты только что выполнил голосовую команду пользователя: {out.say}. Подтверди это "
                    "одной очень короткой фразой, без шуток-простыней.")
        return "Скажи одно слово: «Готово»."

    def record(self, cmd: Command, ms: int) -> None:
        self.history.append((self.voice.now(), cmd.kind, cmd.text, ms))
        log.info("быстрый путь: «%s» (%s) выполнено за %d мс после слова пробуждения", cmd.text, cmd.kind, ms)

    # ── консоль ──

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "fast":
            return None
        args = [a.lower() for a in parts[1:]]
        if args[:1] in (["on"], ["off"]):
            self._set("fastpath", args[0])
            return f"быстрый путь {args[0]}" + ("" if self.available() else " (но модели Vosk нет — работать не будет)")
        if args[:1] == ["voice"]:
            if len(args) < 2 or args[1] not in MODES:
                return f"fast voice off|short|full — сейчас {self.mode()} (off = тон/экран, без голоса)"
            self._set("fastpath_voice", args[1])
            return f"ответы на быстрые команды: {args[1]}"
        if args[:1] == ["log"]:
            return "\n".join(f"{t:%H:%M:%S} {kind} «{text}» {ms} мс" for t, kind, text, ms in self.history) \
                or "быстрых команд ещё не было"
        state = ("выкл" if not self.enabled() else "вкл") + ("" if self.available() else ", нет модели Vosk")
        last = self.history[-1] if self.history else None
        return (f"быстрый путь: {state}; ответы вслух: {self.mode()}; выполнено {len(self.history)}"
                + (f", последняя «{last[2]}» за {last[3]} мс" if last else "")
                + ". fast on|off, fast voice off|short|full, fast log")
