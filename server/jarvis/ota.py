"""Обновление прошивки колонки по воздуху. Сервер отдаёт образ по HTTP на том же порту
(/firmware.bin?token=…), а колонке шлёт {"type": "ota", "url": …}; она качает, проверяет,
перезагружается и подключается уже с новой версией. Протокол — docs/protocol.md.

Где образ: JARVIS_FIRMWARE, иначе firmware/jarvis.bin рядом с сервером (так его кладёт
в аддон tools/deploy_addon.py), иначе ../firmware/build/jarvis.bin (разработка).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from . import protocol
from .config import Settings
from .devices import Device, DeviceHub
from .protocol import Message

log = logging.getLogger(__name__)

PATH = "/firmware.bin"
APP_DESC = 32            # esp_app_desc_t сразу за заголовками образа и первого сегмента
APP_MAGIC = 0xABCD5432
DOWNLOAD_TIMEOUT_S = 180
REBOOT_TIMEOUT_S = 90


@dataclass(frozen=True)
class Firmware:
    path: Path
    version: str
    size: int
    sha256: str

    @classmethod
    def load(cls, path: Path) -> Firmware:
        data = path.read_bytes()
        if len(data) < APP_DESC + 48 or int.from_bytes(data[APP_DESC:APP_DESC + 4], "little") != APP_MAGIC:
            raise ValueError(f"{path} — не образ прошивки ESP32")
        version = data[APP_DESC + 16:APP_DESC + 48].split(b"\0")[0].decode(errors="replace")
        return cls(path, version, len(data), hashlib.sha256(data).hexdigest())


def find_firmware(configured: str = "") -> Path | None:
    here = Path(__file__).resolve().parents[1]
    for p in ([Path(configured)] if configured else []) + [here / "firmware" / "jarvis.bin",
                                                          here.parent / "firmware" / "build" / "jarvis.bin"]:
        if p.is_file():
            return p
    return None


class Ota:
    def __init__(self, hub: DeviceHub, settings: Settings, firmware_path: str = ""):
        self.hub = hub
        self._settings = settings
        self._path = firmware_path
        self._states: dict[str, asyncio.Queue[Message]] = {}
        hub.ota = self
        hub.add_http(PATH, self._serve)
        hub.on_message(self._on_message)

    def firmware(self) -> Firmware | None:
        p = find_firmware(self._path)
        return Firmware.load(p) if p else None

    # ── HTTP: колонка качает образ ──

    def _serve(self, request: Request) -> Response:
        token = parse_qs(urlsplit(request.path).query).get("token", [""])[0]
        if not hmac.compare_digest(token, self._settings.device_token):
            return Response(403, "Forbidden", Headers([("Content-Length", "0")]), b"")
        fw = self.firmware()
        if fw is None:
            return Response(404, "Not Found", Headers([("Content-Length", "0")]), b"")
        body = fw.path.read_bytes()
        log.info("отдаю прошивку %s (%d байт)", fw.version, len(body))
        return Response(200, "OK", Headers([("Content-Type", "application/octet-stream"),
                                            ("Content-Length", str(len(body)))]), body)

    async def _on_message(self, dev: Device, msg: Message) -> None:
        if msg["type"] == "ota_state" and dev.id in self._states:
            self._states[dev.id].put_nowait(msg)

    # ── обновление ──

    async def update(self, dev: Device, fw: Firmware) -> str:
        if dev.id in self._states:
            return f"{dev.id} уже обновляется"
        states = self._states[dev.id] = asyncio.Queue()
        old = dev.hello.fw
        try:
            await dev.send(protocol.ota(f"{PATH}?token={quote(self._settings.device_token)}", fw.version, fw.size))
            log.info("%s: обновляю %s → %s", dev.id, old, fw.version)
            deadline = time.monotonic() + DOWNLOAD_TIMEOUT_S
            while True:
                try:
                    st = await asyncio.wait_for(states.get(), max(0.1, deadline - time.monotonic()))
                except TimeoutError:
                    return f"{dev.id} не докачала прошивку за {DOWNLOAD_TIMEOUT_S} с"
                if st.get("state") == "error":
                    return f"{dev.id}: обновиться не вышло — {st.get('error', '?')}"
                if st.get("state") == "done":
                    break
            # перезагрузка: ждём, пока колонка вернётся уже с новой версией
            deadline = time.monotonic() + REBOOT_TIMEOUT_S
            while time.monotonic() < deadline:
                d = self.hub.get(dev.id)
                if d is not None and d is not dev:
                    if d.hello.fw == fw.version:
                        return f"{dev.id}: обновлена {old} → {fw.version}"
                    return f"{dev.id} вернулась с {d.hello.fw} — новая прошивка не встала (откат?)"
                await asyncio.sleep(0.5)
            return f"{dev.id} докачала {fw.version}, но не вернулась за {REBOOT_TIMEOUT_S} с"
        finally:
            del self._states[dev.id]

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() != "ota":
            return None
        try:
            fw = self.firmware()
        except (OSError, ValueError) as e:
            return f"ota: {e}"
        dev = self.hub.get()
        info = f"на сервере {fw.version} ({fw.size // 1024} КБ)" if fw else "образа прошивки на сервере нет"
        if len(parts) > 1 and parts[1] == "info" or fw is None or dev is None:
            have = f"{dev.id} на {dev.hello.fw}" if dev else "колонок нет"
            return f"{info}; {have}"
        if dev.hello.fw == fw.version and (len(parts) < 2 or parts[1] != "force"):
            return f"{dev.id} уже на {fw.version} (ota force — залить всё равно)"
        return await self.update(dev, fw)
