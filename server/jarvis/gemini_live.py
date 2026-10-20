"""Клиент Gemini Live API: голос туда, голос обратно по одному WebSocket.

Сервер шлёт звук микрофона (PCM 16 кГц), Gemini сам слышит конец фразы, отвечает звуком
(PCM 24 кГц) и присылает расшифровки обеих сторон. Формат сообщений:
https://ai.google.dev/api/live
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

log = logging.getLogger(__name__)

LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
SETUP_TIMEOUT_S = 10.0
IN_RATE = 16_000
DEFAULT_OUT_RATE = 24_000


class LiveError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    """Что пришло от Gemini. kind: audio (data — PCM, rate), heard (кусок расшифровки
    пользователя), said (кусок расшифровки ответа), tool_call (calls), turn_complete, interrupted, go_away."""
    kind: str
    data: bytes = b""
    text: str = ""
    rate: int = DEFAULT_OUT_RATE
    calls: tuple[tuple[str, str, dict], ...] = ()   # tool_call: (id, имя, аргументы)


SILENCE_MS = 650   # сколько тишины Gemini считает концом фразы: меньше — быстрее ответ, но обрезает паузы


def setup_message(model: str, voice: str, system: str,
                  tools: list[dict[str, Any]] | None = None,
                  silence_ms: int = SILENCE_MS, affective: bool = False) -> dict[str, Any]:
    setup: dict[str, Any] = {
        "model": model if model.startswith("models/") else f"models/{model}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
        "systemInstruction": {"parts": [{"text": system}]},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {"automaticActivityDetection": {
            "silenceDurationMs": silence_ms,
            "startOfSpeechSensitivity": "START_SENSITIVITY_HIGH",
            "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",   # не обрывать на паузе посреди фразы
        }},
    }
    if affective:     # модель слышит интонацию и отвечает в тон (только v1alpha, не на каждой модели)
        setup["generationConfig"]["enableAffectiveDialog"] = True
    if tools:
        setup["tools"] = [{"functionDeclarations": tools}]
    return {"setup": setup}


def parse(msg: dict[str, Any]) -> list[Event]:
    """Одно сообщение сервера → события (в нём может быть сразу звук и расшифровка)."""
    events: list[Event] = []
    if "goAway" in msg:
        events.append(Event("go_away"))
    calls = (msg.get("toolCall") or {}).get("functionCalls")
    if calls:
        events.append(Event("tool_call", calls=tuple(
            (str(c.get("id", "")), str(c.get("name", "")), c.get("args") or {}) for c in calls)))
    content = msg.get("serverContent")
    if not isinstance(content, dict):
        return events
    if text := (content.get("inputTranscription") or {}).get("text"):
        events.append(Event("heard", text=text))
    for part in (content.get("modelTurn") or {}).get("parts", []):
        blob = part.get("inlineData")
        if isinstance(blob, dict) and str(blob.get("mimeType", "")).startswith("audio/pcm"):
            m = re.search(r"rate=(\d+)", blob["mimeType"])
            events.append(Event("audio", data=base64.b64decode(blob.get("data", "")),
                                rate=int(m.group(1)) if m else DEFAULT_OUT_RATE))
    if text := (content.get("outputTranscription") or {}).get("text"):
        events.append(Event("said", text=text))
    if content.get("interrupted"):
        events.append(Event("interrupted"))
    if content.get("turnComplete"):
        events.append(Event("turn_complete"))
    return events


class LiveSession:
    """async with LiveSession(...) as live: live.send_audio(...); async for ev in live.events()."""

    def __init__(self, api_key: str, model: str, voice: str, system: str, *,
                 tools: list[dict[str, Any]] | None = None, silence_ms: int = SILENCE_MS,
                 affective: bool = False, url: str = LIVE_URL, connect: Callable = ws_connect):
        self._key = api_key
        self._setup = setup_message(model, voice, system, tools, silence_ms, affective)
        self._url = url.replace("/v1beta.", "/v1alpha.") if affective else url
        self._connect = connect
        self._ws: Any = None

    async def __aenter__(self) -> LiveSession:
        try:
            self._ws = await self._connect(self._url, additional_headers={"x-goog-api-key": self._key},
                                           max_size=None, open_timeout=SETUP_TIMEOUT_S)
            await self._ws.send(json.dumps(self._setup))
            reply = json.loads(await asyncio.wait_for(self._ws.recv(), SETUP_TIMEOUT_S))
        except ConnectionClosed as e:
            raise LiveError(f"Gemini закрыл соединение: {e.rcvd.code if e.rcvd else '?'} "
                            f"{e.rcvd.reason if e.rcvd else ''}".strip()) from None
        except InvalidStatus as e:     # сервер ответил на рукопожатие кодом (503 при перегрузке)
            raise LiveError(f"Gemini ответил HTTP {e.response.status_code}") from None
        except InvalidHandshake as e:
            raise LiveError(f"Gemini отклонил соединение: {e!r}") from None
        except (OSError, TimeoutError) as e:
            raise LiveError(f"нет связи с Gemini: {e!r}") from None
        if "setupComplete" not in reply:
            await self._ws.close()
            raise LiveError(f"Gemini не принял настройки: {str(reply)[:200]}")
        return self

    @property
    def alive(self) -> bool:
        """Соединение ещё открыто (заготовленная сессия могла остыть и закрыться)."""
        ws = self._ws
        return ws is not None and getattr(getattr(ws, "state", None), "name", "OPEN") == "OPEN"

    async def __aexit__(self, *exc: object) -> None:
        if self._ws is not None:
            await self._ws.close()

    async def send_audio(self, pcm: bytes, rate: int = IN_RATE) -> None:
        await self._ws.send(json.dumps({"realtimeInput": {"audio": {
            "mimeType": f"audio/pcm;rate={rate}", "data": base64.b64encode(pcm).decode()}}}))

    async def audio_stream_end(self) -> None:
        """Микрофон замолчал (мы его выключили на время ответа) — не ждать от него конца фразы."""
        await self._ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

    async def send_text(self, text: str) -> None:
        await self._ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": True}}))

    async def send_tool_results(self, results: list[tuple[str, str, dict]]) -> None:
        """Ответы на tool_call: (id, имя, результат)."""
        await self._ws.send(json.dumps({"toolResponse": {"functionResponses": [
            {"id": i, "name": n, "response": r} for i, n, r in results]}}))

    async def events(self) -> AsyncIterator[Event]:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    log.warning("Gemini прислал не JSON")
                    continue
                for ev in parse(msg):
                    yield ev
        except ConnectionClosed as e:
            if e.rcvd and e.rcvd.code != 1000:
                raise LiveError(f"Gemini закрыл соединение: {e.rcvd.code} {e.rcvd.reason}") from None
