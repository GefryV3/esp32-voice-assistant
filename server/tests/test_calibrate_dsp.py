import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("scipy")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from scipy.signal import butter, lfilter  # noqa: E402

import calibrate_dsp as cal  # noqa: E402
from calibrate_dsp import RATE  # noqa: E402


def fake_speaker(signal, delay_s=1.4, noise_db=-65.0, seed=0):
    """High-pass at 150 Hz, a +6 dB bump at 3 kHz, a 1.4 s pipeline delay and some room noise."""
    b, a = butter(2, 150, "highpass", fs=RATE)
    y = lfilter(b, a, signal)
    bb, aa = butter(2, [2200, 4200], "bandpass", fs=RATE)
    y = y + 1.0 * lfilter(bb, aa, y)                    # roughly +6 dB around 3 kHz
    y = np.concatenate([np.zeros(int(delay_s * RATE)), y, np.zeros(RATE)])
    return y + np.random.default_rng(seed).standard_normal(len(y)) * 10 ** (noise_db / 20)


def measure(**kw):
    ref = cal.sweep()
    ir = cal.impulse_response(fake_speaker(ref, **kw), ref)
    return cal.smoothed_response_db(ir)


def test_recovers_the_response_of_a_known_speaker():
    freqs, db = measure()
    truth_b, truth_a = butter(2, 150, "highpass", fs=RATE)
    from scipy.signal import freqz
    _, h = freqz(truth_b, truth_a, worN=2 * np.pi * freqs / RATE)
    bb, aa = butter(2, [2200, 4200], "bandpass", fs=RATE)
    _, hb = freqz(bb, aa, worN=2 * np.pi * freqs / RATE)
    expected = 20 * np.log10(np.abs(h * (1 + hb)))
    m = (freqs > 250) & (freqs < 12_000)
    err = (db - np.median(db[m] - expected[m]))[m] - expected[m]
    assert np.abs(err).max() < 1.5, np.abs(err).max()


def test_delay_does_not_matter():
    f1, d1 = measure(delay_s=1.4)
    f2, d2 = measure(delay_s=2.1)
    assert np.abs(d1 - d2)[(f1 > 200) & (f1 < 10_000)].max() < 0.7


def test_correction_flattens_a_speaker_towards_the_target():
    freqs, db = measure()
    corr = cal.design_correction(freqs, db)
    after = db + corr
    target = cal.target_db(freqs)
    mid = (freqs >= 500) & (freqs <= 2000)
    before_err = (target - db) - np.median((target - db)[mid])
    after_err = (target - after) - np.median((target - after)[mid])
    band = (freqs > 200) & (freqs < 10_000)
    assert np.abs(after_err[band]).max() < 0.5 * np.abs(before_err[band]).max()


def test_correction_does_not_boost_where_the_speaker_has_nothing():
    freqs, db = measure()
    corr = cal.design_correction(freqs, db, low=100.0)
    assert corr[freqs < 50].max() <= 0.5          # the bass shelf of the target is not chased below the limit
    assert corr.max() <= 9.0 + 1e-9 and corr.min() >= -15.0 - 1e-9


def test_second_pass_adds_to_the_first():
    freqs, db = measure()
    first = cal.design_correction(freqs, db)
    again = cal.design_correction(freqs, db + first, previous=first)         # the speaker now sounds corrected
    assert np.abs(again - first).max() < 3.0                                  # already close: only a small change
