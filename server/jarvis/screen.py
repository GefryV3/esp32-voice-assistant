"""Что сейчас на экране колонки — по тому, что сервер ей отправлял. Нужно приложению на
телефоне: показать колонку в том же состоянии, в каком она сейчас.

Приоритет такой же, как в прошивке (docs/protocol.md, «Приоритет на экране»): карточка поверх
всего → разговор → таймер → ночь → сон по тишине → покой (с плеером, если играет).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .protocol import Message

VOICE_TITLES = {"listening": "Слушает", "thinking": "Думает", "speaking": "Говорит"}
CARD_TITLES = {"msg": "Уведомление", "call": "Звонок", "battery": "Батарея", "info": "Сводка",
               "remark": "Колкость", "wake": "Будит", "check": "Проверка перед сном"}


@dataclass
class Screen:
    voice: str = "idle"
    night: bool = False
    sleep_after: float = 180.0
    timer_end: float = 0.0
    timer_label: str = ""
    card: dict[str, Any] = field(default_factory=dict)
    card_until: float = 0.0
    player: dict[str, Any] = field(default_factory=dict)
    active_at: float = field(default_factory=time.time)   # последнее событие, которое будит экран

    def apply(self, msg: Message, now: float) -> None:
        t = msg.get("type")
        if t == "voice":
            self.voice = str(msg.get("state", "idle"))
            self.active_at = now
        elif t == "emotion":
            self.active_at = now
        elif t == "timer":
            sec = int(msg.get("sec", 0) or 0)
            self.timer_end = now + sec if sec > 0 else 0.0
            self.timer_label = str(msg.get("label", ""))
            self.active_at = now
        elif t == "night":
            self.night = bool(msg.get("on"))
        elif t == "config" and "sleep_after" in msg:
            self.sleep_after = float(msg["sleep_after"])
        elif t == "notify":
            self.card = {k: msg.get(k, "") for k in ("kind", "title", "text", "app")}
            self.card_until = now + int(msg.get("ms", 8000)) / 1000
        elif t == "notify_hide":
            self.card_until = 0.0
        elif t == "player":
            self.player = {k: msg.get(k) for k in ("state", "title", "artist")}

    def summary(self, now: float) -> dict[str, Any]:
        """Режим экрана для приложения: mode, title (коротко), detail, until (мс, для таймера)."""
        if now < self.card_until and self.card:
            kind = str(self.card.get("kind", "msg"))
            title = str(self.card.get("title", ""))
            return {"mode": "card", "title": CARD_TITLES.get(kind, "Карточка"), "detail": title}
        if self.voice in VOICE_TITLES:
            return {"mode": self.voice, "title": VOICE_TITLES[self.voice], "detail": ""}
        if now < self.timer_end:
            return {"mode": "timer", "title": "Таймер", "detail": self.timer_label,
                    "until": int(self.timer_end * 1000)}
        if self.night:
            return {"mode": "night", "title": "Ночь", "detail": "показывает только важное"}
        playing = self.player.get("state") == "playing"
        if not playing and now - self.active_at > self.sleep_after:
            return {"mode": "sleep", "title": "Спит", "detail": "часы на экране"}
        if playing:
            who = " — ".join(x for x in (self.player.get("artist"), self.player.get("title")) if x)
            return {"mode": "player", "title": "Играет", "detail": who}
        return {"mode": "idle", "title": "Ждёт", "detail": "глаза и часы"}
