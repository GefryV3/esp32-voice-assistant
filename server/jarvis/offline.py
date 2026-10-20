"""Колонка без сервера: хлопки на устройстве и план сигналов на случай обрыва связи.

- Хлопки (docs/protocol-next.md, `clap`): детектор живёт в прошивке и работает без сети; здесь —
  настройки (config: clap, clap_sens), приём сообщения `clap` и консольная команда `clap`.
  Что делать с хлопком (переключить свет), решает не этот модуль: подпишись через `on_clap`.
- План (`plan`): ближайшие напоминания и таймеры с Unix-временем. Колонка хранит их в NVS и при
  потере связи сама играет тон и показывает сигнал. Шлём при подключении и при изменениях.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import protocol
from .devices import Device, DeviceHub

log = logging.getLogger(__name__)

PREF_ON = "clap_on"
PREF_SENS = "clap_sens"
DEFAULT_SENS = 5

ClapHandler = Callable[[Device, int], Awaitable[None] | None]


class Offline:
    def __init__(self, hub: DeviceHub, brain=None, clap_on: bool = True, clap_sens: int = DEFAULT_SENS):
        self.hub = hub
        self.brain = brain
        self.clap_on = clap_on
        self.clap_sens = clap_sens
        self.on_clap: list[ClapHandler] = []
        self.claps = 0           # сколько серий пришло с запуска (для команды clap)
        self._tasks: set[asyncio.Task] = set()
        if brain is not None:
            self.clap_on = brain.pref(PREF_ON, "1" if clap_on else "0") == "1"
            try:
                self.clap_sens = max(1, min(10, int(brain.pref(PREF_SENS, str(clap_sens)))))
            except ValueError:
                pass
            brain.plan_changed = self.plan_changed
        hub.on_connect(self._on_connect)
        hub.on_message(self._on_message)
        hub.offline = self

    # ── колонка подключилась / сообщила ──

    async def _on_connect(self, dev: Device) -> None:
        if "mic" in dev.hello.caps:
            await dev.send(self.config_message())
        if "speaker" in dev.hello.caps or "display" in dev.hello.caps:
            await dev.send(self.plan_message())

    async def _on_message(self, dev: Device, msg: dict) -> None:
        if msg.get("type") != "clap":
            return
        count = msg.get("count")
        count = count if isinstance(count, int) and not isinstance(count, bool) else 2
        self.claps += 1
        log.info("%s: хлопки ×%d", dev.id, count)
        for handler in list(self.on_clap):
            try:
                res = handler(dev, count)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                log.exception("обработчик хлопков упал")

    # ── сообщения колонке ──

    def config_message(self) -> protocol.Message:
        return protocol.config(clap=self.clap_on, clap_sens=self.clap_sens)

    def plan_message(self) -> protocol.Message:
        alarms, timers = self.brain.plan_rows() if self.brain is not None else ([], [])
        return protocol.plan(alarms, timers)

    def plan_changed(self) -> None:
        """Brain поменял напоминания или таймеры — обновить план у колонок (из любого потока)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.hub.broadcast(self.plan_message()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── консоль ──

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() not in ("clap", "plan"):
            return None
        if parts[0].lower() == "plan":
            msg = self.plan_message()
            await self.hub.broadcast(msg)
            return f"план отправлен: {protocol.encode(msg)}"
        args = parts[1:]
        if not args or args[0] not in ("on", "off"):
            state = f"{'вкл' if self.clap_on else 'выкл'}, чувствительность {self.clap_sens}"
            return f"clap on|off [чувствительность 1…10] — хлопки на колонке (сейчас {state}, серий принято {self.claps})"
        on = args[0] == "on"
        if len(args) > 1:
            try:
                sens = int(args[1])
            except ValueError:
                return f"чувствительность — число 1…10, получено «{args[1]}»"
            if not 1 <= sens <= 10:
                return "чувствительность — число 1…10"
            self.clap_sens = sens
        self.clap_on = on
        if self.brain is not None:
            self.brain.set_pref(PREF_ON, "1" if on else "0")
            self.brain.set_pref(PREF_SENS, str(self.clap_sens))
        sent = await self.hub.broadcast(self.config_message())
        return f"хлопки {'вкл' if on else 'выкл'}, чувствительность {self.clap_sens}" + ("" if sent else " (колонок нет — уйдёт при подключении)")
