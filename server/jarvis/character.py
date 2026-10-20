"""Характер по времени суток и ночной тон. Дополняет ползунок «Язвительность» (sarcasm в мосте).

Ползунок задаёт базу, время суток сдвигает её: утром мягче, вечером язвительнее, после 23:00 спокойно.
Если пользователь выставил язвительность ниже порога (45), он хочет без подколов — не трогаем число.
"""

from __future__ import annotations

from datetime import datetime

SARCASM_ON = 45        # с этого значения подколы включены (как в voice.system_prompt)
SHIFT = {"morning": -15, "evening": 15, "late": -30}

NOTES = {
    "morning": "Сейчас утро: говори мягче и бодрее обычного, колкости реже и добрее, без шуток про сон.",
    "evening": "Сейчас вечер: можно язвить чуть смелее обычного, настроение расслабленное.",
    "late": "Сейчас поздно, после 23:00: говори спокойно и негромко, без подколов, мягко подталкивай ко сну.",
}

NIGHT_NOTE = ("Сейчас ночь. Говори тихо, вполголоса и коротко: одной фразой, без пересказов и шуток-"
              "простыней, не бодрись и не повышай голос.")

WHISPER_NOTE = ("Пользователь говорит шёпотом: рядом, вероятно, спят. Отвечай шёпотом, совсем тихо и одной "
                "короткой фразой, без шуток-простыней.")


def daypart(now: datetime) -> str:
    h = now.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 18:
        return "day"
    if 18 <= h < 23:
        return "evening"
    return "late"


def effective_sarcasm(now: datetime, base: int) -> int:
    if base < SARCASM_ON:
        return base
    return min(max(base + SHIFT.get(daypart(now), 0), 0), 100)


def daypart_note(now: datetime) -> str:
    note = NOTES.get(daypart(now), "")
    return f"\n{note}" if note else ""
