"""Debug screenshot of the speaker's panel: `shot` in the console / tools/send.py asks the speaker for the
frame it is showing right now, reassembles it from {"type": "shot"} pieces and answers with a PNG."""

from __future__ import annotations

import asyncio
import base64
import struct
import zlib

from .devices import Device, DeviceHub

W = H = 240
TIMEOUT_S = 15


def to_png(rgb565_be: bytes) -> bytes:
    """RGB565 (high byte first) → 8-bit RGB PNG, no extra libraries."""
    rows = bytearray()
    for y in range(H):
        rows.append(0)
        line = rgb565_be[y * W * 2:(y + 1) * W * 2]
        for i in range(0, len(line), 2):
            v = (line[i] << 8) | line[i + 1]
            r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
            rows += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


class Screenshot:
    def __init__(self, hub: DeviceHub) -> None:
        self._hub = hub
        self._buf = bytearray(W * H * 2)
        self._got = 0
        self._done: asyncio.Event | None = None
        hub.on_message(self._on_message)

    async def _on_message(self, dev: Device, msg: dict) -> None:
        if msg["type"] != "shot" or self._done is None:
            return
        y, n = int(msg.get("y", 0)), int(msg.get("n", 0))
        data = base64.b64decode(msg.get("d", ""))
        if not (0 <= y and y + n <= H and len(data) == n * W * 2):
            return
        self._buf[y * W * 2:(y + n) * W * 2] = data
        self._got += n
        if self._got >= H:
            self._done.set()

    async def command(self, line: str) -> str | tuple[str, bytes] | None:
        if line.strip() != "shot":
            return None
        dev = self._hub.get()
        if dev is None:
            return "колонок нет — снимать нечего"
        self._got = 0
        self._done = asyncio.Event()
        try:
            await dev.send({"type": "shot"})
            await asyncio.wait_for(self._done.wait(), TIMEOUT_S)
        except TimeoutError:
            return f"колонка не прислала кадр за {TIMEOUT_S} с (получено строк: {self._got} из {H}); прошивка старая?"
        finally:
            self._done = None
        return f"снимок экрана {dev.id} (запись screen.png)", to_png(bytes(self._buf))
