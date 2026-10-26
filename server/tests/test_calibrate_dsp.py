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


def test_ir_quality_tells_a_real_measurement_from_noise():
    ref = cal.sweep()
    delay, good = cal.ir_quality(cal.impulse_response(fake_speaker(ref, delay_s=1.4), ref))
    assert delay == pytest.approx(1.4, abs=0.02) and good > 30
    noise = np.random.default_rng(3).standard_normal(len(ref) + 4 * RATE) * 0.01
    assert cal.ir_quality(cal.impulse_response(noise, ref))[1] < 20


def test_mic_calibration_file_is_read_and_removed(tmp_path):
    f = tmp_path / "mic.txt"
    f.write_text('"Sens Factor =-1.2dB"\n* header\n100\t-3.0\n1000, 0.0\n10000 4.0\n', encoding="utf-8")
    cf, cd = cal.load_mic_calibration(f)
    freqs = np.array([100.0, 1000.0, 10000.0])
    assert np.allclose(cal.remove_mic_response(freqs, np.array([0.0, 0.0, 0.0]), cf, cd), [3.0, 0.0, -4.0])
    (tmp_path / "bad.txt").write_text("1 2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        cal.load_mic_calibration(tmp_path / "bad.txt")


def test_two_microphones_average_out_a_fault_and_flag_the_disagreement():
    freqs = cal.log_grid()
    truth = np.zeros_like(freqs)
    bumpy = truth + 6 * np.exp(-np.log2(freqs / 4000) ** 2)             # one microphone has a +6 dB bump at 4 kHz
    mean, spread = cal.combine_mics(freqs, [truth, bumpy])
    at = int(np.argmin(abs(freqs - 4000)))
    assert mean[at] == pytest.approx(3.0, abs=0.7) and spread[at] == pytest.approx(6.0, abs=0.7)   # the middle match eats a bit
    conf = cal.confidence(spread)
    assert conf[at] < 0.8 and conf[int(np.argmin(abs(freqs - 1000)))] == 1.0
    weak = cal.design_correction(freqs, mean, confidence=conf)
    strong = cal.design_correction(freqs, mean)
    assert abs(weak[at]) < abs(strong[at])


def test_regularize_cuts_peaks_but_barely_fills_dips():
    freqs = cal.log_grid()
    peak = np.where((freqs > 2500) & (freqs < 5000), -9.0, 0.0)               # a broad peak to cut
    dip = np.where((freqs > 900) & (freqs < 1100), 9.0, 0.0)                  # a dip someone wants to fill
    out = cal.regularize(freqs, peak + dip)
    assert out[int(np.argmin(abs(freqs - 3500)))] < -7.0
    assert out.max() <= 2.0 + 1e-9
    low = cal.regularize(freqs, np.full_like(freqs, 12.0))
    assert low[freqs < 250].max() <= 4.0 + 1e-9
