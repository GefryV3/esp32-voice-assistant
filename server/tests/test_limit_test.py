import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
pytest.importorskip("soundcard")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import limit_test as lt  # noqa: E402
from limit_test import LEVELS_DB, RATE  # noqa: E402


def fake_speaker(x, delay_s=1.3, saturation=0.25, noise_db=-70.0, seed=1):
    """A speaker that saturates softly above `saturation`, 1.3 s of pipeline delay and some room noise."""
    y = saturation * np.tanh(x / saturation)
    y = np.concatenate([np.zeros(int(delay_s * RATE)), y, np.zeros(2 * RATE)])
    return y + np.random.default_rng(seed).standard_normal(len(y)) * 10 ** (noise_db / 20)


def measure(freq, **kw):
    signal, starts = lt.staircase(freq)
    rec = fake_speaker(signal, **kw)
    origin = lt.find_start(rec[: 6 * RATE])
    rows = []
    for db, s in zip(LEVELS_DB, starts):
        a = origin + s + int(0.25 * RATE)
        rows.append({"db": db, **lt.analyse(rec[a: a + int(0.55 * RATE)], freq)})
    return origin, rows


def test_sync_finds_the_delay_and_distortion_grows_where_the_speaker_saturates():
    origin, rows = measure(250)
    assert origin / RATE == pytest.approx(1.3, abs=0.01)
    thd = [r["thd"] for r in rows]
    assert thd[0] < 0.01                                   # the quietest burst is clean
    assert thd[-1] > 0.10                                  # the loudest one is clearly distorted
    assert all(b >= a - 0.005 for a, b in zip(thd, thd[1:]))      # distortion only grows with level


def test_a_linear_speaker_reports_no_distortion():
    _, rows = measure(1000, saturation=100.0)
    assert max(r["thd"] for r in rows) < 0.01


def test_measured_level_follows_the_input_until_saturation():
    _, rows = measure(100, saturation=0.5)
    a1 = [r["a1"] for r in rows]
    assert a1[1] / a1[0] == pytest.approx(10 ** (3 / 20), rel=0.05)      # +3 dB in, +3 dB out
