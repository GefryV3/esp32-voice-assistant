"""Протокол колонка ↔ сервер, версия 1. Описание: docs/protocol.md.

Здесь — единственное место на сервере, где знают формат сообщений. Конструкторы
проверяют диапазоны: колонке никогда не уходит значение, которое она не умеет показать.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

PROTO_VERSION = 1

CLOSE_REPLACED = 4000      # та же колонка подключилась заново
CLOSE_BAD_TOKEN = 4001
CLOSE_BAD_HELLO = 4002
CLOSE_BAD_VERSION = 4003

HELLO_TIMEOUT_S = 5.0

VOICE_STATES = ("idle", "listening", "thinking", "speaking")
EMOTIONS = ("happy", "skeptic", "roll", "side", "surprise", "sorry")
CAPTION_KINDS = ("heard", "said", "reply", "none")
ICONS = ("none", "light")
SLEEP_CLOCK = ("top", "bottom")
CAPS = ("display", "mic", "speaker", "wake", "phone", "pc")
NOTIFY_KINDS = ("msg", "call", "battery", "info", "remark", "wake", "check")

Message = dict[str, Any]


class ProtocolError(Exception):
    def __init__(self, message: str, close_code: int = CLOSE_BAD_HELLO):
        super().__init__(message)
        self.close_code = close_code


def encode(msg: Message) -> str:
    return json.dumps(msg, ensure_ascii=False, separators=(",", ":"))


def decode(raw: str | bytes) -> Message:
    if isinstance(raw, bytes):
        raise ProtocolError("ожидался текстовый кадр, пришёл бинарный")
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"битый JSON: {e}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("сообщение должно быть объектом с полем type")
    return msg


# ── рукопожатие ──────────────────────────────────────────────────────────────

_DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Hello:
    device: str
    fw: str
    token: str
    caps: frozenset[str] = field(default_factory=frozenset)


def parse_hello(raw: str | bytes) -> Hello:
    msg = decode(raw)
    if msg["type"] != "hello":
        raise ProtocolError(f"первым должно идти hello, пришло {msg['type']!r}")
    if msg.get("proto") != PROTO_VERSION:
        raise ProtocolError(
            f"версия протокола {msg.get('proto')!r} не поддерживается (нужна {PROTO_VERSION})",
            CLOSE_BAD_VERSION,
        )
    device = msg.get("device")
    if not isinstance(device, str) or not _DEVICE_ID.match(device):
        raise ProtocolError(f"плохое имя устройства: {device!r}")
    token = msg.get("token")
    if not isinstance(token, str):
        raise ProtocolError("нет token")
    caps = msg.get("caps", [])
    if not isinstance(caps, list):
        raise ProtocolError("caps должен быть списком")
    return Hello(
        device=device,
        fw=str(msg.get("fw", "?")),
        token=token,
        caps=frozenset(c for c in caps if c in CAPS),
    )


def welcome(now: float, tz: str, server_version: str) -> Message:
    return {"type": "welcome", "proto": PROTO_VERSION, "server": server_version,
            "time": int(now), "tz": tz}


# ── команды экрану ───────────────────────────────────────────────────────────

def _int_in(name: str, value: int, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"{name} должен быть целым {lo}…{hi}, получено {value!r}")
    return value


def _text(name: str, value: str, max_bytes: int) -> str:
    """Строка для колонки: у неё буферы фиксированной длины в байтах UTF-8."""
    if not isinstance(value, str) or len(value.encode()) > max_bytes:
        raise ValueError(f"{name} — строка не длиннее {max_bytes} байт UTF-8, получено {value!r}")
    return value


def _one_of(name: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ValueError(f"{name} должен быть одним из {allowed}, получено {value!r}")
    return value


def voice(state: str) -> Message:
    return {"type": "voice", "state": _one_of("state", state, VOICE_STATES)}


def emotion(name: str, ms: int = 2600) -> Message:
    return {"type": "emotion", "name": _one_of("эмоция", name, EMOTIONS),
            "ms": _int_in("ms", ms, 300, 60_000)}


def caption(kind: str, text: str = "", icon: str = "none") -> Message:
    """Подпись внизу экрана в разговоре: heard — что слышно сейчас, said — что понято,
    reply — что сделано (со значком), none — убрать."""
    return {"type": "caption", "kind": _one_of("kind", kind, CAPTION_KINDS),
            "text": _text("text", text, 127), "icon": _one_of("icon", icon, ICONS)}


def timer(sec: int, label: str = "") -> Message:
    """Таймер считает сама колонка; sec 0 — снять."""
    return {"type": "timer", "sec": _int_in("sec", sec, 0, 86_400), "label": _text("label", label, 31)}


def agenda(accent: str = "", text: str = "") -> Message:
    """Ближайшее дело по нижней дуге: accent — цветом, text — следом. Оба пустые — убрать."""
    return {"type": "agenda", "accent": _text("accent", accent, 47), "text": _text("text", text, 79)}


def config(sleep_after: int | None = None, sleep_clock: str | None = None) -> Message:
    """Настройки экрана: через сколько секунд тишины засыпать и где часы во сне."""
    msg: Message = {"type": "config"}
    if sleep_after is not None:
        msg["sleep_after"] = _int_in("sleep_after", sleep_after, 10, 86_400)
    if sleep_clock is not None:
        msg["sleep_clock"] = _one_of("sleep_clock", sleep_clock, SLEEP_CLOCK)
    return msg


def coin(result: int) -> Message:
    return {"type": "game", "name": "coin", "result": _int_in("result", result, 0, 1)}


def dice(result: int) -> Message:
    return {"type": "game", "name": "dice", "result": _int_in("result", result, 1, 6)}


def wheel(count: int, result: int) -> Message:
    _int_in("count", count, 2, 8)
    return {"type": "game", "name": "wheel", "count": count,
            "result": _int_in("result", result, 0, count - 1)}


def rps(mine: int, result: int) -> Message:
    return {"type": "game", "name": "rps", "mine": _int_in("mine", mine, 0, 2),
            "result": _int_in("result", result, 0, 2)}


def magic8(answer: int) -> Message:
    return {"type": "game", "name": "magic8", "answer": _int_in("answer", answer, 1, 10)}


def ttt(board: str) -> Message:
    if len(board) != 9 or any(c not in "XO." for c in board):
        raise ValueError(f"board — 9 символов из X, O, '.', получено {board!r}")
    return {"type": "game", "name": "ttt", "board": board}


def weather(cond: int, temp: int) -> Message:
    return {"type": "weather", "cond": _int_in("cond", cond, -1, 7),
            "temp": _int_in("temp", temp, -60, 60)}


def night(on: bool) -> Message:
    return {"type": "night", "on": bool(on)}


def brightness(level: int) -> Message:
    return {"type": "brightness", "level": _int_in("level", level, 0, 255)}


def reminders(count: int) -> Message:
    return {"type": "reminders", "count": _int_in("count", count, 0, 99)}


def barge(on: bool, ratio: float = 3.0) -> Message:
    """Перебивание голосом: колонка сама слушает эхо и замолкает, когда её перебивают."""
    return {"type": "barge", "on": int(on), "ratio_x10": _int_in("ratio_x10", round(ratio * 10), 12, 200)}


def ping(ping_id: int) -> Message:
    return {"type": "ping", "id": ping_id}


# ── звук ─────────────────────────────────────────────────────────────────────

AUDIO_RATE = 16_000        # микрофон колонки и звук по умолчанию: 16 кГц, 16 бит, моно


def listen_start(gain_db: int = 12) -> Message:
    return {"type": "listen_start", "gain": _int_in("gain", gain_db, 0, 30)}


def listen_stop() -> Message:
    return {"type": "listen_stop"}


def audio_start(rate: int = AUDIO_RATE) -> Message:
    return {"type": "audio_start", "rate": _int_in("rate", rate, 8000, 48_000)}


def audio_end() -> Message:
    return {"type": "audio_end"}


def audio_stop() -> Message:
    return {"type": "audio_stop"}


def volume(level: int) -> Message:
    return {"type": "volume", "level": _int_in("level", level, 0, 100)}


# ── обновление прошивки ──────────────────────────────────────────────────────

def ota(url: str, version: str, size: int) -> Message:
    """Скачать прошивку с этого же сервера (url — путь с ?token=) и перезагрузиться в неё."""
    if not url.startswith("/") or len(url.encode()) > 200:
        raise ValueError(f"url — путь на сервере не длиннее 200 байт, получено {url!r}")
    return {"type": "ota", "url": url, "version": version, "size": int(size)}


def _clip(value: str, max_bytes: int) -> str:
    """Обрезать по границе символа, чтобы влезло в буфер колонки (байты UTF-8)."""
    data = value.encode()
    if len(data) <= max_bytes:
        return value
    return data[:max_bytes].decode(errors="ignore").rstrip() + "…" if max_bytes > 3 else ""


def notify(kind: str, title: str, text: str = "", app: str = "", ms: int = 8000, level: int | None = None) -> Message:
    """Карточка на экране колонки: уведомление с телефона, звонок, батарея, колкость.
    Длинный текст обрезается — колонка показывает две строки. level — заряд для батареи."""
    msg = {"type": "notify", "kind": _one_of("kind", kind, NOTIFY_KINDS),
           "app": _clip(app, 31), "title": _clip(title, 60), "text": _clip(text, 190),
           "ms": _int_in("ms", ms, 1000, 120_000)}
    if level is not None:
        msg["level"] = _int_in("level", level, 0, 100)
    return msg


def notify_hide() -> Message:
    return {"type": "notify_hide"}


def player(state: str, title: str = "", artist: str = "", pos_ms: int = 0, dur_ms: int = 0) -> Message:
    """Мини-плеер: что играет на телефоне. state none — убрать."""
    return {"type": "player", "state": _one_of("state", state, ("playing", "paused", "none")),
            "title": _clip(title, 60), "artist": _clip(artist, 47),
            "pos": max(0, int(pos_ms)) // 1000, "dur": max(0, int(dur_ms)) // 1000}
