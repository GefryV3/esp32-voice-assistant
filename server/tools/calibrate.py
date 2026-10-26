"""Measure how the speaker really sounds and build a correction towards the target curve (JBL-like).

    python tools/calibrate.py --check            # only listen to the room: is the microphone alive, is it quiet
    python tools/calibrate.py                    # two passes: measure, correct, measure again, save

Put a USB microphone (default: name contains "fifine") about a metre from the speaker, pointing at it. The speaker
plays a loud-ish sweep for ~10 s per pass: the room must be quiet. Stop tools/stream.py first (it holds the speaker).
The result is saved to ~/.jarvis_stream_correction.json; restart tools/stream.py to use it (the "Подгонка" slider
on the equalizer page sets how much of it is applied). The microphone itself is not calibrated: expect ±3 dB.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import warnings
from datetime import datetime
from pathlib import Path

import numpy as np
import soundcard as sc
from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibrate_dsp as cal  # noqa: E402
import stream_dsp  # noqa: E402
from jarvis.config import read_env_file  # noqa: E402

RATE = cal.RATE
BLOCK = 4800
RECORDINGS = Path(__file__).resolve().parents[1] / "recordings"


def find_mic(name: str):
    mics = sc.all_microphones(include_loopback=False)
    for m in mics:
        if name.lower() in m.name.lower():
            return m
    seen = ", ".join(m.name for m in mics) or "none"
    raise SystemExit(f"no microphone with «{name}» in its name; the system sees: {seen}")


def record(mic, seconds: float) -> np.ndarray:
    with mic.recorder(samplerate=RATE, channels=1) as rec:
        return rec.record(numframes=int(seconds * RATE))[:, 0]


def dbfs(x: np.ndarray) -> float:
    return 20 * float(np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9))


def pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


async def play_and_record(url: str, token: str, mic, signal: np.ndarray, chain: stream_dsp.Chain | None) -> np.ndarray:
    loop = asyncio.get_running_loop()
    signal = np.concatenate([signal, np.zeros(RATE)])                     # a second of tail for the pipeline
    seconds = len(signal) / RATE + 7                                      # + ~1.5 s of delay before the sound starts
    async with connect(url, max_size=None) as ws:
        await ws.send(token)
        await ws.send(f"stream {RATE} music")
        try:
            reply = await asyncio.wait_for(ws.recv(), 6)
        except TimeoutError:
            raise SystemExit("the speaker is busy: stop tools/stream.py first") from None
        if reply != "ok":
            raise SystemExit(reply)
        recording = loop.run_in_executor(None, record, mic, seconds)
        await asyncio.sleep(0.5)
        for i in range(0, len(signal), BLOCK):
            block = signal[i:i + BLOCK]
            await ws.send(pcm(chain.process(block) if chain else block))
        await ws.send("stream_end")
        await ws.recv()
        return await recording


def measure(rec: np.ndarray, ref: np.ndarray, gate_ms: float):
    peak = float(np.abs(rec).max())
    print(f"  microphone: peak {20 * np.log10(peak + 1e-9):.1f} dBFS, average {dbfs(rec):.1f} dBFS")
    if peak > 0.98:
        raise SystemExit("the microphone clips: lower its gain in Windows or use --level -26")
    if peak < 0.003:
        raise SystemExit("the microphone hardly hears anything: raise its gain in Windows or check where it points")
    return cal.smoothed_response_db(cal.impulse_response(rec, ref), gate_ms=gate_ms)


def make_chain(correction: tuple[list[float], list[float]] | None) -> stream_dsp.Chain:
    """The real chain with everything off except the correction, on a throwaway settings file."""
    stream_dsp.SAVED = Path(tempfile.gettempdir()) / "jarvis_calibrate_eq.json"
    eq = stream_dsp.Equalizer()
    eq.correction = correction
    eq.fx = {"vbass": 0.0, "comp": 0.0, "cut": 20.0, "corr": 100.0}
    eq.gains, eq.volume, eq.version = [0.0] * len(stream_dsp.BANDS), 0.0, 1
    return stream_dsp.Chain(eq)


def show(title: str, freqs, db, target) -> None:
    print(f"  {title}: " + "  ".join(f"{b}Hz {m:+.1f}" for b, m, _ in cal.band_table(freqs, db, target)))


async def main_async(args) -> None:
    mic = find_mic(args.mic)
    print(f"microphone: {mic.name}")
    ambient = await asyncio.get_running_loop().run_in_executor(None, record, mic, 2.0)
    print(f"room noise {dbfs(ambient):.1f} dBFS, peak {20 * np.log10(np.abs(ambient).max() + 1e-9):.1f} dBFS")
    if args.check:
        return
    if dbfs(ambient) > -45:
        raise SystemExit("the room is too noisy for a measurement (hairdryer, fan, music?): quieter, please")

    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    url = args.url or f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin"
    token = args.token or env.get("JARVIS_DEVICE_TOKEN", "")
    ref = cal.sweep(level_db=args.level)
    freqs = cal.log_grid()
    target = cal.target_db(freqs)

    corr = None
    first = None
    for n in range(1, args.passes + 1):
        chain = make_chain((freqs.tolist(), corr.tolist())) if corr is not None else None
        print(f"pass {n}: " + ("with the correction so far" if chain else "the speaker as it is") + ", ~20 s of sweep")
        rec = await play_and_record(url, token, mic, ref, chain)
        f, db = measure(rec, ref, args.gate)
        if first is None:
            first = db
        show(f"measured (re 1 kHz)", f, db - np.interp(np.log(1000), np.log(f), db), target)
        corr = cal.design_correction(f, db, previous=corr, damping=0.8)
        if args.passes == n:
            break

    show("target (re 1 kHz)", freqs, target, target)
    print("correction to apply: " + "  ".join(f"{b}Hz {float(np.interp(np.log(b), np.log(freqs), corr)):+.1f}"
                                              for b in (63, 125, 250, 500, 1000, 2000, 4000, 8000, 12000)))
    stream_dsp.CORRECTION = Path.home() / ".jarvis_stream_correction.json"
    eq = stream_dsp.Equalizer.__new__(stream_dsp.Equalizer)
    eq.lock = __import__("threading").Lock()
    eq.version = 0
    eq.correction = None
    eq.set_correction(freqs.tolist(), corr.tolist())
    RECORDINGS.mkdir(exist_ok=True)
    path = RECORDINGS / f"calibration-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps({"freqs": freqs.tolist(), "first_pass_db": first.tolist(), "last_pass_db": db.tolist(),
                                "correction_db": corr.tolist(), "target_db": target.tolist()}), encoding="utf-8")
    print(f"saved: {stream_dsp.CORRECTION} (restart tools/stream.py), details: {path}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mic", default="fifine", help="part of the microphone name")
    p.add_argument("--level", type=float, default=-20.0, help="sweep level, dBFS")
    p.add_argument("--passes", type=int, default=2, help="measure, then re-measure with the correction applied")
    p.add_argument("--gate", type=float, default=30.0, help="ms of the impulse response kept (cuts room reflections)")
    p.add_argument("--check", action="store_true", help="only record 2 s of the room and report the level")
    p.add_argument("--url")
    p.add_argument("--token")
    asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    main()
