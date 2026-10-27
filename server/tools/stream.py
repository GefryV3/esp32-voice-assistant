"""Play whatever the computer is playing on the speaker (WASAPI loopback, Windows), with a live equalizer.

    python tools/stream.py            # until Ctrl+C; equalizer page at http://localhost:8800
    python tools/stream.py --list     # show output devices

Needs `pip install soundcard scipy`. Start music on the computer (or send the phone's sound to it,
e.g. over Bluetooth) and it comes out of the speaker with ~1.5 s delay. The server must be
a version with `stream` support on /admin and the speaker firmware 0.9.2+ (music mode).

The equalizer page (tools/stream_eq.html) can be opened from the phone too, on the computer's LAN
address; its settings are remembered between runs. The sound processing is in tools/stream_dsp.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import soundcard as sc
from websockets.asyncio.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from jarvis.config import read_env_file  # noqa: E402
from stream_dsp import RATE, Chain, Equalizer  # noqa: E402

rng = np.random.default_rng()


def to_pcm(x: np.ndarray) -> bytes:
    """16-bit PCM with TPDF dither: quantisation noise stays noise instead of turning into distortion on quiet passages."""
    dither = (rng.random(len(x)) - rng.random(len(x))) / 32768.0
    return (np.clip(np.round((x + dither) * 32767), -32768, 32767)).astype("<i2").tobytes()

BLOCK = 4800                # 100 ms; a multiple of the compressor slice
discontinuities = 0         # glitches the capture API reported (dropped or repeated audio before it ever got here)
PAGE = Path(__file__).with_name("stream_eq.html")


def restart_self(server: ThreadingHTTPServer) -> None:
    """Start this program over with the same arguments (the equalizer page's restart button)."""
    server.shutdown()
    server.server_close()                                   # free the port for the new process
    os.execv(sys.executable, [sys.executable, "-u", *sys.argv])


def serve(eq: Equalizer, port: int) -> None:
    holder: list[ThreadingHTTPServer] = []

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
            if self.path == "/preset":
                try:
                    data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                    (eq.save_preset if data["action"] == "save" else eq.load_preset)(data["name"])
                except (ValueError, KeyError, TypeError, OSError):
                    self.send_error(404)
                    return
                self._send(json.dumps(eq.state()).encode(), "application/json")
                return
            if self.path == "/restart":
                self._send(b'{"ok": true}', "application/json")
                threading.Timer(0.4, restart_self, args=(holder[0],)).start()
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                eq.set(data["gains"], data["volume"], data.get("fx"))
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            self._send(json.dumps(eq.state()).encode(), "application/json")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    holder.append(server)
    threading.Thread(target=server.serve_forever, daemon=True).start()


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
        await ws.send(f"stream {RATE} music")
        reply = await ws.recv()
        if reply != "ok":
            raise SystemExit(reply)
        print("server accepted the stream", flush=True)
        loop = asyncio.get_running_loop()
        chain = Chain(eq)
        sent = 0
        peak_out, worst_limiter, worst_comp = 0.0, 1.0, 0.0
        late, last = 0, time.monotonic()
        with mic.recorder(samplerate=RATE, channels=2, blocksize=BLOCK) as rec:
            while True:
                block = await loop.run_in_executor(None, rec.record, BLOCK)
                now = time.monotonic()
                late += now - last > 0.25          # a block that took more than 250 ms: the speaker would run dry
                last = now
                mono = block.mean(axis=1)
                out = chain.process(mono)
                await ws.send(to_pcm(out))
                sent += 1
                peak_out = max(peak_out, float(np.abs(out).max()))
                worst_limiter = min(worst_limiter, chain._limiter._gain)
                worst_comp = min(worst_comp, chain._comp.reduction_db)
                if sent % 50 == 1:
                    print(f"sent {sent} blocks, in {float(np.abs(mono).max()):.2f}, out peak {peak_out:.2f}, "
                          f"limiter down to {20 * np.log10(worst_limiter):.1f} dB, compressor down to {worst_comp:.1f} dB, "
                          f"capture glitches {discontinuities}, late blocks {late}", flush=True)
                    peak_out, worst_limiter, worst_comp = 0.0, 1.0, 0.0


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    def count_glitch(message, category, *args, **kwargs):        # loopback "data discontinuity": count, don't print
        global discontinuities
        discontinuities += 1
    warnings.showwarning = count_glitch
    warnings.simplefilter("always", sc.SoundcardRuntimeWarning)
    env = read_env_file(Path(__file__).resolve().parents[1] / ".env")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin")
    p.add_argument("--token", default=env.get("JARVIS_DEVICE_TOKEN", ""))
    p.add_argument("--device", help="part of the output device name (default: system default)")
    p.add_argument("--port", type=int, default=8800, help="equalizer page port")
    p.add_argument("--list", action="store_true")
    args = p.parse_args()
    if args.list:
        for s in sc.all_speakers():
            print(s.name)
        return
    eq = Equalizer()
    serve(eq, args.port)
    print(f"equalizer: http://localhost:{args.port}  (from the phone: http://{lan_address()}:{args.port})", flush=True)
    try:
        asyncio.run(run(args.url, args.token, args.device, eq))
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":
    main()
