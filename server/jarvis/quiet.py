"""Ночная громкость и режим тишины «не слушай» (только голосом: mute_listening).

Ночью динамик на время разговора тише (громкость пользователя в настройках не трогаем, после разговора
возвращаем её же). Режим тишины: Gemini зовёт mute_listening(minutes), сервер шлёт колонке
`quiet {on:true}` и перестаёт отдавать звук Gemini совсем.

Выбор, как выйти из тишины. Ждать только таймер мало: «Мага, вернись» должно работать. Поэтому в тишине
сервер сам слушает маленьким локальным распознавателем Vosk только два слова — «мага» и «вернись»
(грамматика из двух слов + приманки, как у слова пробуждения; в Gemini звук не уходит). «Вернись» снимает
тишину; «мага» получает короткий ответ «я молчу, скажи вернись»: фразу озвучивает Gemini по ТЕКСТОВОЙ
подсказке, микрофон при этом закрыт (one_shot). Без модели Vosk выйти можно только по таймеру, консоли
(`mute off`) или Telegram. Оговорка по контракту (protocol-next.md): если прошивка в `quiet` совсем
перестаёт слать звук, слов «вернись» сервер не услышит; прошивке стоит продолжать слать звук серверу.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import protocol
from .wake_vosk import DECOYS, RATE, _models, ensure_model, normalize

log = logging.getLogger(__name__)

NIGHT_VOLUME_FACTOR = 0.45     # ночью громкость = доля дневной, но не ниже NIGHT_VOLUME_MIN
NIGHT_VOLUME_MIN = 8
MIN_MUTE_MIN = 1.0
MAX_MUTE_MIN = 480.0
DEFAULT_MUTE_MIN = 60.0
PROMPT_EVERY_S = 20.0          # «я молчу» на слово «мага» — не чаще
RESUME_WORD = "вернись"
WAKE_WORD = "мага"
RESUME_DECOYS = ("верь", "верно", "верни", "верный", "вернуть", "вернулись")

MUTED_PROMPT = "Скажи ровно одну короткую фразу: «Я молчу. Скажи: Мага, вернись»."
BACK_PROMPT = "Скажи одним коротким словом, что снова слушаешь («Слушаю»)."


def night_volume(day: int) -> int:
    """Тихая громкость на ночь: доля дневной, не громче дневной."""
    if day <= 0:
        return 0
    return min(day, max(NIGHT_VOLUME_MIN, round(day * NIGHT_VOLUME_FACTOR)))


def quiet_msg(on: bool) -> dict[str, Any]:
    return {"type": "quiet", "on": bool(on)}


class VoskWords:
    """Потоковый распознаватель с грамматикой из пары слов: process() → найденное слово или ''."""

    def __init__(self, model_dir: Path | str, words: tuple[str, ...] = (WAKE_WORD, RESUME_WORD), *,
                 recognizer_factory=None):
        self.words = tuple(normalize(" ".join(words)))
        grammar = json.dumps([*self.words, *(d for d in (*DECOYS, *RESUME_DECOYS) if d not in self.words),
                              "[unk]"], ensure_ascii=False)
        if recognizer_factory is None:
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            key = str(model_dir)
            if key not in _models:
                _models[key] = Model(key)
            recognizer_factory = lambda: KaldiRecognizer(_models[key], RATE, grammar)  # noqa: E731
        self._factory = recognizer_factory
        self.reset()

    def reset(self) -> None:
        self._rec = self._factory()

    def process(self, pcm: bytes) -> str:
        if self._rec.AcceptWaveform(pcm):
            text = json.loads(self._rec.Result()).get("text", "")
        else:
            text = json.loads(self._rec.PartialResult()).get("partial", "")
        for w in normalize(text):
            if w in self.words:
                self.reset()
                return w
        return ""


async def make_mute_factory(model_dir: Path) -> Callable[[], Any] | None:
    """Фабрика локального слушателя для тишины; нет модели или vosk — None (выход только по таймеру)."""
    try:
        path = await asyncio.to_thread(ensure_model, model_dir)
        return lambda: VoskWords(path)
    except Exception as e:
        log.warning("слушатель «вернись» в тишине выключен: %s", e)
        return None


class _State:
    def __init__(self, until: float, listener: Any, task: asyncio.Task):
        self.until, self.listener, self.task = until, listener, task
        self.last_prompt = -1e18


class Quiet:
    """Режим тишины по колонкам. voice — Voice (clock, hub, audio, start, _spawn, _prewarm)."""

    def __init__(self, voice: Any, listener_factory: Callable[[], Any] | None = None):
        self.voice = voice
        self.listener_factory = listener_factory
        self.states: dict[str, _State] = {}

    def active(self, dev_id: str) -> bool:
        return dev_id in self.states

    def left(self, dev_id: str) -> float:
        st = self.states.get(dev_id)
        return max(st.until - self.voice.clock(), 0.0) if st else 0.0

    async def start(self, dev, seconds: float) -> None:
        old = self.states.pop(dev.id, None)
        if old is not None:
            old.task.cancel()
        listener = None
        if self.listener_factory is not None:
            listener = await asyncio.to_thread(self.listener_factory)
        task = self.voice._spawn(self._expire(dev, seconds))
        self.states[dev.id] = _State(self.voice.clock() + seconds, listener, task)
        log.info("%s: тишина на %.0f мин (слушаю только «%s»)", dev.id, seconds / 60,
                 RESUME_WORD if listener else "— (нет Vosk, только таймер)")
        await self._send(dev, quiet_msg(True))

    async def _expire(self, dev, seconds: float) -> None:
        await asyncio.sleep(seconds)
        await self.stop(dev, "время вышло")

    async def stop(self, dev, why: str = "", say: bool = False) -> bool:
        st = self.states.pop(dev.id, None)
        if st is None:
            return False
        if st.task is not asyncio.current_task():
            st.task.cancel()
        log.info("%s: тишина снята (%s)", dev.id, why or "консоль")
        await self._send(dev, quiet_msg(False))
        if "mic" in dev.hello.caps:
            await self._send(dev, protocol.listen_start(self.voice.audio.mic_gain))
        det = self.voice._detectors.get(dev.id)
        if det is not None:
            det.reset()
        if self.voice._detectors:
            self.voice._prewarm()
        if say:
            self.voice.start(dev, BACK_PROMPT, service=True, one_shot=True)
        return True

    async def restore(self, dev) -> None:
        """Колонка переподключилась в тишине: listen_start снимает quiet, повторяем."""
        if dev.id in self.states:
            await self._send(dev, quiet_msg(True))

    @staticmethod
    async def _send(dev, msg) -> None:
        try:
            await dev.send(msg)
        except Exception as e:
            log.debug("%s: %s не доставлено: %r", dev.id, msg.get("type"), e)

    def on_audio(self, dev, pcm: bytes) -> bool:
        """True — кадр съеден режимом тишины (в Gemini и в детектор слова он не идёт)."""
        st = self.states.get(dev.id)
        if st is None:
            return False
        if dev.id in self.voice._talks or st.listener is None:
            return True
        word = st.listener.process(pcm)
        if word == RESUME_WORD:
            self.voice._spawn(self.stop(dev, "голосом", say=True))
        elif word == WAKE_WORD and self.voice.clock() - st.last_prompt > PROMPT_EVERY_S:
            st.last_prompt = self.voice.clock()
            self.voice.start(dev, MUTED_PROMPT, service=True, one_shot=True)
        return True

    # ── инструмент Gemini и консоль ──

    async def mute_tool(self, minutes: Any) -> dict:
        dev = self.voice.hub.get()
        if dev is None:
            return {"ok": False, "error": "колонки нет на связи"}
        try:
            mins = float(minutes) if minutes is not None else DEFAULT_MUTE_MIN
        except (TypeError, ValueError):
            mins = DEFAULT_MUTE_MIN
        mins = min(max(mins, MIN_MUTE_MIN), MAX_MUTE_MIN)
        await self.start(dev, mins * 60)
        return {"ok": True, "minutes": round(mins),
                "note": "Скажи одной короткой фразой, что замолкаешь, и что вернуть можно словами "
                        "«Мага, вернись». Больше ничего не говори."}

    async def command(self, args: list[str]) -> str:
        dev = self.voice.hub.get()
        if dev is None:
            return "колонок нет"
        if args and args[0] == "off":
            return "тишина снята" if await self.stop(dev, "консоль") else "тишины и не было"
        if not args:
            return (f"тишина ещё {self.left(dev.id) / 60:.0f} мин" if self.active(dev.id)
                    else "mute <минуты>|off — режим «не слушай» (сейчас выкл)")
        try:
            mins = float(args[0])
        except ValueError:
            return "mute <минуты>|off"
        return str((await self.mute_tool(mins))["minutes"]) + " мин тишины"
