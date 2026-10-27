"""Clean virtual bass: track the bass notes and synthesise their harmonics with oscillators.

The nonlinear-device approach (clipping the bass to make harmonics) is cheap but every pair of simultaneous notes
also makes intermodulation products, which is the "mud" and "rasp" people hear. Here the bass band is analysed with
a phase vocoder (STFT, instantaneous frequency from the phase advance between frames), every spectral peak becomes
a track with a frequency and an amplitude, and a bank of oscillators plays the 2nd, 3rd and 4th harmonic of each track.
Oscillators do not interact, so no intermodulation. The price is a latency of D samples (the caller delays the rest of
the signal by the same amount) and some smearing of very short transients (a kick lasts about as long as one frame).

See the DAFx-20 "Virtual Bass System with Fuzzy Separation of Tones and Transients" for the family of methods.
"""

from __future__ import annotations

import numpy as np

RATE = 48_000
HARMONICS = ((2, 1.0), (3, 0.6), (4, 0.25))        # (multiple of the fundamental, relative level)


class TonalBass:
    N = 4096                         # analysis frame: 85 ms, 11.7 Hz per bin
    H = 512                          # hop: 10.7 ms
    LO, HI = 35.0, 125.0             # the notes we cannot play, Hz
    D = N // 2 + H // 2              # latency of the harmonics relative to the input, samples
    MIN_AMPLITUDE = 0.002            # quieter peaks are noise, not notes
    MATCH_HZ = 8.0                   # a peak within this of an existing track continues it

    def __init__(self, rate: int = RATE) -> None:
        self.rate = rate
        self.win = np.hanning(self.N + 1)[:-1]
        self.frame = np.zeros(self.N)
        self.inbuf = np.zeros(0)
        self.consumed = 0
        self.total_in = 0
        self.prev_phase: np.ndarray | None = None
        self.k0 = max(1, int(np.floor(self.LO * self.N / rate)))
        self.k1 = int(np.ceil(self.HI * self.N / rate)) + 1
        self.tracks: list[dict] = []
        self.out = np.zeros(0)
        self.out_start = self.H - self.D            # time (input sample index) of out[0]

    def process(self, x: np.ndarray) -> np.ndarray:
        """Harmonics for the same number of samples, as they should be heard D samples after `x`."""
        n = len(x)
        self.inbuf = np.concatenate([self.inbuf, x])
        self.total_in += n
        while len(self.inbuf) >= self.H:
            chunk, self.inbuf = self.inbuf[: self.H], self.inbuf[self.H:]
            self.frame = np.concatenate([self.frame[self.H:], chunk])
            self.consumed += self.H
            self.out = np.concatenate([self.out, self._hop()])
        t1 = self.total_in - self.D
        t0 = t1 - n
        res = np.zeros(n)
        lo, hi = max(t0, self.out_start), min(t1, self.out_start + len(self.out))
        if hi > lo:
            res[lo - t0: hi - t0] = self.out[lo - self.out_start: hi - self.out_start]
        keep_from = max(t1, self.out_start)
        if keep_from > self.out_start:
            self.out = self.out[keep_from - self.out_start:]
            self.out_start = keep_from
        return res

    def _hop(self) -> np.ndarray:
        spectrum = np.fft.rfft(self.frame * self.win)
        mag, phase = np.abs(spectrum), np.angle(spectrum)
        peaks: list[tuple[float, float]] = []
        if self.prev_phase is not None:
            for k in range(self.k0, self.k1):
                if mag[k] >= mag[k - 1] and mag[k] > mag[k + 1]:
                    amp = 4.0 * mag[k] / self.N                      # a Hann-windowed sine of amplitude a peaks at a*N/4
                    if amp < self.MIN_AMPLITUDE:
                        continue
                    advance = phase[k] - self.prev_phase[k] - 2 * np.pi * k * self.H / self.N
                    advance = (advance + np.pi) % (2 * np.pi) - np.pi
                    freq = (k + advance * self.N / (2 * np.pi * self.H)) * self.rate / self.N
                    if self.LO <= freq <= self.HI:
                        peaks.append((float(freq), float(amp)))
        self.prev_phase = phase
        return self._synthesise(peaks)

    def _synthesise(self, peaks: list[tuple[float, float]]) -> np.ndarray:
        out = np.zeros(self.H)
        used: set[int] = set()
        new_tracks = []
        for freq, amp in sorted(peaks, key=lambda p: -p[1]):
            best = None
            for i, tr in enumerate(self.tracks):
                if i not in used and abs(tr["f"] - freq) < self.MATCH_HZ and (
                        best is None or abs(tr["f"] - freq) < abs(self.tracks[best]["f"] - freq)):
                    best = i
            if best is None:
                track = {"f": freq, "a": 0.0, "ph": 0.0}          # a new note fades in
            else:
                used.add(best)
                track = self.tracks[best]
            new_tracks.append(self._oscillate(track, freq, amp, out))
        for i, tr in enumerate(self.tracks):
            if i not in used and tr["a"] > 1e-4:
                new_tracks.append(self._oscillate(tr, tr["f"], 0.0, out))   # a note that ended fades out
        self.tracks = new_tracks
        return out

    def _oscillate(self, track: dict, freq: float, amp: float, out: np.ndarray) -> dict:
        ramp = (np.arange(self.H) + 1) / self.H
        freqs = track["f"] + (freq - track["f"]) * ramp
        amps = track["a"] + (amp - track["a"]) * ramp
        phase = track["ph"] + 2 * np.pi * np.cumsum(freqs) / self.rate
        for multiple, level in HARMONICS:
            out += level * amps * np.sin(multiple * phase)
        return {"f": freq, "a": amp, "ph": float(phase[-1] % (2 * np.pi))}


class TransientPunch:
    """The transient half of the hybrid virtual bass: a nonlinear generator that is only allowed to speak while a bass
    note is starting (a kick, a plucked string). Tonal harmonics come from TonalBass; they smear attacks because the
    analysis frame is long. Intermodulation is not an issue here: it exists for a few tens of milliseconds only.

    The bass band is normalised by its own fast envelope (so the harmonics do not depend on the level), saturated with
    tanh (odd harmonics), band-passed above the fundamentals, scaled back by the envelope and gated by an onset
    detector: fast envelope divided by slow envelope."""

    def __init__(self, rate: int = RATE) -> None:
        from scipy.signal import butter

        self.rate = rate
        self._fast_a = float(np.exp(-1.0 / (0.004 * rate)))      # 4 ms
        self._slow_a = float(np.exp(-1.0 / (0.120 * rate)))      # 120 ms
        self._gate_a = float(np.exp(-1.0 / (0.010 * rate)))      # 10 ms smoothing of the gate
        self._bp = butter(4, [130.0, 450.0], "bandpass", fs=rate, output="sos")
        self._zi = {"fast": np.zeros(1), "slow": np.zeros(1), "gate": np.zeros(1), "bp": np.zeros((len(self._bp), 2))}

    def process(self, low: np.ndarray) -> np.ndarray:
        from scipy.signal import lfilter, sosfilt

        mag = np.abs(low)
        fast, self._zi["fast"] = lfilter([1 - self._fast_a], [1, -self._fast_a], mag, zi=self._zi["fast"])
        slow, self._zi["slow"] = lfilter([1 - self._slow_a], [1, -self._slow_a], mag, zi=self._zi["slow"])
        onset = np.clip((fast / (slow + 2e-3) - 1.3) / 1.0, 0.0, 1.0)          # 0 on sustained bass, 1 on an attack
        gate, self._zi["gate"] = lfilter([1 - self._gate_a], [1, -self._gate_a], onset, zi=self._zi["gate"])
        normalised = low / (fast + 0.004)
        shaped, self._zi["bp"] = sosfilt(self._bp, np.tanh(2.5 * normalised), zi=self._zi["bp"])
        return shaped * fast * gate
