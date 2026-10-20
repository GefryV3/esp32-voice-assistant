"""«Возьми зонт»: когда пользователь уходит из дома и в ближайшие часы обещают дождь.

Прогноз берём у Open-Meteo (бесплатно, без ключа) по координатам из .env: он не зависит
от HA и от ветки с погодой. «Ушёл» приходит от рутин (HA person.* → not_home).
.env: UMBRELLA=on|off, WEATHER_LAT, WEATHER_LON (по умолчанию центр Вены),
UMBRELLA_PROB (порог вероятности дождя в процентах, 50), UMBRELLA_HOURS (на сколько часов вперёд, 8).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from .untis import Fetch, urllib_fetch

log = logging.getLogger(__name__)

URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
       "&hourly=precipitation_probability,precipitation&forecast_hours={hours}&timezone=auto")
REPEAT_S = 3 * 3600.0       # не чаще раза в 3 часа
MIN_MM = 0.3                # столько осадков за час считаем дождём независимо от вероятности

PROMPT = (
    "Пользователь только что вышел из дома. По прогнозу {when}. Скажи ему одной короткой фразой "
    "взять зонт, в твоей обычной манере. Больше ничего не добавляй.")


@dataclass(frozen=True)
class UmbrellaConfig:
    enabled: bool = True
    lat: float = 48.208       # центр Вены; свои координаты — WEATHER_LAT/WEATHER_LON в .env
    lon: float = 16.373
    prob: int = 50
    hours: int = 8

    @classmethod
    def from_env(cls, env: dict[str, str]) -> UmbrellaConfig:
        def num(key: str, default, cast):
            try:
                return cast(env.get(key, "").strip() or default)
            except ValueError:
                return default
        return cls(enabled=env.get("UMBRELLA", "on").strip().lower() not in ("0", "false", "no", "off"),
                   lat=num("WEATHER_LAT", 48.208, float), lon=num("WEATHER_LON", 16.373, float),
                   prob=num("UMBRELLA_PROB", 50, int), hours=max(1, num("UMBRELLA_HOURS", 8, int)))


def rain_in(data: dict, prob: int, now: datetime) -> str:
    """Ответ Open-Meteo → «в 15:00 дождь (70 %)» или пустая строка, если дождя не ждём."""
    h = data.get("hourly") or {}
    times, probs, mm = h.get("time") or [], h.get("precipitation_probability") or [], h.get("precipitation") or []
    for i, t in enumerate(times):
        try:
            when = datetime.fromisoformat(t)
        except ValueError:
            continue
        if when.replace(minute=59) < now:        # час уже прошёл
            continue
        p = probs[i] if i < len(probs) and probs[i] is not None else 0
        m = mm[i] if i < len(mm) and mm[i] is not None else 0
        if p >= prob or m >= MIN_MM:
            return f"около {when:%H:%M} дождь ({p} %)" if p else f"около {when:%H:%M} дождь"
    return ""


class Umbrella:
    def __init__(self, cfg: UmbrellaConfig, announce: Callable[[str], Awaitable[bool]], *,
                 fetch: Fetch = urllib_fetch, night: Callable[[], bool] = lambda: False,
                 now: Callable[[], datetime] = datetime.now, clock: Callable[[], float] | None = None):
        import time
        self.cfg, self.announce, self.fetch, self.night = cfg, announce, fetch, night
        self.now = now
        self._clock = clock or time.monotonic
        self._last: float | None = None

    async def rain_soon(self) -> str:
        url = URL.format(lat=self.cfg.lat, lon=self.cfg.lon, hours=self.cfg.hours)
        data = json.loads(await self.fetch(url, None, {}))
        return rain_in(data, self.cfg.prob, self.now())

    async def on_leave(self) -> bool:
        """Человек вышел из дома. True — зонт напомнили."""
        if not self.cfg.enabled or self.night():
            return False
        if self._last is not None and self._clock() - self._last < REPEAT_S:
            return False
        try:
            when = await self.rain_soon()
        except Exception as e:
            log.warning("зонт: прогноз не получен: %r", e)
            return False
        if not when:
            return False
        if await self.announce(PROMPT.format(when=when)):
            self._last = self._clock()
            log.info("зонт: напомнил (%s)", when)
            return True
        return False
