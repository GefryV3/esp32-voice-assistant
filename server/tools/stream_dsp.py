"""Sound processing for tools/stream.py: equalizer, virtual bass with a bass cut, compressor, soft limiter.

Everything runs on 48 kHz mono blocks and keeps its filter state between blocks. Settings live in
`Equalizer` (shared with the web page); `Chain` is what the audio loop calls.

Why these stages: the speaker (a small full-range driver) cannot play below ~100 Hz, so power spent
there is wasted and eats the headroom of everything else. Cut it, and rebuild the *impression* of
that bass from its harmonics (virtual bass). The compressor then raises the average level without
raising the peaks, which is the cheapest way to get more loudness out of the same amplifier.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
from scipy.ndimage import maximum_filter1d
from scipy.signal import butter, sosfilt, sosfreqz

RATE = 48_000
SAVED = Path.home() / ".jarvis_stream_eq.json"

# (centre Hz, kind): the lowest and highest bands are shelves, the rest are peaks of about an octave
BANDS = [(63, "ls"), (125, "pk"), (250, "pk"), (500, "pk"), (1000, "pk"), (2000, "pk"), (4000, "pk"), (8000, "hs")]
MAX_DB = 15.0
DEFAULT_FX = {"vbass": 0.0, "comp": 0.0, "cut": 20.0}     # 0…100 %, 0…100 %, Hz (20 — practically off)
FX_RANGE = {"vbass": (0.0, 100.0), "comp": (0.0, 100.0), "cut": (20.0, 160.0)}

SUB = 240                    # compressor works on 5 ms slices
LOOK = 240                   # limiter looks 5 ms ahead (and delays the sound by as much)
ATTACK_S, RELEASE_S = 0.010, 0.150


def biquad(kind: str, gain_db: float, freq: float, rate: int = RATE, q: float = 1.1) -> list[float]:
    """RBJ cookbook biquad as an SOS row [b0 b1 b2 1 a1 a2]."""
    a = 10 ** (gain_db / 40)
    w = 2 * np.pi * freq / rate
    cosw, sinw = np.cos(w), np.sin(w)
    if kind == "pk":
        alpha = sinw / (2 * q)
        b = [1 + alpha * a, -2 * cosw, 1 - alpha * a]
        den = [1 + alpha / a, -2 * cosw, 1 - alpha / a]
    else:
        sq = 2 * np.sqrt(a) * (sinw / 2 * np.sqrt(2))
        if kind == "ls":
            b = [a * ((a + 1) - (a - 1) * cosw + sq), 2 * a * ((a - 1) - (a + 1) * cosw), a * ((a + 1) - (a - 1) * cosw - sq)]
            den = [(a + 1) + (a - 1) * cosw + sq, -2 * ((a - 1) + (a + 1) * cosw), (a + 1) + (a - 1) * cosw - sq]
        else:
            b = [a * ((a + 1) + (a - 1) * cosw + sq), -2 * a * ((a - 1) + (a + 1) * cosw), a * ((a + 1) + (a - 1) * cosw - sq)]
            den = [(a + 1) - (a - 1) * cosw + sq, 2 * ((a - 1) - (a + 1) * cosw), (a + 1) - (a - 1) * cosw - sq]
    return [b[0] / den[0], b[1] / den[0], b[2] / den[0], 1.0, den[1] / den[0], den[2] / den[0]]


def soft_limit(x: np.ndarray, knee: float = 0.85) -> np.ndarray:
    """Untouched below the knee; above it peaks bend smoothly towards 1.0 instead of clipping."""
    a = np.abs(x)
    r = 1.0 - knee
    return np.where(a <= knee, x, np.sign(x) * (knee + r * np.tanh((a - knee) / r)))


class Equalizer:
    """Band gains, volume and effects, shared between the audio loop and the web page."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gains = [0.0] * len(BANDS)
        self.volume = 0.0
        self.fx = dict(DEFAULT_FX)
        self.version = 0
        try:
            saved = json.loads(SAVED.read_text(encoding="utf-8"))
            self.set(saved["gains"], saved["volume"], saved.get("fx"))
        except (OSError, ValueError, KeyError):
            pass

    def set(self, gains: list, volume: float, fx: dict | None = None) -> None:
        gains = [max(-MAX_DB, min(MAX_DB, float(g))) for g in gains][: len(BANDS)]
        with self.lock:
            self.gains = gains + [0.0] * (len(BANDS) - len(gains))
            self.volume = max(-24.0, min(12.0, float(volume)))
            for key, (lo, hi) in FX_RANGE.items():
                if fx and key in fx:
                    self.fx[key] = max(lo, min(hi, float(fx[key])))
            self.version += 1
            data = {"gains": self.gains, "volume": self.volume, "fx": dict(self.fx)}
        try:
            SAVED.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def snapshot(self) -> tuple[list[float], float, dict, int]:
        with self.lock:
            return list(self.gains), self.volume, dict(self.fx), self.version

    def state(self) -> dict:
        """What the web page needs: settings and the combined EQ + bass-cut response curve."""
        gains, volume, fx, _ = self.snapshot()
        sos = np.vstack([np.array([biquad(kind, g, f) for (f, kind), g in zip(BANDS, gains)]), cut_sos(fx["cut"])])
        freqs = np.logspace(np.log10(30), np.log10(11_000), 120)
        _, h = sosfreqz(sos, worN=2 * np.pi * freqs / RATE)
        return {"bands": [f for f, _ in BANDS], "gains": gains, "volume": volume, "fx": fx, "max": MAX_DB,
                "ranges": FX_RANGE, "freqs": freqs.round(1).tolist(),
                "curve": (20 * np.log10(np.abs(h) + 1e-9)).round(2).tolist()}


def cut_sos(freq: float) -> np.ndarray:
    return butter(4, freq, "highpass", fs=RATE, output="sos")


class Compressor:
    """Feed-forward peak compressor on 5 ms slices; gain is smoothed (fast attack, slow release) and
    interpolated per sample. Peaks that slip through during the attack are caught by the soft limiter."""

    def __init__(self) -> None:
        self.reduction_db = 0.0
        self._att = float(np.exp(-SUB / RATE / ATTACK_S))
        self._rel = float(np.exp(-SUB / RATE / RELEASE_S))

    def process(self, x: np.ndarray, amount: float) -> np.ndarray:
        a = amount / 100.0
        n = len(x) // SUB
        if a <= 0.0 or n == 0 or len(x) % SUB:
            self.reduction_db = 0.0
            return x
        threshold = -6.0 - 18.0 * a                   # dBFS
        ratio = 1.0 + 3.0 * a
        makeup = 0.6 * -threshold * (1.0 - 1.0 / ratio)
        level = 20 * np.log10(np.abs(x).reshape(n, SUB).max(axis=1) + 1e-6)
        target = -np.maximum(level - threshold, 0.0) * (1.0 - 1.0 / ratio)   # wanted reduction, dB (<= 0)
        gains = np.empty(n)
        g = self.reduction_db
        for i in range(n):
            coef = self._att if target[i] < g else self._rel
            g = coef * g + (1 - coef) * target[i]
            gains[i] = g
        self.reduction_db = g
        centres = np.arange(n) * SUB + SUB / 2
        per_sample = np.interp(np.arange(len(x)), centres, gains + makeup)
        return x * 10 ** (per_sample / 20)


class Limiter:
    """Look-ahead peak limiter: the gain starts dropping `LOOK` samples before a peak arrives, so peaks are
    turned down instead of being bent by a waveshaper (which sounds like distortion). Output is delayed by LOOK."""

    def __init__(self, ceiling: float = 0.95, look: int = LOOK) -> None:
        self.ceiling, self.look = ceiling, look
        self._hist = np.zeros(look)
        self._gain = 1.0
        self._att = 1.0 - float(np.exp(-4.5 / look))         # 4.5 time constants fit into the look-ahead: ~1 % of the cut is left
        self._rel = float(np.exp(-1.0 / (RATE * 0.08)))      # 80 ms release

    def process(self, x: np.ndarray) -> np.ndarray:
        n, look = len(x), self.look
        ext = np.concatenate([self._hist, x])                 # output sample i is ext[i], i.e. delayed by `look`
        self._hist = ext[n:]
        ahead = maximum_filter1d(np.abs(ext), size=look + 1, mode="constant", cval=0.0, origin=-(look // 2))[:n]
        target = np.minimum(1.0, self.ceiling / np.maximum(ahead, 1e-9))
        gains = np.empty(n)
        g, att, rel = self._gain, self._att, self._rel
        for i in range(n):
            t = target[i]
            g = g + (t - g) * att if t < g else t - (t - g) * rel
            gains[i] = g
        self._gain = g
        return ext[:n] * gains


class Chain:
    """EQ → bass cut + virtual bass → compressor → volume → look-ahead limiter."""

    def __init__(self, eq: Equalizer) -> None:
        self.eq = eq
        self._version = -1
        self._eq_sos = np.zeros((len(BANDS), 6))
        self._eq_zi = np.zeros((len(BANDS), 2))
        self._cut_zi = np.zeros((2, 2))
        self._src = butter(2, [40, 130], "bandpass", fs=RATE, output="sos")      # the bass we cannot play
        self._src_zi = np.zeros((len(self._src), 2))
        self._harm = butter(2, [110, 330], "bandpass", fs=RATE, output="sos")    # its harmonics we can
        self._harm_zi = np.zeros((len(self._harm), 2))
        self._comp = Compressor()
        self._limiter = Limiter()
        self._cut = cut_sos(DEFAULT_FX["cut"])
        self._fx = dict(DEFAULT_FX)
        self._pre = 1.0

    def _refresh(self) -> None:
        gains, volume, fx, version = self.eq.snapshot()
        if version == self._version:
            return
        self._version = version
        self._eq_sos = np.array([biquad(kind, g, f) for (f, kind), g in zip(BANDS, gains)])
        self._cut = cut_sos(fx["cut"])
        self._fx = fx
        # a boost eats headroom (half of the biggest one is taken back), so does the bass we rebuild
        pre_db = volume - 0.5 * max(0.0, max(gains)) - 3.0 * fx["vbass"] / 100.0
        self._pre = 10 ** (pre_db / 20)

    def process(self, mono: np.ndarray) -> np.ndarray:
        self._refresh()
        x, self._eq_zi = sosfilt(self._eq_sos, mono, zi=self._eq_zi)
        main, self._cut_zi = sosfilt(self._cut, x, zi=self._cut_zi)
        vbass = self._fx["vbass"] / 100.0
        if vbass > 0.0:
            low, self._src_zi = sosfilt(self._src, x, zi=self._src_zi)
            # tanh gives the odd harmonics (3f), |x| the even ones (2f); both land inside the second band
            shaped = 0.5 * np.tanh(4.0 * low) + 1.0 * np.abs(low)
            harm, self._harm_zi = sosfilt(self._harm, shaped, zi=self._harm_zi)
            main = main + harm * (2.5 * vbass)
        main = self._comp.process(main, self._fx["comp"])
        return soft_limit(self._limiter.process(main * self._pre), knee=0.97)   # the soft limit is only a safety net
