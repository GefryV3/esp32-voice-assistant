"""Звук колонок: запись с микрофона и воспроизведение на динамике. Протокол — docs/protocol.md,
раздел «Звук». Здесь же команды консоли для проверки: rec, echo, beep, play, stop, gain.
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import time
import wave
from array import array
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import protocol
from .devices import Device, DeviceHub
from .protocol import AUDIO_RATE, Message

log = logging.getLogger(__name__)

PLAY_CHUNK_S = 0.1         # звук на колонку уходит кусками по 100 мс
PLAY_LEAD_S = 1.0          # и не больше чем на 1 с впереди реального времени (буфер колонки — 4 с)
PLAYED_SLACK_S = 5.0       # сколько ждать played сверх длины звука
MAX_REC_S = 60

Reply = str | tuple[str, bytes]   # ответ команды: текст или текст + WAV


# ── PCM и WAV ────────────────────────────────────────────────────────────────

def wav_bytes(pcm: bytes, rate: int = AUDIO_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _to_int16(frames: bytes, width: int) -> array:
    if width == 2:
        return array("h", frames)
    if width == 1:   # 8 бит в WAV — беззнаковые
        return array("h", ((b - 128) << 8 for b in frames))
    if width in (3, 4):  # старшие 16 бит знакового little-endian сэмпла
        return array("h", (int.from_bytes(frames[i + width - 2:i + width], "little", signed=True)
                           for i in range(0, len(frames), width)))
    raise ValueError(f"WAV с {width * 8} битами на сэмпл не поддерживается")


def read_wav(data: bytes) -> tuple[bytes, int]:
    """WAV любой разрядности → (PCM 16 бит моно, частота). Стерео сводится в моно."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getcomptype() != "NONE":
            raise ValueError("нужен несжатый WAV (PCM)")
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        samples = _to_int16(w.readframes(w.getnframes()), width)
    if channels > 1:
        samples = array("h", (sum(samples[i:i + channels]) // channels
                              for i in range(0, len(samples) - channels + 1, channels)))
    if not 8000 <= rate <= 48_000:
        raise ValueError(f"частота {rate} Гц — колонка играет 8…48 кГц")
    return samples.tobytes(), rate


def tone(freq: float = 880, ms: int = 300, rate: int = AUDIO_RATE, level: float = 0.5) -> bytes:
    """Синус с плавными краями по 10 мс, чтобы не щёлкало."""
    n = rate * ms // 1000
    fade = max(1, rate // 100)
    amp = 32767 * level
    return array("h", (int(amp * math.sin(2 * math.pi * freq * i / rate) * min(1.0, i / fade, (n - i) / fade))
                       for i in range(n))).tobytes()


def levels(pcm: bytes) -> tuple[float, float, int]:
    """Громкость записи: (RMS в дБFS, пик в дБFS, сколько сэмплов упёрлось в потолок)."""
    s = array("h", pcm[: len(pcm) // 2 * 2])
    if not s:
        return -math.inf, -math.inf, 0
    rms = math.sqrt(sum(x * x for x in s) / len(s))
    peak = max(abs(min(s)), max(s))
    clipped = sum(1 for x in s if x >= 32767 or x <= -32768)

    def db(v: float) -> float:
        return 20 * math.log10(v / 32768) if v > 0 else -math.inf
    return db(rms), db(peak), clipped


# ── звук колонок ─────────────────────────────────────────────────────────────

class Stream:
    """Один непрерывный звук на колонке. Темп: держим колонку не больше чем на PLAY_LEAD_S
    впереди. Где колонка сейчас, считаем сами: если данные запаздывали, она доиграла буфер и
    ждёт — тогда новое начнёт играть сразу, «сейчас»."""

    def __init__(self, audio: Audio, dev: Device, rate: int):
        self._audio = audio
        self.dev = dev
        self.rate = rate
        self._lock = audio._locks.setdefault(dev.id, asyncio.Lock())
        self._fut: asyncio.Future | None = None
        self._ends_at = 0.0          # когда колонка доиграет всё отправленное (по нашим часам)
        self._chunk = int(rate * 2 * PLAY_CHUNK_S) // 2 * 2
        self._stopped = False

    async def __aenter__(self) -> Stream:
        await self._lock.acquire()
        try:
            self._fut = self._audio._played[self.dev.id] = asyncio.get_running_loop().create_future()
            await self.dev.send(protocol.audio_start(self.rate))
        except BaseException:
            self._audio._played.pop(self.dev.id, None)
            self._lock.release()
            raise
        self._ends_at = self._audio._clock()
        return self

    async def write(self, pcm: bytes) -> None:
        clock = self._audio._clock
        for off in range(0, len(pcm), self._chunk):
            if self._stopped:
                return
            ahead = self._ends_at - clock()
            if ahead > PLAY_LEAD_S:
                await asyncio.sleep(ahead - PLAY_LEAD_S)
            piece = pcm[off:off + self._chunk]
            await self.dev.ws.send(piece)
            self._ends_at = max(self._ends_at, clock()) + len(piece) / (self.rate * 2)

    async def stop(self) -> None:
        """Замолчать сразу (перебили): колонка выбросит буфер."""
        if not self._stopped:
            self._stopped = True
            await self.dev.send(protocol.audio_stop())

    async def __aexit__(self, exc_type, *exc: object) -> None:
        try:
            if exc_type is not None and not self._stopped:
                self._stopped = True
                await self.dev.send(protocol.audio_stop())
            elif not self._stopped:
                await self.dev.send(protocol.audio_end())
            left = max(0.0, self._ends_at - self._audio._clock())
            await asyncio.wait_for(asyncio.shield(self._fut), (0 if self._stopped else left) + PLAYED_SLACK_S)
        except TimeoutError:
            log.warning("%s не ответила played — считаю, что доиграла", self.dev.id)
        except Exception:
            pass   # колонка отключилась — ждать нечего
        finally:
            self._audio._played.pop(self.dev.id, None)
            self._lock.release()


class Audio:
    def __init__(self, hub: DeviceHub, data_dir: Path | str, *, mic_gain: int = 12,
                 clock: Callable[[], float] = time.monotonic):
        self.hub = hub
        self.recordings = Path(data_dir) / "recordings"
        self.mic_gain = mic_gain
        self._clock = clock
        self._rec: dict[str, bytearray] = {}
        self._played: dict[str, asyncio.Future] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.keep_listening: set[str] = set()   # колонки, чей микрофон нужен постоянно (voice)
        hub.audio = self
        hub.on_audio(self._on_audio)
        hub.on_message(self._on_message)

    def _on_audio(self, dev: Device, data: bytes) -> None:
        buf = self._rec.get(dev.id)
        if buf is not None:
            buf += data

    async def _on_message(self, dev: Device, msg: Message) -> None:
        if msg["type"] == "played":
            fut = self._played.get(dev.id)
            if fut is not None and not fut.done():
                fut.set_result(None)

    # ── микрофон ──

    async def record(self, dev: Device, seconds: float) -> bytes:
        """Записать seconds секунд с микрофона колонки, PCM 16 кГц 16 бит моно."""
        if dev.id in self._rec:
            raise RuntimeError(f"{dev.id} уже записывает")
        buf = self._rec[dev.id] = bytearray()
        try:
            await dev.send(protocol.listen_start(self.mic_gain))
            await asyncio.sleep(seconds)
        finally:
            del self._rec[dev.id]
            if dev.id not in self.keep_listening:   # слово пробуждения слушает дальше
                try:
                    await dev.send(protocol.listen_stop())
                except Exception:
                    pass
        return bytes(buf)

    # ── динамик ──

    async def play(self, dev: Device, pcm: bytes, rate: int = AUDIO_RATE) -> None:
        """Проиграть PCM 16 бит моно и дождаться, пока колонка доиграет."""
        async with self.stream(dev, rate) as s:
            await s.write(pcm)

    def stream(self, dev: Device, rate: int = AUDIO_RATE) -> Stream:
        """Звук по мере поступления (ответ Gemini): async with audio.stream(dev, 24000) as s:
        await s.write(...) сколько угодно раз; на выходе — дождаться, пока колонка доиграет."""
        return Stream(self, dev, rate)

    async def stop(self, dev: Device) -> None:
        await dev.send(protocol.audio_stop())

    # ── команды консоли ──

    def _save(self, pcm: bytes) -> tuple[str, bytes]:
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".wav"
        wav = wav_bytes(pcm)
        try:
            self.recordings.mkdir(parents=True, exist_ok=True)
            (self.recordings / name).write_bytes(wav)
        except OSError as e:
            log.warning("запись не сохранилась: %s", e)
        return name, wav

    @staticmethod
    def _describe(pcm: bytes, seconds: float) -> str:
        rms, peak, clipped = levels(pcm)
        got = len(pcm) / (AUDIO_RATE * 2)
        lost = f", потеряно {seconds - got:.1f} с" if seconds - got > max(0.2, seconds * 0.05) else ""
        return f"{got:.1f} с{lost}; громкость {rms:.0f} дБFS, пик {peak:.0f} дБFS" + (
            f", упёрлось в потолок {clipped} сэмплов — убавь gain" if clipped else "")

    async def command(self, line: str) -> Reply | None:
        """Звуковые команды консоли; None — это не они."""
        parts = line.split()
        if not parts:
            return None
        cmd, args = parts[0].lower(), parts[1:]
        if cmd not in ("rec", "echo", "beep", "play", "stop", "gain"):
            return None
        if cmd == "gain":
            if len(args) != 1 or not args[0].isdigit() or not 0 <= int(args[0]) <= 30:
                return f"gain 0…30 (дБ), сейчас {self.mic_gain}"
            self.mic_gain = int(args[0])
            return f"усиление микрофона {self.mic_gain} дБ"

        dev = self.hub.get()
        if dev is None:
            return "колонок нет"
        need = "speaker" if cmd in ("beep", "play", "stop") else "mic"
        if need not in dev.hello.caps:
            return f"{dev.id}: прошивка без «{need}» — перепрошей колонку"
        try:
            match cmd:
                case "rec" | "echo":
                    seconds = float(args[0]) if args else 5.0
                    if not 0.5 <= seconds <= MAX_REC_S:
                        return f"{cmd} 0.5…{MAX_REC_S} (секунд)"
                    await dev.send(protocol.voice("listening"))
                    pcm = await self.record(dev, seconds)
                    name, wav = self._save(pcm)
                    text = f"запись {name}: {self._describe(pcm, seconds)}"
                    if cmd == "echo" and pcm and "speaker" in dev.hello.caps:
                        await dev.send(protocol.voice("speaking"))
                        await self.play(dev, pcm)
                        text += "\n→ проиграл на колонке"
                    await dev.send(protocol.voice("idle"))
                    return text, wav
                case "beep":
                    freq = float(args[0]) if args else 880.0
                    ms = int(args[1]) if len(args) > 1 else 300
                    if not 50 <= freq <= 8000 or not 20 <= ms <= 10_000:
                        return "beep [50…8000 Гц] [20…10000 мс]"
                    started = self._clock()
                    await self.play(dev, tone(freq, ms))
                    return f"→ {dev.id}: тон {freq:.0f} Гц {ms} мс, доиграла за {self._clock() - started:.2f} с"
                case "play":
                    if not args:
                        return "play <файл.wav на сервере>"
                    pcm, rate = read_wav(Path(" ".join(args)).read_bytes())
                    await self.play(dev, pcm, rate)
                    return f"→ {dev.id}: {len(pcm) / rate / 2:.1f} с, {rate} Гц"
                case "stop":
                    await self.stop(dev)
                    return f"→ {dev.id}: замолчать"
        except (ValueError, OSError, RuntimeError, wave.Error) as e:
            return f"{cmd}: {e}"
        return None
