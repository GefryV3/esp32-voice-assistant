"""Шёпот в ответ (#42): говорят шёпотом — отвечаем тише и коротко, как ночью.

Признак считаем по PCM, который сервер и так получает (16 кГц, int16), кадрами по 40 мс:
* уровень: у шёпота средняя громкость активных кадров ниже порога LEVEL_DB (дБFS);
* звонкость: у голоса есть основной тон (нормированная автокорреляция на лагах 50…400 Гц имеет пик
  выше 0.5), у шёпота тона нет, это шум с формантами (пик корреляции у шума ~0.2).
Шёпот = тихо И почти нет звонких кадров (не больше 25%). Решение принимается один раз на разговор,
как только набралось MIN_ACTIVE активных кадров (~0.5 с речи). Пороги не подобраны по живой записи:
статистика пишется в журнал, порог уровня правится консолью `whisper <дБFS>`.
"""

from __future__ import annotations

import math
from array import array
from operator import mul
from typing import Any

FRAME = 640                 # сэмплов в кадре: 40 мс при 16 кГц
DECIM = 4                   # прореживание до 4 кГц перед автокорреляцией (дёшево на чистом Python)
LAGS = range(10, 81)        # лаги 4 кГц → основной тон 400…50 Гц
ACTIVE_DB = -50.0           # тише — пауза, кадр не считаем
LEVEL_DB = -36.0            # средний уровень активных кадров ниже — «тихо»
VOICED_R = 0.5              # пик автокорреляции выше — кадр звонкий (есть основной тон)
MAX_VOICED = 0.25           # доля звонких активных кадров у шёпота не больше
MIN_ACTIVE = 12             # активных кадров для решения (~0.5 с речи)

WHISPER_FACTOR = 0.25       # громкость динамика на шёпот: доля дневной, но не ниже минимума
WHISPER_MIN = 5


def whisper_volume(day: int, night_level: int) -> int:
    """Ещё тише ночной: доля дневной (не ниже 5), но не громче ночной и не громче дневной."""
    if day <= 0:
        return 0
    return min(day, night_level, max(WHISPER_MIN, round(day * WHISPER_FACTOR)))


def frame_stats(frame: array) -> tuple[float, float]:
    """(уровень в дБFS, пик нормированной автокорреляции 0…1) одного кадра."""
    n = len(frame)
    rms = math.sqrt(sum(x * x for x in frame) / n) if n else 0.0
    db = 20 * math.log10(rms / 32768) if rms > 0 else -math.inf
    if db < ACTIVE_DB:
        return db, 0.0
    d = [sum(frame[i:i + DECIM]) / DECIM for i in range(0, n - DECIM + 1, DECIM)]
    mean = sum(d) / len(d)
    d = [x - mean for x in d]
    best = 0.0
    for lag in LAGS:
        a, b = d[:-lag], d[lag:]
        e = math.sqrt(sum(map(mul, a, a)) * sum(map(mul, b, b)))
        if e > 0:
            best = max(best, sum(map(mul, a, b)) / e)
    return db, best


class WhisperDetector:
    """Потоковый: feed(pcm) → None пока не решили, потом True (шёпот) или False. Решает один раз."""

    def __init__(self, level_db: float = LEVEL_DB):
        self.level_db = level_db
        self.verdict: bool | None = None
        self._buf = b""
        self._levels: list[float] = []
        self._voiced = 0

    def stats(self) -> str:
        n = len(self._levels)
        if not n:
            return "речи не было"
        return (f"средний уровень {sum(self._levels) / n:.1f} дБFS (порог {self.level_db:.0f}), "
                f"звонких кадров {self._voiced}/{n}")

    def feed(self, pcm: bytes) -> bool | None:
        if self.verdict is not None:
            return self.verdict
        self._buf += pcm
        size = FRAME * 2
        while len(self._buf) >= size and self.verdict is None:
            frame = array("h")
            frame.frombytes(self._buf[:size])
            self._buf = self._buf[size:]
            db, r = frame_stats(frame)
            if db < ACTIVE_DB:
                continue
            self._levels.append(db)
            self._voiced += r > VOICED_R
            if len(self._levels) >= MIN_ACTIVE:
                mean = sum(self._levels) / len(self._levels)
                self.verdict = mean < self.level_db and self._voiced / len(self._levels) <= MAX_VOICED
        return self.verdict


class WhisperConfig:
    """pref whisper (on по умолчанию, off) и whisper_db (порог уровня). brain=None — настройки в памяти."""

    def __init__(self, brain: Any = None):
        self.brain = brain
        self._prefs: dict[str, str] = {}

    def _get(self, key: str, default: str) -> str:
        return (self.brain.pref(key, default) if self.brain else self._prefs.get(key, default)) or default

    def _set(self, key: str, value: str) -> None:
        if self.brain:
            self.brain.set_pref(key, value)
        else:
            self._prefs[key] = value

    def enabled(self) -> bool:
        return self._get("whisper", "on") != "off"

    def level_db(self) -> float:
        try:
            return float(self._get("whisper_db", str(LEVEL_DB)))
        except ValueError:
            return LEVEL_DB

    def detector(self) -> WhisperDetector | None:
        return WhisperDetector(self.level_db()) if self.enabled() else None

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "whisper":
            return None
        arg = parts[1].lower() if len(parts) > 1 else "status"
        if arg in ("on", "off"):
            self._set("whisper", arg)
            return f"шёпот в ответ: {arg}"
        try:
            db = float(arg)
        except ValueError:
            return (f"шёпот в ответ: {'вкл' if self.enabled() else 'выкл'}, порог уровня {self.level_db():.0f} дБFS. "
                    "whisper on|off|<дБFS, например -36; больше (ближе к 0) — чаще считать шёпотом>")
        if not -70 <= db <= -10:
            return "порог в дБFS от -70 до -10"
        self._set("whisper_db", str(db))
        return f"порог шёпота {db:.0f} дБFS"
