"""Maths of the speaker measurement (tools/calibrate.py): sweep, impulse response, smoothing, target curve,
correction design. Pure functions on numpy arrays, no sound devices, so it can be tested with a simulated speaker.

Method: an exponential sine sweep is played, the microphone records it, and the recording is deconvolved with the
sweep to get the impulse response of speaker + room + microphone. The response is windowed (the first reflections
of the room are cut off), turned into a smoothed magnitude curve and compared with the target curve.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

RATE = 48_000

# Target: Harman-style "house" sound that JBL (a Harman brand) tunes its speakers to. dB relative to 1 kHz.
# There is no public measured curve of a particular JBL, so this is the general shape: a bass shelf, flat middle,
# a little presence around 3 kHz and a gentle roll-off of the top.
TARGET_POINTS = [(20, -18), (40, -6), (63, 3), (100, 5), (160, 4.5), (250, 3), (400, 1.5), (630, 0.5), (1000, 0),
                 (1600, 0.5), (2500, 1.5), (4000, 1), (6300, -1), (10000, -3), (16000, -8), (20000, -14)]


def target_db(freqs: np.ndarray) -> np.ndarray:
    f, d = zip(*TARGET_POINTS)
    return np.interp(np.log(freqs), np.log(f), d)


def sweep(seconds: float = 8.0, f0: float = 20.0, f1: float = 20_000.0, level_db: float = -20.0) -> np.ndarray:
    """Exponential sine sweep with short fades at both ends."""
    t = np.arange(int(seconds * RATE)) / RATE
    k = np.log(f1 / f0)
    x = np.sin(2 * np.pi * f0 * seconds / k * (np.exp(t / seconds * k) - 1))
    fade = int(0.05 * RATE)
    x[:fade] *= np.linspace(0, 1, fade) ** 2
    x[-fade:] *= np.linspace(1, 0, fade) ** 2
    return x * 10 ** (level_db / 20)


def impulse_response(recording: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Deconvolution in the frequency domain, lightly regularised where the sweep has no energy."""
    n = 1 << int(np.ceil(np.log2(len(recording) + len(reference))))
    rec, ref = np.fft.rfft(recording, n), np.fft.rfft(reference, n)
    eps = 1e-8 * np.max(np.abs(ref)) ** 2
    return np.fft.irfft(rec * np.conj(ref) / (np.abs(ref) ** 2 + eps), n)


def ir_quality(ir: np.ndarray) -> tuple[float, float]:
    """(delay in seconds of the strongest peak, how far it stands above the rest of the response in dB).
    A real measurement has a sharp direct-sound peak well above everything around it; noise does not."""
    a = np.abs(ir[: len(ir) // 2])
    peak = int(np.argmax(a))
    guard, span = int(0.05 * RATE), int(0.5 * RATE)
    around = np.concatenate([a[max(0, peak - span): max(0, peak - guard)], a[peak + guard: peak + span]])
    return peak / RATE, 20 * float(np.log10(a[peak] / (np.sqrt(np.mean(around ** 2)) + 1e-12)))


def load_mic_calibration(path) -> tuple[np.ndarray, np.ndarray]:
    """Microphone calibration file: lines of `Hz dB` (also comma or tab separated, `#`/`*` comments), the way
    UMIK-1 / iMM-6 files and digitised curves come. The dB are how much the microphone over-reads at that frequency."""
    rows = []
    for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip().replace(",", " ").replace("	", " ")
        if not line or line[0] in "#*\"":
            continue
        parts = line.split()
        try:
            rows.append((float(parts[0]), float(parts[1])))
        except (ValueError, IndexError):
            continue                      # a header line such as "Sens Factor =-1.2dB"
    if len(rows) < 3:
        raise ValueError(f"{path}: need at least three `Hz dB` lines")
    rows.sort()
    f, d = zip(*rows)
    return np.array(f), np.array(d)


def remove_mic_response(freqs: np.ndarray, measured_db: np.ndarray, cal_freqs: np.ndarray, cal_db: np.ndarray) -> np.ndarray:
    """Subtract what the microphone itself added (held flat outside the range of the calibration file)."""
    return measured_db - np.interp(np.log(freqs), np.log(cal_freqs), cal_db)


def log_grid(points: int = 160, lo: float = 30.0, hi: float = 18_000.0) -> np.ndarray:
    return np.logspace(np.log10(lo), np.log10(hi), points)


def smoothed_response_db(ir: np.ndarray, gate_ms: float = 30.0, fraction: float = 3.0):
    """(freqs, dB) of the impulse response around its main peak, smoothed in 1/`fraction` octave bands."""
    peak = int(np.argmax(np.abs(ir[: len(ir) // 2])))
    pre, post = int(0.003 * RATE), int(gate_ms / 1000 * RATE)
    seg = ir[max(0, peak - pre): peak + post].copy()
    taper = int(len(seg) * 0.4)
    seg[-taper:] *= 0.5 * (1 + np.cos(np.linspace(0, np.pi, taper)))        # soft end: no ringing from the cut
    n = RATE                                                                   # 1 Hz resolution
    power = np.abs(np.fft.rfft(seg, n)) ** 2
    bins = np.arange(len(power))
    freqs = log_grid()
    half = 2 ** (1 / (2 * fraction))
    out = np.empty(len(freqs))
    for i, f in enumerate(freqs):
        sel = (bins >= f / half) & (bins <= f * half)
        out[i] = 10 * np.log10(power[sel].mean() + 1e-30)
    return freqs, out


def design_correction(freqs: np.ndarray, measured_db: np.ndarray, *, max_boost: float = 9.0, max_cut: float = 15.0,
                      low: float = 90.0, high: float = 14_000.0, previous: np.ndarray | None = None,
                      damping: float = 1.0) -> np.ndarray:
    """Correction in dB (to add to the signal) that moves `measured_db` onto the target.

    The level is matched in the middle (500 Hz…2 kHz). Below `low` and above `high` the speaker has nothing to give,
    so boosting is faded out there: it would only burn amplifier power. `previous` accumulates a second pass."""
    diff = target_db(freqs) - measured_db
    diff = diff - np.median(diff[(freqs >= 500) & (freqs <= 2000)])
    if previous is not None:
        diff = previous + damping * diff
    below = np.clip(np.log2(freqs / (low * 0.65)) / np.log2(1 / 0.65), 0, 1)        # 0 at 0.65*low, 1 at low
    above = np.clip(1 - np.log2(freqs / high) / np.log2(1.3), 0, 1)                 # 1 at high, 0 at 1.3*high
    diff = np.where(diff > 0, diff * np.minimum(below, np.where(freqs > high, above, 1.0)), diff)
    diff = np.clip(diff, -max_cut, max_boost)
    kernel = np.ones(5) / 5                                                        # light smoothing in log frequency
    padded = np.pad(diff, 2, mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def band_table(freqs, measured_db, target, bands=(63, 125, 250, 500, 1000, 2000, 4000, 8000, 12000)) -> list[tuple]:
    """(band, measured, target) relative to 1 kHz, for printing."""
    ref_m = np.interp(np.log(1000), np.log(freqs), measured_db)
    rows = []
    for b in bands:
        rows.append((b, float(np.interp(np.log(b), np.log(freqs), measured_db) - ref_m),
                     float(np.interp(np.log(b), np.log(freqs), target))))
    return rows
