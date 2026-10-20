"""Telegram-бот: переписка с Jarvis текстом и напоминания, когда колонка не ответила.

Выбор: Bot API ходит обычным HTTPS через стандартную библиотеку (urllib в потоке), новых
зависимостей нет. Входящие — long polling (getUpdates). Ответ даёт Gemini через REST
generateContent (не Live): текстовый запрос проще, чем держать голосовую сессию без звука, а
цикл «функция → результат → ответ» занимает десяток строк. Инструменты те же, что у голоса:
Brain.tools() и Brain.call(). Чужим chat_id не отвечаем вообще, даже «нельзя».

История общая с голосом (Brain.log_message / recent_messages, таблица messages): каждое сообщение и
ответ пишем туда, а контекст берём оттуда же блоком «Недавний разговор» в подсказке — с пометкой
источника и времени, поэтому бот знает, что говорили колонке, а колонка — что писали сюда. Внутри
одного запроса цепочка «функция → результат» остаётся настоящими ходами Gemini. Без Brain (тесты,
консоль без базы) работает прежняя история в памяти.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from .brain import Brain
from .gemini_fallback import CHAT_NOTE as OVERLOAD_NOTE, Notice
from .voice import system_prompt

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
FALLBACK_MODELS = ("gemini-flash-lite-latest",)   # у «большой» модели на бесплатном ключе 20 запросов в сутки
GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
POLL_S = 30            # сколько Telegram держит getUpdates открытым
MAX_LEN = 4000         # лимит сообщения Telegram — 4096
MAX_ROUNDS = 5         # сколько раз подряд Gemini может звать функции
HISTORY = 12           # реплик общей истории в подсказке (и в памяти, если нет Brain)
RETRY_S = 5.0
RETRY_GEMINI_S = 2.0

CHAT_NOTE = """

ВАЖНО: сейчас это не голос, а переписка в Telegram. Ответ читают глазами: «звучит вслух» к нему
не относится. Можно короткие списки и числа цифрами, но без длинных простыней и без разметки
Markdown. Отвечай по-русски."""

Http = Callable[[str, dict[str, Any], dict[str, str], float], Awaitable[dict]]


async def urllib_post(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> dict:
    """POST JSON → JSON. Ошибки HTTP превращаются в RuntimeError без ключей в тексте."""
    def run() -> dict:
        req = urllib.request.Request(url, json.dumps(payload).encode(), {
            "Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:200].decode(errors='replace')}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"нет связи: {e.reason}") from None
    return await asyncio.to_thread(run)


def parse_chat_ids(raw: str) -> frozenset[int]:
    """«123, 456» → {123, 456}; мусор пропускаем."""
    ids = set()
    for part in raw.replace(";", ",").split(","):
        try:
            ids.add(int(part.strip()))
        except ValueError:
            pass
    return frozenset(ids)


def split_text(text: str, limit: int = MAX_LEN) -> list[str]:
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts + [text] if text.strip() else parts


async def _no_device(msg: dict) -> None:
    pass


class Telegram:
    def __init__(self, token: str, chat_ids: frozenset[int], *, api_key: str, model: str,
                 brain: Brain | None = None, now: Callable[[], datetime] = datetime.now,
                 sarcasm: Callable[[], int] = lambda: 60,
                 device_send: Callable[[dict], Awaitable[None]] = _no_device,
                 http: Http = urllib_post, fallback_model: str = "", clock: Callable[[], float] = time.monotonic):
        self.token, self.chat_ids, self.api_key, self.model = token, chat_ids, api_key, model
        self.brain, self.now, self.sarcasm, self.http = brain, now, sarcasm, http
        self.device_send = device_send
        self.fallback_model = fallback_model      # запасная модель при 503 (кроме FALLBACK_MODELS)
        self._clock = clock
        self._notice = Notice()
        self._fell_back = False                   # в этом запросе пришлось уйти на запасную модель
        self.presence = None     # Presence: команда «presence here|away|auto» из чата
        self._history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY))
        self._offset = 0
        self._task: asyncio.Task | None = None

    # ── Bot API ──

    async def _api(self, method: str, payload: dict[str, Any], timeout: float = 15) -> Any:
        r = await self.http(API.format(token=self.token, method=method), payload, {}, timeout)
        if not r.get("ok"):
            raise RuntimeError(f"Telegram {method}: {r.get('description', r)}")
        return r.get("result")

    async def send(self, text: str, chat_id: int | None = None) -> bool:
        """Написать в чат (по умолчанию во все разрешённые). True — дошло хотя бы одному."""
        targets = [chat_id] if chat_id is not None else sorted(self.chat_ids)
        ok = False
        for cid in targets:
            try:
                for part in split_text(text):
                    await self._api("sendMessage", {"chat_id": cid, "text": part})
                ok = True
            except Exception as e:
                log.warning("telegram: не отправилось в %s: %s", cid, e)
        return ok

    # ── Gemini ──

    def _system(self) -> str:
        b = self.brain
        history = b.recent_messages(HISTORY) if b else None
        return system_prompt(self.now(), self.sarcasm(), b.facts() if b else None,
                             b is not None, b is not None and b.ha is not None, history,
                             history_lines=HISTORY) + CHAT_NOTE

    async def _gemini(self, body: dict[str, Any]) -> dict:
        """Запрос к Gemini; перегрузка (429/503) обычна и коротка — пробуем ещё несколько раз."""
        models = [self.model]
        models += [m for m in (*FALLBACK_MODELS, self.fallback_model) if m and m not in models]
        attempt = 0
        while True:
            try:
                return await self.http(GEMINI.format(model=models[0]), body,
                                       {"x-goog-api-key": self.api_key}, 60)
            except RuntimeError as e:
                if not str(e).startswith(("HTTP 429", "HTTP 503")):
                    raise
                if len(models) > 1 and ("PerDay" in str(e) or "RESOURCE_EXHAUSTED" in str(e)):
                    log.warning("telegram: у модели %s кончилась квота, перехожу на %s", models[0], models[1])
                    models.pop(0)       # суточная квота: ждать бессмысленно, берём запасную модель
                    continue
                if self.fallback_model and len(models) > 1 and str(e).startswith("HTTP 503") and attempt >= 1:
                    log.warning("telegram: модель %s перегружена (503), перехожу на %s", models[0], models[1])
                    models.pop(0)       # одна повторная попытка, потом запасная модель
                    self._fell_back = True
                    attempt = 0
                    continue
                attempt += 1
                if attempt >= 5:
                    raise
                await asyncio.sleep(RETRY_GEMINI_S * attempt)

    async def ask(self, chat_id: int, text: str) -> str:
        """Текст пользователя → ответ Gemini (с вызовами функций). История хранится по чатам."""
        hist = self._history[chat_id]
        contents = ([] if self.brain is not None else list(hist)) + [{"role": "user", "parts": [{"text": text}]}]
        body: dict[str, Any] = {"systemInstruction": {"parts": [{"text": self._system()}]}}
        if self.brain is not None:
            self.brain.log_message("telegram", "user", text)    # после сборки подсказки: сама реплика в ней не нужна
        if self.brain is not None:
            body["tools"] = [{"functionDeclarations": self.brain.tools()}]
        answer = ""
        self._fell_back = False
        for _ in range(MAX_ROUNDS):
            r = await self._gemini({**body, "contents": contents})
            cands = r.get("candidates") or []
            parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            answer = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
            if not calls or self.brain is None:
                break
            contents.append({"role": "model", "parts": parts})
            results = []
            for c in calls:
                out = await self.brain.call(c.get("name", ""), c.get("args") or {}, self.device_send)
                log.info("telegram: %s(%s) → %s", c.get("name"), c.get("args"), out)
                results.append({"functionResponse": {"name": c.get("name", ""), "response": out}})
            contents.append({"role": "user", "parts": results})
        answer = answer or "Не смог ничего ответить, попробуй ещё раз."
        if self._fell_back and self._notice.take(self._clock()):
            answer = OVERLOAD_NOTE + "\n" + answer       # один раз за десять минут, не при каждом ответе
        if self.brain is not None:
            self.brain.log_message("telegram", "assistant", answer)
        else:
            hist.append({"role": "user", "parts": [{"text": text}]})
            hist.append({"role": "model", "parts": [{"text": answer}]})
        return answer

    # ── входящие ──

    async def handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if chat_id not in self.chat_ids:
            if chat_id is not None:
                log.info("telegram: чужой чат %s проигнорирован", chat_id)
            return
        if not text:
            return
        if text.split()[0].lower().lstrip("/") == "presence" and self.presence is not None:
            await self.send(await self.presence.command("presence " + " ".join(text.split()[1:])) or "", chat_id)
            return
        try:
            reply = await self.ask(chat_id, text)
        except Exception as e:
            log.warning("telegram: Gemini не ответил: %s", e)
            reply = "Не получилось спросить Gemini, попробуй ещё раз позже."
        await self.send(reply, chat_id)

    async def poll_once(self) -> None:
        updates = await self._api("getUpdates", {"offset": self._offset, "timeout": POLL_S,
                                                 "allowed_updates": ["message"]}, POLL_S + 10)
        for upd in updates or []:
            self._offset = max(self._offset, upd.get("update_id", 0) + 1)
            await self.handle_update(upd)

    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("telegram: опрос не удался: %s", e)
                await asyncio.sleep(RETRY_S)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def command(self, line: str) -> str | None:
        """Консоль: `tg <текст>` — как будто написали в Telegram, ответ печатается здесь."""
        parts = line.split(maxsplit=1)
        if not parts or parts[0].lower() != "tg":
            return None
        if len(parts) < 2:
            return "tg <текст>: спросить так же, как через Telegram"
        try:
            return await self.ask(0, parts[1])
        except Exception as e:
            return f"tg: {e}"
