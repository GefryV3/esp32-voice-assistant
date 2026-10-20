"""Перегрузка Gemini (503): распознать и не спамить пользователя одним и тем же сообщением.

Голосовой Live, фоновые агенты и Telegram при перегрузке пробуют запасную модель; об этом
пользователю говорят один раз за OVERLOAD_NOTE_EVERY_S, а не при каждом запросе.
"""

from __future__ import annotations

import urllib.error

OVERLOAD_NOTE_EVERY_S = 600.0     # о перегрузке говорим не чаще раза в 10 минут
FALLBACK_HOLD_S = 120.0           # после перехода на запасную модель столько держимся за неё

_MARKERS = ("503", "overload", "unavailable", "try again later", "1013", "high demand")

# одна фраза для голоса: Gemini проговорит её сам в начале ответа
VOICE_NOTE = ("\n\nВАЖНО: основная модель Gemini сейчас перегружена, ты работаешь на запасной. "
              "В самом начале первого ответа скажи об этом одной короткой фразой («Основной мозг "
              "перегружен, я на запасном»), дальше отвечай как обычно и больше об этом не напоминай.")
CHAT_NOTE = "⚠ Основная модель Gemini перегружена, ответил запасной."


def is_overload(err: object) -> bool:
    """503 / «перегружено» в ошибке: исключение, HTTPError или просто текст."""
    if isinstance(err, urllib.error.HTTPError):
        return err.code == 503
    text = str(err).casefold()
    return any(m in text for m in _MARKERS)


class Notice:
    """Разрешает «сказать об этом» не чаще раза в every секунд."""

    def __init__(self, every: float = OVERLOAD_NOTE_EVERY_S):
        self.every = every
        self._last = -1e18

    def take(self, now: float) -> bool:
        if now - self._last < self.every:
            return False
        self._last = now
        return True
