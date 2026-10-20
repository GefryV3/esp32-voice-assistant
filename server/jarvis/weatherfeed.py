"""Значок погоды и температура на экране колонки, без вопроса Gemini.

Раньше значок появлялся, только когда кто-то спросил про погоду (`home_weather`), а после перезагрузки
колонки пропадал. Теперь сервер сам шлёт его при подключении колонки и потом раз в REFRESH_S.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .devices import Device, DeviceHub

log = logging.getLogger(__name__)

REFRESH_S = 900.0     # 15 минут: погода меняется медленно, а запрос в HA недешёвый (прогноз)


class WeatherFeed:
    def __init__(self, hub: DeviceHub, ha: Any, *, refresh_s: float = REFRESH_S):
        self.hub, self.ha, self.refresh_s = hub, ha, refresh_s
        self._task: asyncio.Task | None = None
        hub.on_connect(self._on_connect)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def _on_connect(self, dev: Device) -> None:
        # не держим подключение: HA может отвечать секунды, значок придёт, когда придёт
        asyncio.create_task(self.push(dev))

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_s)
            await self.push()

    async def push(self, dev: Device | None = None) -> bool:
        """Отправить погоду на экран. False — колонки с экраном нет или HA не дал погоду."""
        dev = dev or self.hub.get()
        if dev is None or "display" not in dev.hello.caps:
            return False
        try:
            out = await self.ha.weather(dev.send)
        except Exception as e:      # HA недоступен, колонка отключилась: в следующий раз получится
            log.debug("погода на экран не ушла: %r", e)
            return False
        return bool(out.get("ok"))
