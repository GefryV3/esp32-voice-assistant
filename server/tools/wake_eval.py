"""Проверка слова пробуждения на записях: сколько раз «Мага» не услышали и сколько раз услышали зря.

    python tools/wake_eval.py                      # папка data/eval, оба детектора
    python tools/wake_eval.py --detectors vosk     # только «Мага» (Vosk)
    python tools/wake_eval.py --dir путь --threshold 0.6

В папке лежат WAV-записи (16 кГц, 16 бит, моно; другая частота приводится сама):
    yes_*.wav — в записи говорят слово («Мага, включи свет»): детектор обязан сработать;
    no_*.wav  — слова нет (разговор, телевизор, музыка, тишина): срабатывать нельзя.
Как записать с колонки — docs/wake-eval.md. Печатает по каждому детектору пропуски и ложные
срабатывания, в конце итог; код выхода 1, если что-то не так (удобно гонять после смены порога).
"""

from __future__ import annotations

import argparse
import sys
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RATE = 16_000
CHUNK = 1280              # 80 мс: шаг, которым колонка кормит детектор
TAIL_S = 1.0              # тишина после записи: Vosk выдаёт слово на паузе
DEFAULT_DIR = Path(__file__).resolve().parents[1] / "data" / "eval"
DEFAULT_MODELS = Path(__file__).resolve().parents[1] / "data" / "models"


def load_wav(path: Path) -> bytes:
    """PCM 16 кГц 16 бит моно. Стерео — первый канал, другая частота — линейная передискретизация."""
    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path.name}: нужны 16 бит")
        ch, rate, raw = w.getnchannels(), w.getframerate(), w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype="<i2")
    if ch > 1:
        x = x[::ch]
    if rate != RATE and len(x):
        n = int(len(x) * RATE / rate)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x.astype(np.float32)).astype("<i2")
    return x.astype("<i2").tobytes()


def fires(detector, pcm: bytes, threshold: float) -> float:
    """Лучшая оценка детектора на записи (сработал, если ≥ threshold)."""
    detector.reset()
    pcm = pcm + bytes(int(RATE * TAIL_S) * 2)
    best = 0.0
    step = CHUNK * 2
    for i in range(0, len(pcm), step):
        chunk = pcm[i:i + step]
        if len(chunk) < step:
            chunk += bytes(step - len(chunk))
        best = max(best, float(detector.process(chunk)))
    return best


@dataclass
class Result:
    name: str
    yes: int = 0
    no: int = 0
    misses: list[tuple[str, float]] = field(default_factory=list)
    false_alarms: list[tuple[str, float]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.misses and not self.false_alarms


def clips(folder: Path) -> tuple[list[Path], list[Path]]:
    return sorted(folder.glob("yes_*.wav")), sorted(folder.glob("no_*.wav"))


def evaluate(folder: Path, detectors: dict[str, object], threshold: float = 0.5) -> list[Result]:
    yes, no = clips(folder)
    pcm = {p: load_wav(p) for p in [*yes, *no]}
    results = []
    for name, det in detectors.items():
        r = Result(name, len(yes), len(no))
        for p in yes:
            score = fires(det, pcm[p], threshold)
            if score < threshold:
                r.misses.append((p.name, score))
        for p in no:
            score = fires(det, pcm[p], threshold)
            if score >= threshold:
                r.false_alarms.append((p.name, score))
        results.append(r)
    return results


def report(results: list[Result], threshold: float) -> str:
    lines = []
    for r in results:
        lines.append(f"{r.name}: порог {threshold}, записей со словом {r.yes}, без слова {r.no}")
        lines.append(f"  пропуски: {len(r.misses)} из {r.yes}" + (" — " + ", ".join(f"{n} ({s:.2f})" for n, s in r.misses) if r.misses else ""))
        lines.append(f"  ложные срабатывания: {len(r.false_alarms)} из {r.no}"
                     + (" — " + ", ".join(f"{n} ({s:.2f})" for n, s in r.false_alarms) if r.false_alarms else ""))
    return "\n".join(lines)


def make_detectors(names: list[str], model_dir: Path, word: str) -> dict[str, object]:
    out: dict[str, object] = {}
    if "vosk" in names:
        from jarvis.wake_vosk import VoskWake, ensure_model
        out["vosk (Мага)"] = VoskWake(ensure_model(model_dir), (word,))
    if "oww" in names:
        from jarvis.wake import WakeWord, ensure_models
        ensure_models(model_dir)
        out["openWakeWord (hey jarvis)"] = WakeWord(model_dir)
    return out


def main(argv: list[str] | None = None, factory: Callable[..., dict[str, object]] = make_detectors) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="папка с yes_*.wav и no_*.wav")
    p.add_argument("--models", type=Path, default=DEFAULT_MODELS, help="папка моделей (data/models)")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--detectors", default="vosk,oww", help="vosk, oww или оба через запятую")
    p.add_argument("--word", default="мага", help="слово для Vosk")
    args = p.parse_args(argv)
    yes, no = clips(args.dir)
    if not yes and not no:
        print(f"В {args.dir} нет ни yes_*.wav, ни no_*.wav. Как записать: docs/wake-eval.md")
        return 2
    names = [n.strip() for n in args.detectors.split(",") if n.strip()]
    results = evaluate(args.dir, factory(names, args.models, args.word), args.threshold)
    print(report(results, args.threshold))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
