"""Play whatever the computer is playing on the speaker (WASAPI loopback, Windows), with a live equalizer.

    python tools/stream.py            # until Ctrl+C; equalizer page at http://localhost:8800
    python tools/stream.py --list     # show output devices

Needs `pip install soundcard scipy`. Start music on the computer (or send the phone's sound to it,
e.g. over Bluetooth) and it comes out of the speaker with ~1.5 s delay. The server must be
a version with `stream` support on /admin. The equalizer page (tools/stream_eq.html) can be opened
from the phone too, on the computer's LAN address; its settings are remembered between runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundcard as sc
from scipy.signal import sosfilt, sosfreqz
from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jarvis.config import read_env_file  # noqa: E402

CAPTURE_RATE = 48_000
SEND_RATE = 48_000          # the capture rate: no resampling, so nothing is lost on the way to the speaker
BLOCK = 4800                # 100 ms
PAGE = Path(__file__).with_name("stream_eq.html")
SAVED = Path.home() / ".jarvis_stream_eq.json"

# (centre Hz, kind): the lowest and highest bands are shelves, the rest are peaks of about an octave
BANDS = [(63, "ls"), (125, "pk"), (250, "pk"), (500, "pk"), (1000, "pk"), (2000, "pk"), (4000, "pk"), (8000, "hs")]
MAX_DB = 15.0


def biquad(kind: str, gain_db: float, freq: float, rate: int = SEND_RATE, q: float = 1.1) -> list[float]:
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


FW_HIGHPASS_HZ = 300.0      # the firmware cuts everything below this on any sound it plays (audio.cpp)


def highpass(fc: float, fs: float) -> tuple[np.ndarray, np.ndarray]:
    """2nd-order Butterworth high-pass exactly as the firmware's Highpass class builds it."""
    w0 = 2 * np.pi * fc / fs
    c, alpha = np.cos(w0), np.sin(w0) / np.sqrt(2.0)
    a0 = 1 + alpha
    b = np.array([(1 + c) / 2, -(1 + c), (1 + c) / 2]) / a0
    return b, np.array([1.0, -2 * c / a0, (1 - alpha) / a0])


def firmware_bypass(floor_hz: float) -> list[float]:
    """SOS row that undoes the firmware's 300 Hz high-pass down to `floor_hz`: HP(floor) / HP(300).
    Both have a double zero at DC, which cancels, so the ratio is a plain stable biquad."""
    b_floor, a_floor = highpass(floor_hz, SEND_RATE)
    b_fw, a_fw = highpass(FW_HIGHPASS_HZ, SEND_RATE)
    gain = b_floor[0] / b_fw[0]
    return [gain, gain * a_fw[1], gain * a_fw[2], 1.0, a_floor[1], a_floor[2]]


class Equalizer:
    """Band gains and volume, shared between the audio loop and the web page."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gains = [0.0] * len(BANDS)
        self.volume = 0.0
        self.version = 0
        self.bypass_hz = 0.0         # 0 = off
        try:
            saved = json.loads(SAVED.read_text(encoding="utf-8"))
            self.set(saved["gains"], saved["volume"])
        except (OSError, ValueError, KeyError):
            pass

    def set(self, gains: list, volume: float) -> None:
        gains = [max(-MAX_DB, min(MAX_DB, float(g))) for g in gains][: len(BANDS)]
        with self.lock:
            self.gains = gains + [0.0] * (len(BANDS) - len(gains))
            self.volume = max(-24.0, min(12.0, float(volume)))
            self.version += 1
            data = {"gains": self.gains, "volume": self.volume}
        try:
            SAVED.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

    def filters(self) -> tuple[np.ndarray, float, int]:
        """SOS cascade, linear gain, version. Boost eats headroom: half of the biggest boost is taken back."""
        with self.lock:
            gains, volume, version = list(self.gains), self.volume, self.version
        rows = [biquad(kind, g, f) for (f, kind), g in zip(BANDS, gains)]
        if self.bypass_hz:
            rows.insert(0, firmware_bypass(self.bypass_hz))
        sos = np.array(rows)
        pre = volume - 0.5 * max(0.0, max(gains)) - (self._bypass_headroom() if self.bypass_hz else 0.0)
        return sos, 10 ** (pre / 20), version

    def _bypass_headroom(self) -> float:
        """Bass is lifted up to (300/floor)^2 before the firmware takes it back: keep room for it."""
        return min(9.0, 20 * np.log10(FW_HIGHPASS_HZ / self.bypass_hz) * 0.5)

    def state(self) -> dict:
        sos, _, _ = self.filters()
        freqs = np.logspace(np.log10(30), np.log10(11_000), 120)
        _, h = sosfreqz(sos, worN=2 * np.pi * freqs / SEND_RATE)
        return {"bands": [f for f, _ in BANDS], "gains": self.gains, "volume": self.volume, "max": MAX_DB,
                "freqs": freqs.round(1).tolist(), "curve": (20 * np.log10(np.abs(h) + 1e-9)).round(2).tolist()}


def serve(eq: Equalizer, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: bytes, kind: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/state":
                self._send(json.dumps(eq.state()).encode(), "application/json")
            else:
                self._send(PAGE.read_bytes(), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                eq.set(data["gains"], data["volume"])
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            self._send(json.dumps(eq.state()).encode(), "application/json")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def soft_limit(x: np.ndarray, knee: float = 0.85) -> np.ndarray:
    """Untouched below the knee; above it peaks bend smoothly towards 1.0 instead of clipping."""
    a = np.abs(x)
    r = 1.0 - knee
    return np.where(a <= knee, x, np.sign(x) * (knee + r * np.tanh((a - knee) / r)))


def lan_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.178.1", 9))
            return s.getsockname()[0]
    except OSError:
        return "localhost"


def pick(name: str | None):
    speaker = next((s for s in sc.all_speakers() if name.lower() in s.name.lower()), None) if name else sc.default_speaker()
    if speaker is None:
        raise SystemExit(f"no output device matching «{name}», see --list")
    return sc.get_microphone(speaker.name, include_loopback=True)


async def run(url: str, token: str, device: str | None, eq: Equalizer) -> None:
    mic = pick(device)
    print(f"capturing «{mic.name}» → speaker, Ctrl+C to stop")
    async with connect(url, max_size=None) as ws:
        await ws.send(token)
        await ws.send(f"stream {SEND_RATE} music")
        reply = await ws.recv()
        if reply != "ok":
            raise SystemExit(reply)
        print("server accepted the stream", flush=True)
        loop = asyncio.get_running_loop()
        sent = 0
        sos, gain, version = eq.filters()
        zi = np.zeros((len(sos), 2))
        with mic.recorder(samplerate=CAPTURE_RATE, channels=2, blocksize=BLOCK) as rec:
            while True:
                block = await loop.run_in_executor(None, rec.record, BLOCK)
                mono = block.mean(axis=1)
                if eq.version != version:
                    sos, gain, version = eq.filters()
                out, zi = sosfilt(sos, mono, zi=zi)
                out = soft_limit(out * gain)
                pcm = (np.clip(out, -1, 1) * 32767).astype("<i2").tobytes()
                await ws.send(pcm)
                sent += 1
                if sent % 50 == 1:
                    print(f"sent {sent} blocks, level {float(np.abs(mono).max()):.2f}", flush=True)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)   # loopback data discontinuity
    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin")
    p.add_argument("--token", default=env.get("JARVIS_DEVICE_TOKEN", ""))
    p.add_argument("--device", help="part of the output device name (default: system default)")
    p.add_argument("--port", type=int, default=8800, help="equalizer page port")
    p.add_argument("--bypass", type=float, default=0.0, metavar="HZ",
                   help="for firmware before 0.9.2 (no music mode): undo its 300 Hz bass cut down to this frequency")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()
    if args.list:
        for s in sc.all_speakers():
            print(s.name)
        return
    eq = Equalizer()
    eq.bypass_hz = args.bypass
    serve(eq, args.port)
    print(f"equalizer: http://localhost:{args.port}  (from the phone: http://{lan_address()}:{args.port})", flush=True)
    try:
        asyncio.run(run(args.url, args.token, args.device, eq))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
