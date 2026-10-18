"""WebSocket-хаб для колонок: рукопожатие, реестр подключённых устройств, отправка команд."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import __version__, protocol
from .config import Settings
from .protocol import Hello, Message, ProtocolError

log = logging.getLogger(__name__)

DEVICE_PATH = "/device"


@dataclass
class Device:
    hello: Hello
    ws: ServerConnection
    connected_at: float = field(default_factory=time.time)
    status: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.hello.device

    async def send(self, msg: Message) -> None:
        await self.ws.send(protocol.encode(msg))


MessageHandler = Callable[[Device, Message], Awaitable[None]]
AudioHandler = Callable[[Device, bytes], None]
RouteHandler = Callable[[ServerConnection], Awaitable[None]]


class DeviceHub:
    def __init__(self, settings: Settings, *, hello_timeout: float = protocol.HELLO_TIMEOUT_S,
                 clock: Callable[[], float] = time.time):
        self._settings = settings
        self._hello_timeout = hello_timeout
        self._clock = clock
        self._server: Server | None = None
        self.devices: dict[str, Device] = {}
        self._handlers: list[MessageHandler] = []
        self._audio_handlers: list[AudioHandler] = []
        self._connect_handlers: list[Callable[[Device], Awaitable[None]]] = []
        self._routes: dict[str, RouteHandler] = {DEVICE_PATH: self._handle_device}
        self._background: set[asyncio.Task] = set()
        self.bridge: Any = None  # мост с телефоном и компьютером (jarvis.bridge), если запущен
        self.audio: Any = None   # звук колонок (jarvis.audio), если запущен

    # ── жизненный цикл ──

    async def start(self, host: str | None = None, port: int | None = None) -> None:
        self._server = await serve(
            self._handle,
            self._settings.host if host is None else host,
            self._settings.port if port is None else port,
        )
        log.info("жду колонки на ws://%s:%d%s", self._settings.host, self.port, DEVICE_PATH)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> DeviceHub:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "хаб не запущен"
        return self._server.sockets[0].getsockname()[1]

    # ── публичное API ──

    def on_message(self, handler: MessageHandler) -> None:
        """Подписка на сообщения от колонок (кроме hello/pong — их хаб обрабатывает сам)."""
        self._handlers.append(handler)

    def on_audio(self, handler: AudioHandler) -> None:
        """Подписка на бинарные кадры (звук микрофона). Обработчик не должен ждать."""
        self._audio_handlers.append(handler)

    def on_connect(self, handler: Callable[[Device], Awaitable[None]]) -> None:
        """Подписка на подключение колонки (вызывается сразу после welcome)."""
        self._connect_handlers.append(handler)

    def add_route(self, path: str, handler: RouteHandler) -> None:
        """Дополнительный WebSocket-вход на том же порту (например, служебный /admin)."""
        self._routes[path] = handler

    def get(self, device_id: str | None = None) -> Device | None:
        """Колонка по имени; без имени — единственная (или первая) подключённая."""
        if device_id is not None:
            return self.devices.get(device_id)
        return next(iter(self.devices.values()), None)

    async def broadcast(self, msg: Message) -> int:
        sent = 0
        for dev in list(self.devices.values()):
            try:
                await dev.send(msg)
                sent += 1
            except ConnectionClosed:
                pass
        return sent

    # ── соединение ──

    async def _handle(self, ws: ServerConnection) -> None:
        route = self._routes.get(ws.request.path) if ws.request else None
        if route is None:
            await ws.close(1008, "unknown path")
            return
        await route(ws)

    async def _handle_device(self, ws: ServerConnection) -> None:
        try:
            hello = await self._handshake(ws)
        except ProtocolError as e:
            log.warning("отказ %s: %s", ws.remote_address, e)
            await ws.close(e.close_code, str(e)[:120])
            return
        except ConnectionClosed:
            return  # оборвалась до hello — например, перезагрузилась во время прошивки

        dev = Device(hello=hello, ws=ws)
        old = self.devices.get(dev.id)
        self.devices[dev.id] = dev
        if old is not None:
            # Старое соединение обычно уже мёртвое (колонка перезагрузилась), и закрытие
            # ждёт таймаута — не держим из-за него новую колонку без welcome.
            log.info("%s переподключилась, старое соединение закрываю", dev.id)
            task = asyncio.create_task(old.ws.close(protocol.CLOSE_REPLACED, "replaced"))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        else:
            log.info("подключилась %s (прошивка %s, умеет: %s)", dev.id, hello.fw,
                     ", ".join(sorted(hello.caps)) or "—")

        try:
            await dev.send(protocol.welcome(self._clock(), self._settings.tz, __version__))
            for handler in self._connect_handlers:
                try:
                    await handler(dev)
                except Exception:
                    log.exception("обработчик подключения %s упал", dev.id)
            async for raw in ws:
                await self._dispatch(dev, raw)
        except ConnectionClosed:
            pass
        finally:
            if self.devices.get(dev.id) is dev:
                del self.devices[dev.id]
                log.info("отключилась %s", dev.id)

    async def _handshake(self, ws: ServerConnection) -> Hello:
        try:
            raw = await asyncio.wait_for(ws.recv(), self._hello_timeout)
        except TimeoutError:
            raise ProtocolError("не прислала hello вовремя") from None
        hello = protocol.parse_hello(raw)
        if not hmac.compare_digest(hello.token, self._settings.device_token):
            raise ProtocolError("неверный токен", protocol.CLOSE_BAD_TOKEN)
        return hello

    async def _dispatch(self, dev: Device, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            for audio_handler in self._audio_handlers:
                audio_handler(dev, raw)
            return
        try:
            msg = protocol.decode(raw)
        except ProtocolError as e:
            log.warning("%s: %s", dev.id, e)
            return
        if msg["type"] == "status":
            dev.status = {k: v for k, v in msg.items() if k != "type"}
            log.debug("%s status %s", dev.id, dev.status)
        elif msg["type"] == "pong":
            return
        for handler in self._handlers:
            try:
                await handler(dev, msg)
            except Exception:
                log.exception("обработчик сообщения %s упал", msg["type"])
