import importlib.util
import json
import sys
import wave
from pathlib import Path

import numpy as np

from jarvis.wake_vosk import VoskWake

SPEC = importlib.util.spec_from_file_location("wake_eval", Path(__file__).resolve().parents[1] / "tools" / "wake_eval.py")
we = importlib.util.module_from_spec(SPEC)
sys.modules["wake_eval"] = we
SPEC.loader.exec_module(we)


def write(path, samples, rate=16000, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(samples, dtype="<i2").tobytes())


class LoudDetector:
    """Срабатывает, если в записи есть громкий кусок (синтетика вместо слова)."""

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def process(self, pcm):
        return 1.0 if np.abs(np.frombuffer(pcm, dtype="<i2")).max() > 8000 else 0.0


def test_load_wav_converts_stereo_and_rate(tmp_path):
    # 1 с стерео на 8 кГц: L=1000, R=-1000 → моно 16 кГц, 16000 отсчётов, первый канал
    write(tmp_path / "a.wav", np.tile(np.array([1000, -1000], dtype=np.int16), 8000), rate=8000, channels=2)
    x = np.frombuffer(we.load_wav(tmp_path / "a.wav"), dtype="<i2")
    assert len(x) == 16000 and x[0] == 1000 and x[-1] == 1000


def folder(tmp_path):
    loud = np.full(16000, 12000)
    quiet = np.zeros(16000)
    write(tmp_path / "yes_ok.wav", loud)
    write(tmp_path / "yes_missed.wav", quiet)
    write(tmp_path / "no_ok.wav", quiet)
    write(tmp_path / "no_false.wav", loud)
    return tmp_path


def test_evaluate_finds_misses_and_false_alarms(tmp_path):
    (r,) = we.evaluate(folder(tmp_path), {"fake": LoudDetector()})
    assert (r.yes, r.no) == (2, 2)
    assert [n for n, _ in r.misses] == ["yes_missed.wav"]
    assert [n for n, _ in r.false_alarms] == ["no_false.wav"]
    assert not r.ok
    text = we.report([r], 0.5)
    assert "пропуски: 1 из 2" in text and "yes_missed.wav" in text and "ложные срабатывания: 1 из 2" in text


def test_detector_is_reset_for_every_clip(tmp_path):
    det = LoudDetector()
    we.evaluate(folder(tmp_path), {"fake": det})
    assert det.resets == 4


def test_main_exit_codes_and_empty_dir(tmp_path, capsys):
    def fake(names, models, word):
        return {"fake": LoudDetector()}

    assert we.main(["--dir", str(tmp_path)], fake) == 2
    assert "docs/wake-eval.md" in capsys.readouterr().out
    folder(tmp_path)
    assert we.main(["--dir", str(tmp_path)], fake) == 1
    for name in ("yes_missed.wav", "no_false.wav"):
        (tmp_path / name).unlink()
    assert we.main(["--dir", str(tmp_path)], fake) == 0


class SpeechRec:
    """Вместо Kaldi: «слышит» мага в записи, где есть громкий кусок."""

    def __init__(self):
        self.word = ""

    def AcceptWaveform(self, pcm):
        if np.abs(np.frombuffer(pcm, dtype="<i2")).max() > 8000:
            self.word = "мага"
        return False

    def PartialResult(self):
        return json.dumps({"partial": self.word})

    def Result(self):
        return json.dumps({"text": self.word})


def test_works_with_real_vosk_wrapper(tmp_path):
    det = VoskWake("x", ("мага",), recognizer_factory=SpeechRec)
    (r,) = we.evaluate(folder(tmp_path), {"vosk": det})
    assert [n for n, _ in r.misses] == ["yes_missed.wav"] and [n for n, _ in r.false_alarms] == ["no_false.wav"]
