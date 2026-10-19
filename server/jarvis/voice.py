"""Разговор с колонкой: слово пробуждения на сервере → сессия Gemini Live → ответ голосом.

Микрофон колонки слушается постоянно. Пока разговора нет, звук идёт в детектор слова
(jarvis.wake). Услышали — открываем сессию Gemini Live и шлём туда звук; Gemini сам понимает,
где фраза кончилась, и отвечает голосом, который сразу уходит на динамик. Пока колонка
говорит, микрофон в Gemini не идёт: эхо собственного ответа он принял бы за перебивание.
После ответа ещё FOLLOW_UP_S слушаем без слова пробуждения — можно продолжить разговор.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import protocol
from .audio import Audio
from .brain import Brain
from .config import Settings
from .devices import Device, DeviceHub
from .gemini_live import Event, LiveError, LiveSession

log = logging.getLogger(__name__)

NO_SPEECH_S = 8.0       # после слова (или после ответа) никто не заговорил — закончить
FOLLOW_UP_S = 6.0       # после ответа слушаем без слова пробуждения
MAX_TURN_S = 60.0       # один ответ дольше этого — что-то не так
WAKE_COOLDOWN_S = 1.5   # после разговора детектор не слушает: хвост эха
CAPTION_HOLD_S = 4.0    # подпись висит после конца разговора

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")


def system_prompt(now: datetime, sarcasm: int = 60, facts: list[str] | None = None,
                  tools: bool = False, home: bool = False) -> str:
    tone = ("Характер сдержанный, но с острым умом: можешь едко пошутить или подколоть, "
            "особенно когда пользователь сам напрашивается." if sarcasm >= 45 else
            "Характер спокойный и доброжелательный, без подколов.")
    abilities = (
        "Ты умеешь запоминать факты о пользователе (remember), ставить напоминания и таймеры. "
        "Делай это сразу функцией, а не обещай; после вызова коротко подтверди словами. "
        + ("Ты полностью управляешь умным домом: свет (цвет, яркость, эффекты), розетки, климат, плееры, замки, сцены, датчики (home_set, home_service, home_state, home_list). "
           "Когда просят включить или выключить — делай сразу, не переспрашивай, если ясно что. Никогда не отказывай заранее, что устройство чего-то не умеет: вызови функцию и перескажи ответ — если она вернула ошибку, объясни её причину своими словами. "
           "Телефоном управлять пока не умеешь." if home else
           "Светом и телефоном управлять пока не умеешь — честно скажи, что это скоро появится.")
        if tools else
        "Управлять светом, напоминаниями и телефоном ты пока не умеешь — "
        "если просят, честно скажи, что это скоро появится.")
    memory = ("\nТы помнишь о пользователе:\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""
    return f"""\
Ты голосовой ассистент в колонке с круглым экраном. Колонка стоит в спальне пользователя, у
кровати и компьютера, в Вене. Сейчас {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} \
{now.year}, {now:%H:%M}.

Говори только по-русски, даже если тебе послышался другой язык (микрофон в колонке
простой, речь иногда звучит неразборчиво). Коротко: обычно одно-два предложения, как в живом
разговоре. Если не расслышал — так и скажи, не додумывай. Ответ звучит
вслух — никаких списков, разметки, ссылок и смайлов; числа и время говори словами так, как
их произносят. {tone}

Имени у тебя пока нет. «Hey Jarvis» — просто временное слово, которым тебя зовут, ты не
Джарвис и не изображаешь его. {abilities}
{memory}"""


def tail_bytes(text: str, max_bytes: int = 120) -> str:
    """Конец строки, который влезает в подпись колонки (байты UTF-8)."""
    data = text.encode()
    if len(data) <= max_bytes:
        return text
    return "…" + data[-(max_bytes - 3):].decode(errors="ignore").lstrip()


class Conversation:
    """Один разговор с одной колонкой: от слова пробуждения до тишины."""

    def __init__(self, voice: Voice, dev: Device):
        self.voice = voice
        self.dev = dev
        self.mic: asyncio.Queue[bytes] = asyncio.Queue()
        self.listening = True     # микрофон идёт в Gemini

    def feed(self, pcm: bytes) -> None:
        if self.listening:
            self.mic.put_nowait(pcm)

    async def _pump_mic(self, live: LiveSession) -> None:
        while True:
            pcm = await self.mic.get()
            await live.send_audio(pcm)

    @staticmethod
    async def _read(live: LiveSession, events: asyncio.Queue) -> None:
        try:
            async for ev in live.events():
                await events.put(ev)
        except LiveError as e:
            log.warning("%s", e)
        finally:
            await events.put(None)

    async def _tools(self, live: LiveSession, ev: Event) -> None:
        """Выполнить функции, которые позвал Gemini, и вернуть ему результаты."""
        brain = self.voice.brain
        results = []
        for call_id, name, args in ev.calls:
            out = await brain.call(name, args, self.dev.send) if brain else {"ok": False, "error": "нет инструментов"}
            log.info("%s: %s(%s) → %s", self.dev.id, name, args, out)
            results.append((call_id, name, out))
        await live.send_tool_results(results)

    async def _speak(self, first: Event, events: asyncio.Queue, live: LiveSession) -> Event | None:
        """Проиграть ответ: первый кусок уже пришёл, остальные — до turn_complete.
        Возвращает событие, на котором ответ кончился (None — сессия закрылась)."""
        audio = self.voice.audio
        end: Event | None = None
        async with audio.stream(self.dev, first.rate) as stream:
            await stream.write(first.data)
            deadline = self.voice.clock() + MAX_TURN_S
            while True:
                try:
                    ev = await asyncio.wait_for(events.get(), max(0.1, deadline - self.voice.clock()))
                except TimeoutError:
                    log.warning("ответ Gemini затянулся — обрываю")
                    await stream.stop()
                    return None
                if ev is None or ev.kind in ("turn_complete", "interrupted", "go_away"):
                    end = ev
                    if ev is not None and ev.kind == "interrupted":
                        await stream.stop()
                    break
                if ev.kind == "audio":
                    await stream.write(ev.data)
                elif ev.kind == "tool_call":
                    await self._tools(live, ev)
                elif ev.kind == "said":
                    self.voice.reply_text += ev.text
        return end

    async def run(self, text: str | None = None) -> None:
        dev, v = self.dev, self.voice
        heard = ""
        await dev.send(protocol.voice("thinking" if text else "listening"))
        try:
            facts = v.brain.facts() if v.brain else None
            async with v.live_factory(system_prompt(v.now(), v.sarcasm(), facts, v.brain is not None,
                                                 v.brain is not None and v.brain.ha is not None)) as live:
                pump = asyncio.create_task(self._pump_mic(live))
                events: asyncio.Queue[Event | None] = asyncio.Queue()
                reader = asyncio.create_task(self._read(live, events))
                try:
                    v.transcript = [("текстом", text)] if text else []
                    if text:
                        self.listening = False
                        await live.send_text(text)
                    log.info("%s: разговор начат (%s)", dev.id, "текстом" if text else "по слову")
                    deadline = v.clock() + NO_SPEECH_S
                    why = ""
                    while True:
                        try:
                            ev = await asyncio.wait_for(events.get(), max(0.05, deadline - v.clock()))
                        except TimeoutError:
                            why = "тишина"
                            break
                        if ev is None or ev.kind == "go_away":
                            why = "Gemini закрыл сессию" if ev is None else "Gemini попросил переподключиться"
                            break
                        if ev.kind == "tool_call":
                            await self._tools(live, ev)
                            deadline = v.clock() + NO_SPEECH_S
                        elif ev.kind == "heard":
                            heard += ev.text
                            deadline = v.clock() + NO_SPEECH_S
                            await dev.send(protocol.caption("heard", tail_bytes(heard.strip())))
                        elif ev.kind == "audio":
                            # Gemini отвечает: микрофон в сторону, иначе он услышит сам себя
                            self.listening = False
                            await live.audio_stream_end()
                            if heard.strip():
                                await dev.send(protocol.caption("said", tail_bytes(heard.strip())))
                                log.info("%s: «%s»", dev.id, heard.strip())
                                v.transcript.append(("я", heard.strip()))
                            await dev.send(protocol.voice("speaking"))
                            v.reply_text = ""
                            end = await self._speak(ev, events, live)
                            log.info("%s ← «%s»", dev.id, v.reply_text.strip())
                            v.transcript.append(("колонка", v.reply_text.strip()))
                            if end is None or end.kind == "go_away" or "mic" not in dev.hello.caps:
                                why = "ответ без продолжения"
                                break
                            # продолжение разговора без слова пробуждения
                            heard = ""
                            while not self.mic.empty():
                                self.mic.get_nowait()
                            self.listening = True
                            await dev.send(protocol.voice("listening"))
                            deadline = v.clock() + FOLLOW_UP_S
                finally:
                    pump.cancel()
                    reader.cancel()
                log.info("%s: разговор окончен (%s)", dev.id, why)
        except LiveError as e:
            log.warning("Gemini Live: %s", e)
            await dev.send(protocol.emotion("sorry", 3000))
            await dev.send(protocol.caption("reply", "Не могу связаться с Gemini"))
        finally:
            self.listening = False
            await dev.send(protocol.voice("idle"))


class Voice:
    def __init__(self, hub: DeviceHub, audio: Audio, settings: Settings, *,
                 wake_factory: Callable[[], Any] | None = None,
                 brain: Brain | None = None,
                 live_factory: Callable[[str], LiveSession] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        self.hub = hub
        self.audio = audio
        self.settings = settings
        self.clock = clock
        self.now = now
        self._wake_factory = wake_factory
        self.brain = brain
        self.live_factory = live_factory or (lambda system: LiveSession(
            settings.gemini_api_key, settings.gemini_model, settings.voice_name, system,
            tools=brain.tools() if brain else None))
        self._detectors: dict[str, Any] = {}
        self._talks: dict[str, Conversation] = {}
        self._tasks: set[asyncio.Task] = set()
        self._quiet_until: dict[str, float] = {}
        self.scores: deque[tuple[float, float]] = deque(maxlen=600)   # (время, оценка) ~1 мин
        self.reply_text = ""
        self.transcript: list[tuple[str, str]] = []   # последний разговор: (кто, что)
        hub.voice = self
        hub.on_connect(self._on_connect)
        hub.on_audio(self._on_audio)

    def sarcasm(self) -> int:
        bridge = self.hub.bridge
        return int(bridge.store.values.get("sarcasm", 60)) if bridge is not None else 60

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _on_connect(self, dev: Device) -> None:
        if self._wake_factory is None or "mic" not in dev.hello.caps:
            return
        if dev.id not in self._detectors:
            self._detectors[dev.id] = await asyncio.to_thread(self._wake_factory)
        else:
            self._detectors[dev.id].reset()
        self.audio.keep_listening.add(dev.id)
        await dev.send(protocol.listen_start(self.audio.mic_gain))
        log.info("%s: слушаю слово пробуждения", dev.id)

    def _on_audio(self, dev: Device, pcm: bytes) -> None:
        talk = self._talks.get(dev.id)
        if talk is not None:
            talk.feed(pcm)
            return
        det = self._detectors.get(dev.id)
        if det is None or self.clock() < self._quiet_until.get(dev.id, 0.0):
            return
        score = det.process(pcm)
        self.scores.append((self.clock(), score))
        if score >= self.settings.wake_threshold:
            log.info("%s: слово пробуждения (%.2f)", dev.id, score)
            self.start(dev)

    def start(self, dev: Device, text: str | None = None) -> asyncio.Task | None:
        """Начать разговор: по слову пробуждения или текстом (команда ask)."""
        if dev.id in self._talks:
            return None
        talk = self._talks[dev.id] = Conversation(self, dev)
        return self._spawn(self._run(talk, text))

    async def remind(self, text: str) -> bool:
        """Сказать напоминание вслух. False — колонки нет или она занята, повторим позже."""
        dev = self.hub.get()
        if dev is None or "speaker" not in dev.hello.caps:
            return False
        task = self.start(dev, f"Сработало напоминание: «{text}». Скажи пользователю об этом "
                               "одной короткой фразой, как будто сам вспомнил.")
        return task is not None

    async def _run(self, talk: Conversation, text: str | None) -> None:
        dev = talk.dev
        try:
            await talk.run(text)
        except Exception:
            log.exception("разговор с %s упал", dev.id)
        finally:
            del self._talks[dev.id]
            self._quiet_until[dev.id] = self.clock() + WAKE_COOLDOWN_S
            if dev.id in self._detectors:
                self._detectors[dev.id].reset()
            self._spawn(self._clear_caption(dev))

    async def _clear_caption(self, dev: Device) -> None:
        await asyncio.sleep(CAPTION_HOLD_S)
        if dev.id not in self._talks:
            try:
                await dev.send(protocol.caption("none"))
            except Exception:
                pass

    # ── команды консоли ──

    async def command(self, line: str) -> str | None:
        parts = line.split(maxsplit=1)
        if not parts or parts[0].lower() not in ("ask", "wake", "last"):
            return None
        if parts[0].lower() == "last":
            return "\n".join(f"{who}: {what}" for who, what in self.transcript) or "разговоров ещё не было"
        if parts[0].lower() == "wake":
            recent = [s for t, s in self.scores if t > self.clock() - 60]
            state = "слушает" if self._detectors else "выключено (нет колонки с микрофоном или моделей)"
            return (f"слово пробуждения: {state}, порог {self.settings.wake_threshold}, "
                    f"максимум за минуту {max(recent, default=0):.2f}")
        if len(parts) < 2:
            return "ask <вопрос> — спросить Gemini, ответ голосом на колонке"
        if not self.settings.gemini_api_key:
            return "нет GEMINI_API_KEY в .env — голоса нет"
        dev = self.hub.get()
        if dev is None:
            return "колонок нет"
        if "speaker" not in dev.hello.caps:
            return f"{dev.id}: прошивка без динамика"
        task = self.start(dev, parts[1])
        if task is None:
            return "колонка уже разговаривает"
        await task
        return "\n".join(f"{who}: {what}" for who, what in self.transcript) or "(без ответа)"
