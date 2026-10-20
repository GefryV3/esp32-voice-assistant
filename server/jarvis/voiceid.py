"""Голос хозяина: Мага отзывается на слово только от того, чей голос записан.

Как это работает
  1. Модель говорящего (WeSpeaker ResNet34, ~26 МБ ONNX, считает onnxruntime без torch) превращает
     1-3 секунды речи в вектор-отпечаток (256 чисел). Скачивается при первом запуске в data/models.
  2. `voice enroll`: Мага показывает на экране 10 коротких фраз, колонка записывает их через обычный
     поток микрофона; векторы (не звук!) лежат в brain.db, таблица voiceprints. Звук нигде не хранится.
  3. После слова пробуждения первые 1-2 секунды речи сравниваются с отпечатком хозяина (косинусное
     сходство). Не он: разговор не открывается, в журнале «чужой голос, сходство 0.31».

Режимы и политика (voice on|off|any)
  off (по умолчанию) — ничего не проверяем, поведение как раньше;
  owner — отвечаем хозяину; записанным людям и чужим молчим;
  any N — на N минут отвечаем всем (как presence here), потом снова owner.
  Неуверенность: сходство ниже порога, но не дальше NIGHT_MARGIN от него, и сейчас ночь: ОТВЕЧАЕМ
  (колонка стоит у кровати, сонный и простуженный голос звучит иначе; лучше лишний ответ, чем
  молчание). Днём то же сходство — отказ. Сбой модели, нет образцов, мало речи для вывода:
  отвечаем (нет доказательств, что говорит чужой), кроме случая «после слова вообще не речь».

Чужие голоса (voice enroll <имя>) — только по явной просьбе самого человека: он сам произносит первую
фразу-согласие, имя и векторы удаляются `voice forget <имя>`. Хранятся только векторы, ничего про
разговоры. Для «кто со мной говорил» в памяти (не на диске) держится последний узнанный голос и
время, когда кого слышали после слова пробуждения. Скрытой записи, расшифровки и фоновой
прослушки комнаты здесь нет и не будет: в Австрии это уголовно наказуемо.

Охрана (guard on|off, по умолчанию off): хозяина по presence нет, а микрофон слышит речь (простая
энергия звука, без распознавания). Одно сообщение в Telegram: время, сколько секунд речи, чей голос.
Без записи и без расшифровки; кусок звука для опознавания живёт в памяти несколько секунд.
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.request
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from . import protocol

log = logging.getLogger(__name__)

MODEL_NAME = "wespeaker_en_voxceleb_resnet34.onnx"
MODEL_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/{MODEL_NAME}"
MODEL_MIN_BYTES = 10_000_000
RATE = 16_000
BYTES_PER_S = RATE * 2

OWNER = "хозяин"
MODES = ("off", "owner")
PREF_MODE = "voiceid_mode"
PREF_THRESHOLD = "voiceid_threshold"
PREF_ANY_UNTIL = "voiceid_any_until"      # unix-время, до которого отвечаем всем
PREF_GUARD = "guard"

DEFAULT_THRESHOLD = 0.5
NIGHT_MARGIN = 0.10          # ночью сходство в пределах порога−0.10 считаем «скорее всё-таки он»
MIN_SAMPLES = 5              # меньше удачных записей отпечаток не сохраняем
ANY_DEFAULT_MIN = 30.0
ANY_MAX_MIN = 480.0

VERIFY_SPEECH_S = 1.2        # столько речи после слова хватает для сравнения
VERIFY_SHORT_S = 0.5         # короткая команда («Мага, свет»): хватит и этого, если фраза уже кончилась
VERIFY_TAIL_S = 0.6          # тишина после речи = фраза кончилась
VERIFY_MAX_S = 4.0           # дольше речи не ждём: слово прозвучало вхолостую
VERIFY_USE_S = 3.0           # для сравнения берём не больше стольки секунд речи
FRAME = 480                  # 30 мс при 16 кГц
MIN_RMS = 150.0              # тише — не речь (INMP441 с усилением 12 дБ)

ROOM_WINDOW_S = 900.0        # «в комнате» = слышали голос после слова за последние 15 минут
GUARD_WINDOW_S = 20.0
GUARD_MIN_S = 3.0            # столько секунд речи за окно, чтобы сработала охрана
GUARD_COOLDOWN_S = 600.0     # не чаще раза в 10 минут
GUARD_KEEP_S = 4.0           # кусок звука для опознавания, только в памяти
GUARD_RMS = 400.0

REC_S = 5.0                  # одна фраза записи
ENROLL_MIN_SPEECH_S = 1.0
ENROLL_ATTEMPTS = 2
MAX_NAME = 16

PHRASES = (
    "Сегодня хорошая погода",
    "Включи свет на столе",
    "Который сейчас час",
    "Поставь таймер на десять минут",
    "Расскажи что-нибудь интересное",
    "Как дела у моего хомяка",
    "Завтра рано вставать в школу",
    "Мне нужен совет по домашнему заданию",
    "Какая музыка сейчас играет",
    "Спокойной ночи, до завтра",
)
CONSENT = "Я {name}, согласен, чтобы Мага запомнил мой голос"


# ── признаки звука и модель ──────────────────────────────────────────────────

def _mel(f):
    return 1127.0 * np.log(1.0 + f / 700.0)


@lru_cache(maxsize=1)
def _filters(n_mels: int = 80, n_fft: int = 512, low: float = 20.0, high: float = 8000.0) -> np.ndarray:
    """Треугольные мел-фильтры как в Kaldi (на этих признаках учили модель)."""
    bins = n_fft // 2
    freq_mel = _mel(np.arange(bins) * RATE / n_fft)
    lo, hi = _mel(low), _mel(high)
    delta = (hi - lo) / (n_mels + 1)
    fb = np.zeros((n_mels, bins), np.float32)
    for m in range(n_mels):
        left = lo + m * delta
        centre, right = left + delta, left + 2 * delta
        fb[m] = np.maximum(0.0, np.minimum((freq_mel - left) / (centre - left), (right - freq_mel) / (right - centre)))
    return fb


def fbank(pcm: bytes) -> np.ndarray:
    """PCM 16 кГц 16 бит → лог-мел признаки Kaldi (80 полос, 25/10 мс, окно Повея, вычтено среднее)."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32)
    if len(x) < 400 + 160 * 10:
        raise ValueError("слишком короткий звук для отпечатка")
    n = 1 + (len(x) - 400) // 160
    frames = x[np.arange(400)[None, :] + 160 * np.arange(n)[:, None]]
    frames = frames - frames.mean(1, keepdims=True)
    frames = frames - 0.97 * np.concatenate([frames[:, :1], frames[:, :-1]], 1)
    frames = frames * ((0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, 512, axis=1))[:, :256] ** 2
    mel = np.log(np.maximum(power @ _filters().T, 1.1920929e-07))
    return (mel - mel.mean(0, keepdims=True)).astype(np.float32)


def ensure_model(model_dir: Path) -> Path:
    """Скачать модель говорящего, если её ещё нет (по образцу wake_vosk.ensure_model)."""
    path = model_dir / MODEL_NAME
    if path.is_file() and path.stat().st_size > MODEL_MIN_BYTES:
        return path
    model_dir.mkdir(parents=True, exist_ok=True)
    log.info("скачиваю модель голоса %s (~26 МБ)", MODEL_NAME)
    part = model_dir / f"{MODEL_NAME}.part"
    urllib.request.urlretrieve(MODEL_URL, part)
    if part.stat().st_size < MODEL_MIN_BYTES:
        part.unlink()
        raise RuntimeError("модель голоса скачалась не полностью")
    part.replace(path)
    return path


class OnnxEmbedder:
    """Отпечаток голоса: embed(pcm) → единичный вектор."""

    def __init__(self, path: Path | str):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
        self._input = self._sess.get_inputs()[0].name

    def embed(self, pcm: bytes) -> np.ndarray:
        out = self._sess.run(None, {self._input: fbank(pcm)[None]})[0]
        return unit(np.asarray(out, np.float32).reshape(-1))


def unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(unit(a), unit(b)))


def voiced(pcm: bytes) -> tuple[bytes, float, float]:
    """Только кадры с речью: (звук, секунд речи, секунд тишины в конце). По уровню, без распознавания."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16)
    n = len(x) // FRAME
    if n == 0:
        return b"", 0.0, 0.0
    fr = x[: n * FRAME].reshape(n, FRAME).astype(np.float32)
    rms = np.sqrt((fr ** 2).mean(1))
    mask = rms > max(MIN_RMS, 0.2 * float(np.percentile(rms, 95)))
    if not mask.any():
        return b"", 0.0, n * FRAME / RATE
    tail = n - 1 - int(np.nonzero(mask)[0][-1])
    return fr[mask].astype(np.int16).tobytes(), int(mask.sum()) * FRAME / RATE, tail * FRAME / RATE


# ── хранилище отпечатков ─────────────────────────────────────────────────────

SCHEMA = ("CREATE TABLE IF NOT EXISTS voiceprints (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
          "embedding BLOB NOT NULL, ts TEXT NOT NULL)")


class VoiceStore:
    def __init__(self, db, now: Callable[[], datetime] = datetime.now):
        self.db, self._now = db, now
        db.execute(SCHEMA)
        db.commit()
        self._cache: dict[str, np.ndarray] | None = None

    def replace(self, name: str, embeddings: list[np.ndarray]) -> None:
        stamp = self._now().isoformat(timespec="seconds")
        self.db.execute("DELETE FROM voiceprints WHERE name = ?", (name,))
        self.db.executemany("INSERT INTO voiceprints (name, embedding, ts) VALUES (?, ?, ?)",
                            [(name, np.asarray(e, np.float32).tobytes(), stamp) for e in embeddings])
        self.db.commit()
        self._cache = None

    def forget(self, name: str) -> int:
        n = self.db.execute("DELETE FROM voiceprints WHERE name = ?", (name,)).rowcount
        self.db.commit()
        self._cache = None
        return n

    def counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT name, COUNT(*) FROM voiceprints GROUP BY name ORDER BY name"))

    def centroids(self) -> dict[str, np.ndarray]:
        """Средний вектор на человека (единичный)."""
        if self._cache is None:
            rows: dict[str, list[np.ndarray]] = {}
            for name, blob in self.db.execute("SELECT name, embedding FROM voiceprints"):
                rows.setdefault(name, []).append(unit(np.frombuffer(blob, np.float32)))
            self._cache = {n: unit(np.mean(v, axis=0)) for n, v in rows.items()}
        return self._cache


# ── сравнение и решение ──────────────────────────────────────────────────────

@dataclass
class Match:
    name: str | None          # OWNER, имя записанного человека или None (не узнан)
    owner_score: float | None
    best_other: tuple[str, float] | None
    speech_s: float = 0.0

    @property
    def score(self) -> float:
        if self.name == OWNER or self.name is None:
            return self.owner_score if self.owner_score is not None else -1.0
        return self.best_other[1] if self.best_other else -1.0


def classify(emb: np.ndarray, cents: dict[str, np.ndarray], threshold: float) -> Match:
    scores = {n: cosine(emb, c) for n, c in cents.items()}
    owner = scores.get(OWNER)
    others = {n: s for n, s in scores.items() if n != OWNER}
    best = max(others.items(), key=lambda kv: kv[1]) if others else None
    name = None
    if owner is not None and owner >= threshold and (best is None or owner >= best[1]):
        name = OWNER
    elif best is not None and best[1] >= threshold:
        name = best[0]
    return Match(name, owner, best)


def decide(m: Match, threshold: float, night: bool) -> tuple[bool, str]:
    """Отвечать ли. Возвращает (да/нет, причина для журнала)."""
    if m.name == OWNER:
        return True, f"голос хозяина, сходство {m.score:.2f}"
    if m.name is not None:
        return False, f"голос «{m.name}» (сходство {m.score:.2f}), не хозяин"
    owner = m.owner_score if m.owner_score is not None else -1.0
    if night and owner >= threshold - NIGHT_MARGIN:
        return True, f"ночь, сходство {owner:.2f} чуть ниже порога {threshold:.2f}: отвечаю"
    return False, f"чужой голос, сходство {owner:.2f} (порог {threshold:.2f})"


@dataclass
class _Pending:
    dev: Any
    buf: bytearray
    deciding: bool = False


class VoiceID:
    TOOLS: list[dict[str, Any]] = [
        {"name": "who_is_here", "description": "Кто в комнате и кто говорил с тобой последним, по голосу. Вызови на "
         "«кто сейчас в комнате», «кто со мной говорил», «кто это был». Узнаёшь только людей, которые сами "
         "записали свой голос; незнакомых называй «кто-то незнакомый». Никого не выслеживаешь: это только "
         "те, кого ты услышал после слова «Мага» за последние минуты.",
         "parameters": {"type": "OBJECT", "properties": {}}},
    ]
    tool_names = {"who_is_here"}

    def __init__(self, brain: Any, *, embedder: Any = None, hub: Any = None, voice: Any = None, audio: Any = None,
                 presence: Any = None, notify: Callable[[str], Awaitable[Any]] | None = None,
                 night: Callable[[], bool] | None = None, threshold: float = DEFAULT_THRESHOLD,
                 clock: Callable[[], float] = time.time, now: Callable[[], datetime] = datetime.now):
        self.brain = brain
        self.store = VoiceStore(brain.db, now)
        self.embedder = embedder
        self.hub, self.voice, self.audio, self.presence = hub, voice, audio, presence
        self.notify = notify
        self._night = night or (lambda: False)
        self.default_threshold = threshold
        self._clock, self._now = clock, now
        self.sleep = asyncio.sleep
        self._pending: dict[str, _Pending] = {}
        self._tasks: set[asyncio.Task] = set()
        self.enrolling: str | None = None
        self.last: tuple[str, float, float] | None = None       # (кто, сходство, когда), только в памяти
        self.last_decision = ""
        self._seen: dict[str, float] = {}                       # имя → когда слышали последний раз
        self._guard_events: deque[tuple[float, float, bytes]] = deque()   # (время, секунд, звук) речи
        self._guard_last = -1e9
        if voice is not None:
            voice.voiceid = self
        if hub is not None:
            hub.on_audio(self._guard_audio)

    # ── настройки ──

    @property
    def mode(self) -> str:
        v = self.brain.pref(PREF_MODE, "off")
        return v if v in MODES else "off"

    @property
    def threshold(self) -> float:
        try:
            return float(self.brain.pref(PREF_THRESHOLD, "") or self.default_threshold)
        except ValueError:
            return self.default_threshold

    @property
    def guard_on(self) -> bool:
        return self.brain.pref(PREF_GUARD, "off") == "on"

    def any_left(self) -> float:
        """Сколько секунд ещё отвечаем всем (0 — переопределения нет)."""
        try:
            return max(float(self.brain.pref(PREF_ANY_UNTIL, "0") or 0) - self._clock(), 0.0)
        except ValueError:
            return 0.0

    @property
    def ready(self) -> bool:
        return self.embedder is not None

    def gating(self) -> bool:
        """Проверять ли голос после слова пробуждения."""
        return (self.mode == "owner" and self.ready and OWNER in self.store.counts()
                and self.any_left() <= 0 and self.enrolling is None)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ── проверка после слова пробуждения ──

    def begin(self, dev: Any) -> bool:
        """Слово пробуждения услышано. True — ждём речь для проверки (разговор пока не открыт)."""
        if not self.gating():
            return False
        self._pending[dev.id] = _Pending(dev, bytearray())
        return True

    def on_audio(self, dev: Any, pcm: bytes) -> bool:
        """Звук идёт сюда, пока проверка не закончена. True — кадр съеден."""
        p = self._pending.get(dev.id)
        if p is None:
            return False
        p.buf += pcm
        if p.deciding:
            return True
        _, speech, tail = voiced(bytes(p.buf))
        total = len(p.buf) / BYTES_PER_S
        enough = speech >= VERIFY_SPEECH_S or (speech >= VERIFY_SHORT_S and tail >= VERIFY_TAIL_S)
        if enough:
            p.deciding = True
            self._spawn(self._resolve(p))
        elif total >= VERIFY_MAX_S:
            del self._pending[dev.id]
            log.info("%s: после слова нет речи — разговор не открываю", dev.id)
            self._rearm(dev)
        return True

    def _rearm(self, dev: Any) -> None:
        v = self.voice
        if v is None:
            return
        v._quiet_until[dev.id] = v.clock() + 1.5
        det = v._detectors.get(dev.id)
        if det is not None:
            det.reset()

    def identify(self, pcm: bytes) -> Match:
        """Чей голос в этом куске речи (синхронно: считает модель)."""
        speech, secs, _ = voiced(pcm)
        clip = speech[: int(VERIFY_USE_S * BYTES_PER_S)] if secs >= 0.3 else pcm[: int(VERIFY_USE_S * BYTES_PER_S)]
        m = classify(self.embedder.embed(clip), self.store.centroids(), self.threshold)
        m.speech_s = secs
        return m

    def _remember(self, m: Match) -> None:
        now = self._clock()
        label = m.name or "незнакомый"
        self.last = (label, m.score, now)
        if m.name is not None:
            self._seen[m.name] = now
        else:
            self._seen["незнакомый"] = now

    async def _resolve(self, p: _Pending) -> None:
        dev = p.dev
        try:
            try:
                m = await asyncio.to_thread(self.identify, bytes(p.buf))
            except Exception as e:
                log.warning("голос: не смог сравнить (%s) — отвечаю", e)
                ok, why = True, "сбой проверки голоса"
            else:
                self._remember(m)
                ok, why = decide(m, self.threshold, self._night())
            self.last_decision = f"{'отвечаю' if ok else 'молчу'}: {why}"
            if not ok:
                log.info("%s: %s", dev.id, why)
                self._rearm(dev)
                return
            log.info("%s: %s", dev.id, why)
            v = self.voice
            if v is not None:
                v.start(dev)
                talk = v._talks.get(dev.id)
                if talk is not None:
                    talk.feed(bytes(p.buf))        # звук, пока шла проверка, догоняет в Gemini
        finally:
            self._pending.pop(dev.id, None)

    # ── «кто в комнате» ──

    def who(self) -> dict:
        now = self._clock()
        if not self.store.counts():
            return {"ok": False, "error": "голоса ещё не записаны (voice enroll)"}
        present = sorted(n for n, t in self._seen.items() if now - t <= ROOM_WINDOW_S)
        out: dict[str, Any] = {"ok": True, "in_room_by_voice": present,
                               "note": "это те, кого слышно после слова «Мага» за последние 15 минут; "
                                       "тихих и молчащих людей так не увидеть"}
        if self.last is not None:
            label, score, at = self.last
            out["last_speaker"] = label
            out["minutes_ago"] = round((now - at) / 60, 1)
            out["similarity"] = round(score, 2)
        return out

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "who_is_here":
            return self.who()
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    # ── охрана ──

    def _talking(self, dev: Any) -> bool:
        v = self.voice
        if v is None:
            return False
        return dev.id in v._talks or v.quiet.active(dev.id) or dev.id in self._pending

    def _guard_audio(self, dev: Any, pcm: bytes) -> None:
        if not self.guard_on or self.presence is None or self.enrolling or self._talking(dev):
            return
        now = self._clock()
        dur = len(pcm) / BYTES_PER_S
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32)
        if x.size and float(np.sqrt((x ** 2).mean())) >= GUARD_RMS and not self.presence.here():
            self._guard_events.append((now, dur, pcm))
        while self._guard_events and now - self._guard_events[0][0] > GUARD_WINDOW_S:
            self._guard_events.popleft()
        speech = sum(d for _, d, _ in self._guard_events)
        if speech < GUARD_MIN_S or now - self._guard_last < GUARD_COOLDOWN_S:
            return
        self._guard_last = now
        clip = b"".join(p for _, _, p in self._guard_events)[-int(GUARD_KEEP_S * BYTES_PER_S):]
        self._guard_events.clear()
        self._spawn(self._guard_fire(speech, clip))

    async def _guard_fire(self, speech_s: float, clip: bytes) -> None:
        who = "голос не определён (отпечатки не записаны или модель не загружена)"
        if self.ready and self.store.counts():
            try:
                m = await asyncio.to_thread(self.identify, clip)
                self._remember(m)
                who = (f"голос хозяина ({m.score:.2f})" if m.name == OWNER else
                       f"голос «{m.name}» ({m.score:.2f})" if m.name else
                       f"голос незнакомый (сходство с хозяином {m.owner_score if m.owner_score is not None else 0:.2f})")
            except Exception as e:
                log.warning("охрана: не смог опознать голос: %s", e)
        text = (f"🛡 {self._now():%H:%M}: в комнате слышна речь, около {round(speech_s)} с, а по presence тебя "
                f"нет на месте. {who[0].upper() + who[1:]}. Записи и расшифровки нет.")
        log.info("охрана: %s", text)
        if self.notify is not None:
            try:
                await self.notify(text)
            except Exception as e:
                log.warning("охрана: Telegram не принял сообщение: %s", e)

    # ── запись образцов ──

    def _dev(self) -> Any:
        return self.hub.get() if self.hub is not None else None

    async def _enroll(self, dev: Any, name: str) -> str:
        phrases = list(PHRASES)
        if name != OWNER:
            phrases[0] = CONSENT.format(name=name)
        embs: list[np.ndarray] = []
        self.enrolling = name
        try:
            for i, phrase in enumerate(phrases, 1):
                for _ in range(ENROLL_ATTEMPTS):
                    text = _clip(f"{i}/{len(phrases)} скажи: {phrase}")
                    await dev.send(protocol.caption("reply", text))
                    await self.audio.play(dev, _beep())
                    pcm = await self.audio.record(dev, REC_S)
                    speech, secs, _ = voiced(pcm)
                    if secs >= ENROLL_MIN_SPEECH_S:
                        embs.append(await asyncio.to_thread(self.embedder.embed, speech[: int(6 * BYTES_PER_S)]))
                        break
                    await self.sleep(0.3)
        except Exception as e:
            log.warning("запись голоса прервалась: %s", e)
            return f"запись голоса прервалась ({e}); ничего не сохранено"
        finally:
            self.enrolling = None
            try:
                await dev.send(protocol.caption("none"))
            except Exception:
                pass
        if len(embs) < MIN_SAMPLES:
            return (f"расслышал только {len(embs)} фраз из {len(phrases)} (нужно хотя бы {MIN_SAMPLES}); "
                    "ничего не сохранено. Говори громче и ближе к колонке, потом voice enroll ещё раз")
        self.store.replace(name, embs)
        tip = " Теперь `voice on`: Мага будет отзываться только на тебя." if name == OWNER and self.mode == "off" else ""
        return f"голос «{name}» записан: {len(embs)} образцов из {len(phrases)}.{tip}"

    async def _enroll_job(self, dev: Any, name: str) -> None:
        result = await self._enroll(dev, name)
        log.info("voice enroll %s: %s", name, result)
        if self.notify is not None:
            try:
                await self.notify(result)
            except Exception:
                pass

    # ── команды консоли и Telegram ──

    def status(self) -> str:
        counts = self.store.counts()
        people = ", ".join(f"{n}: {c}" for n, c in counts.items()) or "никого"
        any_left = self.any_left()
        over = f"; отвечаю всем ещё {round(any_left / 60)} мин" if any_left > 0 else ""
        last = f"; последнее решение — {self.last_decision}" if self.last_decision else ""
        return (f"голос: режим {self.mode}{over}, порог {self.threshold:.2f} (ночью до "
                f"{self.threshold - NIGHT_MARGIN:.2f} отвечаю), модель {'готова' if self.ready else 'не загружена'}, "
                f"образцы — {people}; охрана {'on' if self.guard_on else 'off'}{last}")

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts:
            return None
        head = parts[0].lower().lstrip("/")
        args = parts[1:]
        if head == "guard":
            if not args or args[0].lower() not in ("on", "off"):
                return f"guard on|off — сообщение в Telegram, если хозяина нет, а в комнате слышна речь (сейчас {'on' if self.guard_on else 'off'})"
            self.brain.set_pref(PREF_GUARD, args[0].lower())
            return ("охрана включена: одно сообщение не чаще раза в 10 минут, без записи и расшифровки"
                    if args[0].lower() == "on" else "охрана выключена")
        if head != "voice" or not args:
            return None
        sub = args[0].lower()
        if sub == "status":
            return self.status()
        if sub == "threshold":
            if len(args) == 1:
                return f"порог {self.threshold:.2f}; voice threshold 0.2…0.9 (выше — строже, чаще молчит)"
            try:
                v = float(args[1].replace(",", "."))
            except ValueError:
                return "voice threshold <число 0.2…0.9>"
            if not 0.2 <= v <= 0.9:
                return "порог должен быть от 0.2 до 0.9"
            self.brain.set_pref(PREF_THRESHOLD, f"{v:.2f}")
            return f"порог {v:.2f}"
        if sub == "on":
            if not self.ready:
                return "модель голоса ещё не загружена (нужна сеть при первом запуске) — режим не включён"
            if OWNER not in self.store.counts():
                return "образцов хозяина нет: сначала `voice enroll`"
            self.brain.set_pref(PREF_MODE, "owner")
            self.brain.set_pref(PREF_ANY_UNTIL, "0")
            return f"Мага отзывается только на голос хозяина (порог {self.threshold:.2f}); `voice any` — на время всем"
        if sub == "off":
            self.brain.set_pref(PREF_MODE, "off")
            return "проверка голоса выключена: Мага отзывается всем"
        if sub == "any":
            try:
                minutes = float(args[1]) if len(args) > 1 else ANY_DEFAULT_MIN
            except ValueError:
                return "voice any [минуты]"
            minutes = min(max(minutes, 0.0), ANY_MAX_MIN)
            self.brain.set_pref(PREF_ANY_UNTIL, str(self._clock() + minutes * 60 if minutes else 0))
            return f"отвечаю всем {round(minutes)} мин" if minutes else "переопределение снято: снова только хозяин"
        if sub == "list":
            return self.status()
        if sub == "forget":
            if len(args) < 2:
                return "voice forget <имя> (хозяин — свой голос)"
            name = _name(args[1])
            n = self.store.forget(name)
            if name == OWNER and n:
                self.brain.set_pref(PREF_MODE, "off")
            return f"отпечаток «{name}» удалён ({n} образцов)" if n else f"«{name}» не записан"
        if sub == "enroll":
            name = _name(args[1]) if len(args) > 1 else OWNER
            if len(name) > MAX_NAME:
                return f"имя не длиннее {MAX_NAME} символов"
            if not self.ready:
                return "модель голоса ещё не загружена — запись невозможна"
            dev = self._dev()
            if dev is None or "mic" not in dev.hello.caps or self.audio is None:
                return "колонка не подключена или без микрофона"
            if self.enrolling or (self.voice is not None and dev.id in self.voice._talks):
                return "колонка занята (идёт разговор или запись)"
            self._spawn(self._enroll_job(dev, name))
            who = "свой голос" if name == OWNER else f"голос «{name}» (человек сам читает фразу-согласие вслух)"
            return (f"записываю {who}: на экране колонки по очереди {len(PHRASES)} коротких фраз, после писка "
                    f"читай вслух, на каждую {REC_S:.0f} с. Всё займёт около минуты; векторы, не звук, "
                    f"лягут в brain.db. Итог пришлю сюда.")
        return None


def _name(raw: str) -> str:
    name = raw.strip().casefold()
    return OWNER if name in ("я", "me", "owner", OWNER) else name


def _clip(text: str, max_bytes: int = 127) -> str:
    while len(text.encode()) > max_bytes:
        text = text[:-1]
    return text


def _beep() -> bytes:
    from .audio import tone
    return tone(1000, 120)
