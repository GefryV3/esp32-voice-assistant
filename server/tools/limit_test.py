"""Find how loud the speaker can play cleanly: where the amplifier or the cone starts to distort.

    python tools/limit_test.py --check        # only listen to the room (no sound): are the microphones alive
    python tools/limit_test.py                # staircases of tone bursts at 100, 250 and 1000 Hz, with --mic / --mic2

For every frequency the speaker plays 8 bursts that get 3 dB louder each (from -24 to -3 dBFS, straight into the
firmware's music mode at 100 % volume, no equalizer). Every burst is recorded and analysed: how much of it comes back as
harmonics (distortion) and how much the level stops following the input (compression). The result is the loudest
level that stays clean, and what to set the "Потолок усилителя" and "Защита низов" sliders to. The tones get LOUD:
keep ears away from the speaker. Stop tools/stream.py first (it holds the speaker).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import warnings
from pathlib import Path

import numpy as np
import soundcard as sc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate as cal_io  # noqa: E402
from jarvis.config import read_env_file  # noqa: E402

RATE = 48_000
LEVELS_DB = [-24, -21, -18, -15, -12, -9, -6, -3]
FREQS = [100, 250, 1000]
BURST_S, GAP_S, LEAD_S = 0.9, 0.4, 0.6
CHIRP_S = 0.15
THD_WARN, THD_BAD = 0.03, 0.10                    # 3 % and 10 % of the fundamental in harmonics


def chirp() -> np.ndarray:
    t = np.arange(int(CHIRP_S * RATE)) / RATE
    k = np.log(4000 / 500)
    x = np.sin(2 * np.pi * 500 * CHIRP_S / k * (np.exp(t / CHIRP_S * k) - 1)) * 10 ** (-20 / 20)
    x[:200] *= np.linspace(0, 1, 200)
    x[-200:] *= np.linspace(1, 0, 200)
    return x


def staircase(freq: float) -> tuple[np.ndarray, list[int]]:
    """chirp, then tone bursts at rising levels. Returns the signal and the start sample of every burst."""
    parts, starts, pos = [chirp(), np.zeros(int(LEAD_S * RATE))], [], int((CHIRP_S + LEAD_S) * RATE)
    n = int(BURST_S * RATE)
    t = np.arange(n) / RATE
    fade = int(0.03 * RATE)
    env = np.ones(n)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    for db in LEVELS_DB:
        starts.append(pos)
        parts += [np.sin(2 * np.pi * freq * t) * env * 10 ** (db / 20), np.zeros(int(GAP_S * RATE))]
        pos += n + int(GAP_S * RATE)
    return np.concatenate(parts), starts


def find_start(rec: np.ndarray) -> int:
    """Sample at which the chirp starts in the recording (cross-correlation)."""
    ref = chirp()
    n = 1 << int(np.ceil(np.log2(len(rec) + len(ref))))
    corr = np.fft.irfft(np.fft.rfft(rec, n) * np.conj(np.fft.rfft(ref, n)), n)[: len(rec)]
    return int(np.argmax(np.abs(corr)))


def analyse(seg: np.ndarray, freq: float) -> dict:
    """Fundamental amplitude, harmonic distortion (noise floor removed) and mic clipping for one burst."""
    seg = seg * np.hanning(len(seg))
    n = 1 << 16
    spec = np.abs(np.fft.rfft(seg, n)) * 2 / np.sum(np.hanning(len(seg)))
    hz = RATE / n

    def peak(f: float) -> float:
        i = int(round(f / hz))
        return float(spec[max(0, i - 6): i + 7].max())

    def floor(f: float) -> float:
        i = int(round(f / hz))
        around = np.concatenate([spec[max(0, i - 400): max(0, i - 40)], spec[i + 40: i + 400]])
        return float(np.median(around)) if len(around) else 0.0

    fundamental = peak(freq)
    harmonics = [max(peak(h * freq) ** 2 - floor(h * freq) ** 2, 0.0) for h in range(2, 9) if h * freq < RATE / 2 - 500]
    return {"a1": fundamental, "thd": float(np.sqrt(sum(harmonics)) / (fundamental + 1e-12)),
            "snr_db": 20 * np.log10((fundamental + 1e-12) / (floor(freq) + 1e-12))}


async def run_frequency(url: str, token: str, mics: list, freq: float) -> list[list[dict]]:
    """For every microphone: one analysis per burst."""
    signal, starts = staircase(freq)
    recs = await cal_io.play_and_record(url, token, mics, signal, None, extra=3.5)
    out = []
    for rec in recs:
        if np.abs(rec).max() > 0.98:
            print("  this microphone clips: lower its gain in Windows")
        origin = find_start(rec[: int(6 * RATE)])
        rows = []
        for db, s in zip(LEVELS_DB, starts):
            a = origin + s + int(0.25 * RATE)
            rows.append({"db": db, **analyse(rec[a: a + int(0.55 * RATE)], freq)})
        out.append(rows)
    return out


def report(freq: float, per_mic: list[list[dict]], names: list[str]) -> float | None:
    """Print the table for one frequency; return the level (dBFS) at which distortion passes THD_WARN, if it does."""
    print(f"\n{freq} Hz")
    print("  level   " + "   ".join(f"{n[:10]:>26}" for n in names))
    print("  (dBFS)  " + "   ".join(f"{'out dB   comp   THD %':>26}" for _ in names))
    base = [np.mean([r["a1"] for r in rows[:2]]) / np.mean([10 ** (LEVELS_DB[0] / 20), 10 ** (LEVELS_DB[1] / 20)])
            for rows in per_mic]
    worst = None
    for i, db in enumerate(LEVELS_DB):
        cells = []
        for m, rows in enumerate(per_mic):
            r = rows[i]
            gain = r["a1"] / (10 ** (db / 20) * base[m] + 1e-12)
            comp = 20 * np.log10(gain + 1e-12)
            cells.append(f"{20 * np.log10(r['a1'] + 1e-12):7.1f} {comp:+6.1f} {r['thd'] * 100:7.1f}" + ("  !" if r["thd"] > THD_WARN else ""))
        print(f"  {db:5d}   " + "   ".join(f"{c:>26}" for c in cells))
        if worst is None and np.median([rows[i]["thd"] for rows in per_mic]) > THD_WARN:
            worst = float(db)
    return worst


async def main_async(args) -> None:
    mics = [cal_io.find_mic(args.mic)] + ([cal_io.find_mic(args.mic2)] if args.mic2 else [])
    names = [m.name for m in mics]
    print("microphones: " + "; ".join(names))
    loop = asyncio.get_running_loop()
    for m in mics:
        ambient = await loop.run_in_executor(None, cal_io.record, m, 2.0)
        print(f"  {m.name[:30]}: room noise {cal_io.dbfs(ambient):.1f} dBFS")
        if not args.check and cal_io.dbfs(ambient) > -45:
            raise SystemExit("the room is too noisy: quieter, please")
    if args.check:
        return
    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    url = args.url or f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin"
    token = args.token or env.get("JARVIS_DEVICE_TOKEN", "")
    limits: dict[float, float | None] = {}
    for freq in args.freqs:
        print(f"\nplaying {freq} Hz staircase, ~13 s ...", flush=True)
        per_mic = await run_frequency(url, token, mics, freq)
        limits[freq] = report(freq, per_mic, names)
    print("\nsummary (level at which more than 3 % of the sound turns into harmonics):")
    for freq, lim in limits.items():
        print(f"  {freq:5.0f} Hz: " + (f"{lim:+.0f} dBFS" if lim is not None else "stays clean up to -3 dBFS"))
    found = [v for v in limits.values() if v is not None]
    clean = min(found) - 1.5 if found else -3.0
    peak = 10 ** (clean / 20)
    print(f"\nthe cleanest peak level for the whole range: about {clean:+.1f} dBFS (amplitude {peak:.2f})")
    print(f"  → 'Потолок усилителя' (ceil) ≈ {min(100, int(100 * peak / 0.95))} % (the stream's limiter peaks at 0.95 × ceil)")
    low = limits.get(float(args.freqs[0]))
    if low is not None:
        low_peak = 10 ** ((low - 1.5) / 20)
        print(f"  → 'Защита низов' (guard) ≈ {max(0, min(100, int((0.5 - low_peak) / 0.42 * 100)))} % "
              f"(the bass band is held at {low_peak:.2f})")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mic", default="fifine")
    p.add_argument("--mic2", help="a second microphone, e.g. onboard: the two cross-check each other")
    p.add_argument("--freqs", type=float, nargs="+", default=FREQS)
    p.add_argument("--check", action="store_true", help="only record the room, play nothing")
    p.add_argument("--url")
    p.add_argument("--token")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
