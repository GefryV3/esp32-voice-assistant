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
from scipy.signal import butter, firwin, firwin2, freqz, lfilter, sosfilt, sosfreqz

from stream_tonal import TonalBass, TransientPunch

RATE = 48_000
SAVED = Path.home() / ".jarvis_stream_eq.json"
CORRECTION = Path.home() / ".jarvis_stream_correction.json"      # measured speaker correction (tools/calibrate.py)
PRESETS = Path.home() / ".jarvis_stream_presets"                       # named snapshots of all settings + correction
FIR_TAPS = 2049                                                  # linear phase: 21 ms delay, ~23 Hz resolution

# (centre Hz, kind): the lowest and highest bands are shelves, the rest are peaks of about an octave
BANDS = [(63, "ls"), (125, "pk"), (250, "pk"), (500, "pk"), (1000, "pk"), (2000, "pk"), (4000, "pk"), (8000, "hs")]
MAX_DB = 15.0
DEFAULT_FX = {"vbass": 0.0, "comp": 0.0, "cut": 20.0, "corr": 100.0, "guard": 0.0, "ceil": 100.0, "tbass": 0.0, "bcomp": 0.0, "tpunch": 0.0}   # %, %, Hz (20 — practically off), % of the measured correction
FX_RANGE = {"vbass": (0.0, 100.0), "comp": (0.0, 100.0), "cut": (20.0, 160.0), "corr": (0.0, 200.0), "guard": (0.0, 100.0), "ceil": (40.0, 100.0), "tbass": (0.0, 100.0), "bcomp": (0.0, 100.0), "tpunch": (0.0, 100.0)}

SUB = 240                    # compressor works on 5 ms slices
LOOK = 240                   # limiter looks 5 ms ahead (and delays the sound by as much)
GUARD_HZ = 150.0             # speaker protection works on everything below this
GUARD_TAPS = 513             # linear-phase split: the band edges are clean and the two bands stay aligned
GUARD_DELAY = (GUARD_TAPS - 1) // 2 + LOOK          # total latency of the guard stage and the band limiters, samples
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


def correction_fir(freqs, db, strength: float = 1.0) -> np.ndarray:
    """Linear-phase FIR with the given magnitude curve (dB at `freqs`, Hz), scaled by `strength` 0…1."""
    freqs = np.asarray(freqs, float)
    gain = np.asarray(db, float) * strength
    grid = np.concatenate([[0.0], freqs, [RATE / 2]]) / (RATE / 2)
    levels = 10 ** (np.concatenate([[gain[0]], gain, [gain[-1]]]) / 20)
    return firwin2(FIR_TAPS, grid, levels)


class Equalizer:
    """Band gains, volume and effects, shared between the audio loop and the web page."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gains = [0.0] * len(BANDS)
        self.volume = 0.0
        self.fx = dict(DEFAULT_FX)
        self.version = 0
        self.correction: tuple[list[float], list[float]] | None = None
        try:
            c = json.loads(CORRECTION.read_text(encoding="utf-8"))
            self.correction = (c["freqs"], c["db"])
        except (OSError, ValueError, KeyError):
            pass
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

    def set_correction(self, freqs: list[float], db: list[float]) -> None:
        with self.lock:
            self.correction = ([float(f) for f in freqs], [float(g) for g in db])
            self.version += 1
        CORRECTION.write_text(json.dumps({"freqs": list(map(float, freqs)), "db": list(map(float, db))}), encoding="utf-8")

    @staticmethod
    def _preset_path(name: str) -> Path:
        safe = "".join(c for c in name if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("bad preset name")
        return PRESETS / f"{safe}.json"

    def save_preset(self, name: str) -> Path:
        gains, volume, fx, _ = self.snapshot()
        corr = {"freqs": self.correction[0], "db": self.correction[1]} if self.correction is not None else None
        path = self._preset_path(name)
        PRESETS.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"gains": gains, "volume": volume, "fx": fx, "correction": corr}, indent=1), encoding="utf-8")
        return path

    def load_preset(self, name: str) -> None:
        data = json.loads(self._preset_path(name).read_text(encoding="utf-8"))
        if data.get("correction"):
            self.set_correction(data["correction"]["freqs"], data["correction"]["db"])
        self.set(data["gains"], data["volume"], data["fx"])

    def snapshot(self) -> tuple[list[float], float, dict, int]:
        with self.lock:
            return list(self.gains), self.volume, dict(self.fx), self.version

    def state(self) -> dict:
        """What the web page needs: settings and the combined EQ + bass-cut response curve."""
        gains, volume, fx, _ = self.snapshot()
        sos = np.vstack([np.array([biquad(kind, g, f) for (f, kind), g in zip(BANDS, gains)]), cut_sos(fx["cut"])])
        freqs = np.logspace(np.log10(30), np.log10(11_000), 120)
        _, h = sosfreqz(sos, worN=2 * np.pi * freqs / RATE)
        curve = 20 * np.log10(np.abs(h) + 1e-9)
        if self.correction is not None and fx["corr"] > 0:
            _, hc = freqz(correction_fir(*self.correction, fx["corr"] / 100.0), worN=2 * np.pi * freqs / RATE)
            curve = curve + 20 * np.log10(np.abs(hc) + 1e-9)
        return {"bands": [f for f, _ in BANDS], "gains": gains, "volume": volume, "fx": fx, "max": MAX_DB,
                "ranges": FX_RANGE, "freqs": freqs.round(1).tolist(), "curve": curve.round(2).tolist(),
                "has_correction": self.correction is not None}


def cut_sos(freq: float) -> np.ndarray:
    return butter(4, freq, "highpass", fs=RATE, output="sos")


class Compressor:
    """Feed-forward peak compressor on 5 ms slices; gain is smoothed (fast attack, slow release) and
    interpolated per sample. Peaks that slip through during the attack are caught by the soft limiter."""

    def __init__(self, sub: int = SUB, attack: float = ATTACK_S, release: float = RELEASE_S,
                 top_db: float = -6.0, depth_db: float = 18.0, makeup_k: float = 0.6) -> None:
        """`top_db`: threshold at 0 % amount; it drops by `depth_db` at 100 %. `makeup_k`: share of the average
        reduction given back as gain (bigger = quiet parts are raised more)."""
        self.sub, self.top_db, self.depth_db, self.makeup_k = sub, top_db, depth_db, makeup_k
        self.reduction_db = 0.0
        self._att = float(np.exp(-sub / RATE / attack))
        self._rel = float(np.exp(-sub / RATE / release))

    def process(self, x: np.ndarray, amount: float) -> np.ndarray:
        SUB = self.sub
        a = amount / 100.0
        n = len(x) // SUB
        if a <= 0.0 or n == 0 or len(x) % SUB:
            self.reduction_db = 0.0
            return x
        threshold = self.top_db - self.depth_db * a   # dBFS
        ratio = 1.0 + 3.0 * a
        makeup = self.makeup_k * -threshold * (1.0 - 1.0 / ratio)
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

    def process(self, x: np.ndarray, ceiling: np.ndarray | None = None) -> np.ndarray:
        """`ceiling`: an optional ceiling per output sample (the room left by another band) instead of the fixed one."""
        n, look = len(x), self.look
        ext = np.concatenate([self._hist, x])                 # output sample i is ext[i], i.e. delayed by `look`
        self._hist = ext[n:]
        # the ceiling of the samples that are not output yet is not known: hold the last one
        ceil_ext = self.ceiling if ceiling is None else np.concatenate([ceiling, np.full(look, ceiling[-1])])
        ahead = maximum_filter1d(np.abs(ext) / ceil_ext, size=look + 1, mode="constant", cval=0.0, origin=-(look // 2))[:n]
        target = np.minimum(1.0, 1.0 / np.maximum(ahead, 1e-9))
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
        self._src = butter(4, [40, 130], "bandpass", fs=RATE, output="sos")      # the bass we cannot play
        self._src_zi = np.zeros((len(self._src), 2))
        self._harm = butter(4, [90, 360], "bandpass", fs=RATE, output="sos")     # its harmonics we can
        self._harm_zi = np.zeros((len(self._harm), 2))
        self._comp = Compressor()
        self._high_limiter = Limiter()                            # the band above GUARD_HZ; its ceiling is what the bass leaves
        self._total = 0.95                                        # the most the amplifier is asked to play, peak
        # clean virtual bass (tracked notes + oscillators); it adds TonalBass.D samples of latency, so the main
        # signal always waits as long, whether the effect is on or not
        self._tonal = TonalBass()
        self._punch = TransientPunch()
        self._tonal_lp = butter(4, 160.0, "lowpass", fs=RATE, output="sos")
        self._tonal_lp_zi = np.zeros((len(self._tonal_lp), 2))
        self._main_delay = np.zeros(TonalBass.D)
        # speaker protection: the band below GUARD_HZ gets its own limiter so that strong bass cannot slam the cone
        self._lp = firwin(GUARD_TAPS, GUARD_HZ, fs=RATE)
        self._lp_zi = np.zeros(GUARD_TAPS - 1)
        self._bass_limiter = Limiter(ceiling=10.0)
        # dynamic bass: slow compressor with make-up on the low band only. Quiet bass is raised a lot (the amplifier has
        # headroom there), loud bass is not (it has none). Slow, so the gain does not modulate the bass waveform itself.
        self._bass_comp = Compressor(sub=960, attack=0.04, release=0.3, top_db=-24.0, depth_db=14.0, makeup_k=0.9)
        self._y_delay = np.zeros((GUARD_TAPS - 1) // 2)      # the full signal waits as long as the FIR low band
        self._cut = cut_sos(DEFAULT_FX["cut"])
        self._fx = dict(DEFAULT_FX)
        self._pre = 1.0
        self._fir: np.ndarray | None = None
        self._fir_zi = np.zeros(FIR_TAPS - 1)

    def _refresh(self) -> None:
        gains, volume, fx, version = self.eq.snapshot()
        if version == self._version:
            return
        self._version = version
        self._eq_sos = np.array([biquad(kind, g, f) for (f, kind), g in zip(BANDS, gains)])
        self._cut = cut_sos(fx["cut"])
        self._fx = fx
        # a boost eats headroom (half of the biggest one is taken back), so does the bass we rebuild
        pre_db = volume - 0.5 * max(0.0, max(gains)) - 3.0 * fx["vbass"] / 100.0 - 2.0 * fx["tbass"] / 100.0
        self._fir = None
        if self.eq.correction is not None and fx["corr"] > 0:
            strength = fx["corr"] / 100.0
            self._fir = correction_fir(*self.eq.correction, strength)
            pre_db -= 0.6 * max(0.0, max(self.eq.correction[1]) * strength)
        self._pre = 10 ** (pre_db / 20)
        guard = fx["guard"] / 100.0
        self._total = 0.95 * fx["ceil"] / 100.0                                        # what the amplifier can play cleanly
        # the bass band's own ceiling: 40 % -> 0.33, 100 % -> 0.08; with protection off it may use 70 % of the total
        self._bass_limiter.ceiling = 0.7 * self._total if guard <= 0 else min(0.5 - 0.42 * guard, 0.7 * self._total)

    def process(self, mono: np.ndarray) -> np.ndarray:
        self._refresh()
        if self._fir is not None:
            mono, self._fir_zi = lfilter(self._fir, 1.0, mono, zi=self._fir_zi)
        x, self._eq_zi = sosfilt(self._eq_sos, mono, zi=self._eq_zi)
        main, self._cut_zi = sosfilt(self._cut, x, zi=self._cut_zi)
        vbass = self._fx["vbass"] / 100.0
        if vbass > 0.0:
            low, self._src_zi = sosfilt(self._src, x, zi=self._src_zi)
            # u^2 gives the 2nd harmonic, u^3 the 3rd. Both grow faster than the level itself, so quiet bass and
            # leaking noise make almost nothing (no hiss), loud bass makes plenty. No rectifier: it was harsh.
            u = low / 0.25
            shaped = 0.25 * (0.7 * u ** 2 + 0.5 * u ** 3)
            harm, self._harm_zi = sosfilt(self._harm, shaped, zi=self._harm_zi)
            main = main + harm * (2.0 * vbass)
        bass_band, self._tonal_lp_zi = sosfilt(self._tonal_lp, x, zi=self._tonal_lp_zi)
        main = main + self._punch.process(bass_band) * (2.0 * self._fx["tpunch"] / 100.0)   # no latency: before the delay
        delayed = np.concatenate([self._main_delay, main])
        self._main_delay = delayed[len(main):]
        main = delayed[: len(main)]
        harmonics = self._tonal.process(bass_band)             # always runs, so the note tracker stays warm
        main = main + harmonics * (1.5 * self._fx["tbass"] / 100.0)
        main = self._comp.process(main, self._fx["comp"])
        y = main * self._pre
        low, self._lp_zi = lfilter(self._lp, 1.0, y, zi=self._lp_zi)
        aligned = np.concatenate([self._y_delay, y])
        self._y_delay = aligned[len(y):]
        high = aligned[:len(y)] - low
        low = self._bass_comp.process(low, self._fx["bcomp"])
        # priority limiting: the bass keeps its level up to its own ceiling, and the band above it gets only the room
        # the bass leaves. A single limiter on the sum turned the bass down whenever it dominated the peaks, so the
        # louder it played the less bass there was.
        bass = self._bass_limiter.process(low)
        envelope = maximum_filter1d(np.abs(bass), size=int(0.02 * RATE), mode="nearest")        # 20 ms bass peak
        room = np.clip(self._total - envelope, 0.12 * self._total, self._total)
        y = bass + self._high_limiter.process(high, room)
        return soft_limit(y, knee=self._total * 1.02)                                  # only a safety net
