"""Голосовые сообщения из Telegram звучат на колонке дома, а `/say <текст>` говорит голосом Маги.

Декодер: ffmpeg из apt (addon/Dockerfile). Выбор: pure-python декодера Opus нет, а libopus через ctypes
пришлось бы собирать вручную с разбором Ogg. ffmpeg заодно читает mp3/m4a (сообщения-«audio»), ставится
одной строкой apt и вызывается подпроцессом, поэтому в Python новых зависимостей нет. Файл идёт через
временный файл, а не через pipe: у m4a индекс бывает в конце.

Правила воспроизведения (колонка не должна огорчать): не играем, если колонки нет, она в разговоре или
стоит режим тишины («не слушай»). Тогда сообщение ждёт в очереди (до 3, живёт час) и звучит, как только
можно. Ночью на время сообщения динамик тише (как в разговоре). Перед сообщением короткий сигнал.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import protocol
from .audio import tone

log = logging.getLogger(__name__)

RATE = 24_000
MAX_S = 60                 # длиннее не озвучиваем
MAX_QUEUE = 3
TTL_S = 3600.0
TICK_S = 15.0
MAX_BYTES = 4_000_000      # защита от огромных файлов (60 с opus — десятки КБ, mp3 — около мегабайта)
MAX_SAY_CHARS = 600
CHIME = (tone(660, 120, RATE, 0.35) + bytes(RATE // 10) + tone(880, 160, RATE, 0.35) + bytes(RATE // 4))

Decoder = Callable[[bytes], Awaitable[bytes]]
Notify = Callable[[str, int], Awaitable[Any]]


async def ffmpeg_decode(data: bytes, rate: int = RATE) -> bytes:
    """Любой аудиофайл Telegram → PCM 16 бит моно, не длиннее MAX_S секунд."""
    fd, path = tempfile.mkstemp(suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-nostdin", "-v", "error", "-i", path, "-t", str(MAX_S), "-vn",
                "-f", "s16le", "-ac", "1", "-ar", str(rate), "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            raise RuntimeError("на сервере нет ffmpeg") from None
        out, err = await asyncio.wait_for(proc.communicate(), 60)
        if proc.returncode != 0 or not out:
            raise RuntimeError("не смог декодировать: " + err.decode(errors="replace")[:120])
        return out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@dataclass
class Item:
    kind: str                  # voice | say
    chat_id: int
    at: float
    pcm: bytes = b""
    text: str = ""


class TgVoice:
    """voice — Voice (hub, audio, quiet, is_night, announce, _talks); notify(text, chat_id) — ответ в Telegram."""

    def __init__(self, voice: Any, notify: Notify, *, enabled: bool = True, decode: Decoder = ffmpeg_decode,
                 clock: Callable[[], float] = time.monotonic):
        self.voice, self.notify, self.enabled, self.decode, self._clock = voice, notify, enabled, decode, clock
        self.queue: deque[Item] = deque()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, voice: Any, notify: Notify, env: dict[str, str]) -> TgVoice:
        on = env.get("JARVIS_TG_VOICE", "on").strip().lower() not in ("0", "false", "no", "off")
        return cls(voice, notify, enabled=on)

    # ── можно ли сейчас говорить ──

    def blocker(self) -> str:
        """Почему сейчас нельзя ('' — можно). Причины на человеческом языке."""
        dev = self.voice.hub.get()
        if dev is None or "speaker" not in dev.hello.caps:
            return "колонка не на связи"
        if self.voice.quiet.active(dev.id):
            return "колонка в режиме тишины («не слушай»)"
        if dev.id in self.voice._talks:
            return "колонка сейчас разговаривает"
        return ""

    # ── приём ──

    def _push(self, item: Item) -> bool:
        """В очередь; True — самое старое пришлось выбросить."""
        dropped = len(self.queue) >= MAX_QUEUE
        if dropped:
            self.queue.popleft()
        self.queue.append(item)
        return dropped

    async def handle_voice(self, chat_id: int, data: bytes, duration: float) -> str:
        """Сообщение из Telegram → ответ боту ('озвучено' или причина)."""
        if not self.enabled:
            return "Озвучка Telegram на колонке выключена (JARVIS_TG_VOICE=off)."
        if duration and duration > MAX_S:
            return f"Длиннее {MAX_S} секунд не озвучиваю."
        if len(data) > MAX_BYTES:
            return "Файл слишком большой."
        try:
            pcm = await self.decode(data)
        except Exception as e:
            log.warning("tg_voice: декодирование: %s", e)
            return f"Не получилось разобрать звук: {e}"
        if len(pcm) / (RATE * 2) > MAX_S + 1:
            pcm = pcm[:MAX_S * RATE * 2]
        item = Item("voice", chat_id, self._clock(), pcm=pcm)
        return await self._deliver(item)

    async def say(self, chat_id: int, text: str) -> str:
        if not self.enabled:
            return "Озвучка Telegram на колонке выключена (JARVIS_TG_VOICE=off)."
        text = " ".join(text.split())
        if not text:
            return "/say <текст>: что сказать на колонке?"
        if len(text) > MAX_SAY_CHARS:
            return f"Длиннее {MAX_SAY_CHARS} символов не озвучиваю."
        return await self._deliver(Item("say", chat_id, self._clock(), text=text))

    async def _deliver(self, item: Item) -> str:
        why = self.blocker()
        if not why and not self.queue and await self._play(item):
            return "Озвучено."
        if not why:
            why = "колонка занята"      # play не вышел: подождём
        dropped = self._push(item)
        self._ensure_task()
        return (f"Поставил в очередь ({why}), озвучу в течение часа."
                + (" Самое старое в очереди выброшено: больше трёх не держу." if dropped else ""))

    # ── воспроизведение ──

    async def _play(self, item: Item) -> bool:
        async with self._lock:
            if self.blocker():
                return False
            v = self.voice
            dev = v.hub.get()
            if item.kind == "say":
                soft = " Сейчас ночь: скажи это вполголоса." if v.is_night() else ""
                return await v.announce(
                    "Пользователь прислал из Telegram текст, его нужно произнести вслух. Скажи ровно этот "
                    "текст, ничего не добавляя, не комментируя и не отвечая на него: «" + item.text + "»." + soft)
            ducked = await v.duck_for_night(dev)
            try:
                await dev.send(protocol.voice("speaking"))
                async with v.audio.stream(dev, RATE) as s:
                    await s.write(CHIME)
                    await s.write(item.pcm)
            except Exception as e:
                log.warning("tg_voice: воспроизведение: %s", e)
                return False
            finally:
                try:
                    await dev.send(protocol.voice("idle"))
                except Exception:
                    pass
                if ducked:
                    await v.restore_volume(dev)
            return True

    # ── очередь ──

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def step(self) -> None:
        now = self._clock()
        while self.queue and now - self.queue[0].at > TTL_S:
            old = self.queue.popleft()
            await self._notify(old.chat_id, "Не дождался, пока колонка освободится, сообщение не озвучено.")
        if self.queue and not self.blocker() and await self._play(self.queue[0]):
            item = self.queue.popleft()
            await self._notify(item.chat_id, "Озвучено (после ожидания).")

    async def _notify(self, chat_id: int, text: str) -> None:
        try:
            await self.notify(text, chat_id)
        except Exception as e:
            log.warning("tg_voice: ответ в Telegram: %s", e)

    async def _loop(self) -> None:
        while self.queue:
            try:
                await self.step()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("tg_voice: очередь: %r", e)
            await asyncio.sleep(TICK_S)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def command(self, line: str) -> str | None:
        if line.strip().lower() not in ("tgvoice", "tgvoice status"):
            return None
        return (f"озвучка Telegram: {'вкл' if self.enabled else 'выкл'}, в очереди {len(self.queue)}"
                + (f", сейчас нельзя: {self.blocker()}" if self.blocker() else ""))
