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
import re
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import soundcard as sc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate as cal_io  # noqa: E402
from jarvis.config import read_env_file  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402

RATE = 48_000
LEVELS_DB = [-24, -21, -18, -15, -12, -9, -6, -3]
FREQS = [100, 250, 1000]
BURST_S, GAP_S, LEAD_S = 0.9, 0.4, 0.6
CHIRP_S = 0.15
THD_WARN, THD_BAD = 0.03, 0.10                    # 3 % and 10 % of the fundamental in harmonics


def chirp() -> np.ndarray:
    t = np.arange(int(CHIRP_S * RATE)) / RATE
    k = np.log(4000 / 500)
    x = np.sin(2 * np.pi * 500 * CHIRP_S / k * (np.exp(t / CHIRP_S * k) - 1)) * 10 ** (-14 / 20)
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
    """Sample at which the chirp starts in the recording (cross-correlation). Only the first 3 s are searched: the
    loud tone bursts that follow would correlate with a part of the chirp and could win."""
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


async def speaker_uptime(url: str, token: str) -> int | None:
    """Seconds since the speaker booted (asks the server), None if it is not connected."""
    try:
        async with connect(url, max_size=None) as ws:
            await ws.send(token)
            await ws.send("devices")
            m = re.search(r"'uptime': (\d+)", await asyncio.wait_for(ws.recv(), 5))
            return int(m.group(1)) if m else None
    except Exception:
        return None


async def listen(url: str, token: str, freq: float) -> None:
    """Play the staircase for the ears only (no microphone) and watch whether the speaker drops off the network."""
    signal, starts = staircase(freq)
    signal = np.concatenate([signal, np.zeros(RATE)])
    for _ in range(30):
        if await speaker_uptime(url, token) is not None:
            break
        await asyncio.sleep(1)
    events: list[tuple[float, str]] = []
    done = asyncio.Event()
    t0 = time.monotonic()

    async def watch() -> None:
        last = await speaker_uptime(url, token)
        while not done.is_set():
            await asyncio.sleep(0.4)
            now = await speaker_uptime(url, token)
            if now is None and last is not None:
                events.append((time.monotonic() - t0, "left the network"))
            elif now is not None and last is not None and now < last:
                events.append((time.monotonic() - t0, "came back after a reboot"))
            last = now if now is not None else None

    async with connect(url, max_size=None, ping_interval=None) as ws:
        await ws.send(token)
        await ws.send(f"stream {RATE} music")
        if await asyncio.wait_for(ws.recv(), 6) != "ok":
            raise SystemExit("the speaker is busy: stop tools/stream.py first")
        watcher = asyncio.create_task(watch())
        t0 = time.monotonic()
        print(f"  steps ({freq:.0f} Hz): " + ", ".join(f"{db:+d}" for db in LEVELS_DB) + " dBFS, 1.3 s each", flush=True)
        for i in range(0, len(signal), 4800):
            await ws.send(cal_io.pcm(signal[i:i + 4800]))
        try:
            await ws.send("stream_end")
            await asyncio.wait_for(ws.recv(), 12)
        except Exception:
            pass
        await asyncio.sleep(2.5)
        done.set()
        await watcher
    first_step = (CHIRP_S + LEAD_S)
    if not events:
        print("  the speaker stayed on the network the whole time (no reboot)")
    for t, what in events:
        step = (t - 1.5 - first_step) / (BURST_S + GAP_S)          # ~1.5 s from sending to sound
        idx = int(np.clip(np.floor(step), 0, len(LEVELS_DB) - 1))
        print(f"  {t:5.1f} s after the start the speaker {what}: about step {idx + 1} ({LEVELS_DB[idx]:+d} dBFS)")


async def run_frequency(url: str, token: str, mics: list, freq: float) -> list[list[dict]]:
    """For every microphone: one analysis per burst."""
    signal, starts = staircase(freq)
    recs = await cal_io.play_and_record(url, token, mics, signal, None, extra=3.5)
    out = []
    for rec in recs:
        if np.abs(rec).max() > 0.98:
            print("  this microphone clips: lower its gain in Windows")
        origin = find_start(rec[: int(3.0 * RATE)])
        print(f"  recording {len(rec) / RATE:.1f} s, peak {np.abs(rec).max():.3f}, chirp found at {origin / RATE:.2f} s")
        cal_io.save_wav(rec, f"limit-{int(freq)}Hz-mic{len(out) + 1}.wav")
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
    if args.listen:
        env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
        url = args.url or f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin"
        await listen(url, args.token or env.get("JARVIS_DEVICE_TOKEN", ""), args.freqs[0])
        return
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
    if args.listen:
        env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
        url = args.url or f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin"
        await listen(url, args.token or env.get("JARVIS_DEVICE_TOKEN", ""), args.freqs[0])
        return
    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    url = args.url or f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin"
    token = args.token or env.get("JARVIS_DEVICE_TOKEN", "")
    limits: dict[float, float | None] = {}
    for freq in args.freqs:
        for _ in range(30):                                   # wait for the speaker (it may be rebooting)
            if await speaker_uptime(url, token) is not None:
                break
            await asyncio.sleep(1)
        print(f"\nplaying {freq} Hz staircase, ~13 s ...", flush=True)
        started = await speaker_uptime(url, token)
        per_mic = await run_frequency(url, token, mics, freq)
        await asyncio.sleep(2.0)
        after = await speaker_uptime(url, token)
        reset = started is not None and (after is None or after < started)
        limits[freq] = report(freq, per_mic, names)
        if reset:
            print("  !!! THE SPEAKER REBOOTED during this staircase (the supply sagged): the louder steps never played")
            first_dead = next((i for i, r in enumerate(per_mic[0]) if r["snr_db"] < 8), None)
            if first_dead:
                crash = float(LEVELS_DB[first_dead - 1])
                print(f"  the last step that played: {crash:+.0f} dBFS; that is already the rebooting level, not a safe one")
                limits[freq] = min(crash - 3, limits[freq]) if limits[freq] is not None else crash - 3
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
    p.add_argument("--mic2", help="a second microphone. Not the laptop's built-in one: its noise suppression removes steady tones")
    p.add_argument("--freqs", type=float, nargs="+", default=FREQS)
    p.add_argument("--check", action="store_true", help="only record the room, play nothing")
    p.add_argument("--listen", action="store_true", help="just play the first staircase for the ears (no microphones) and report reboots")
    p.add_argument("--url")
    p.add_argument("--token")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
