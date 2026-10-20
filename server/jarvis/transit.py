"""Автобус: отправления с реальным временем из HAFAS ÖBB (покрывает VOR и автобусы Нижней Австрии).

Бесплатно и без ключа: тот же запрос, что шлёт публичное приложение ÖBB (`aid` ниже вшит в само
приложение, это не чей-то личный ключ). Служба неофициальная, формат может смениться, поэтому
всё за интерфейсом Transit и подменяется в тестах. В .env: TRANSIT_STOP (название остановки или
её числовой extId), TRANSIT_LINE (номер линии; пусто — любая). Адрес HAFAS можно сменить
(TRANSIT_URL), если ÖBB закроет этот.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .untis import Fetch, urllib_fetch

log = logging.getLogger(__name__)

HAFAS_URL = "https://fahrplan.oebb.at/bin/mgate.exe"
_BODY = {"lang": "deu", "ver": "1.41",
         "client": {"id": "OEBB", "v": "6030600", "type": "IPH", "name": "oebbPROD-ADHOC"},
         "auth": {"type": "AID", "aid": "OWDL4fE4ixNiPBBm"}}
LATE_MIN = 3                  # с такого опоздания предупреждаем


class TransitError(Exception):
    pass


@dataclass(frozen=True)
class Departure:
    line: str
    direction: str
    planned: datetime
    real: datetime | None = None      # None — реального времени нет
    cancelled: bool = False

    @property
    def delay_min(self) -> int:
        return round((self.real - self.planned).total_seconds() / 60) if self.real else 0

    def describe(self) -> str:
        t = f"{self.planned:%H:%M}"
        if self.cancelled:
            return f"{self.line} в {t} отменён"
        if self.delay_min >= 1:
            return f"{self.line} в {t}, опаздывает на {self.delay_min} мин (будет в {self.real:%H:%M})"
        return f"{self.line} в {t}, по расписанию"


@dataclass(frozen=True)
class TransitConfig:
    stop: str = ""
    line: str = ""
    url: str = HAFAS_URL

    @classmethod
    def from_env(cls, env: dict[str, str]) -> TransitConfig:
        return cls(stop=env.get("TRANSIT_STOP", "").strip(), line=env.get("TRANSIT_LINE", "").strip(),
                   url=env.get("TRANSIT_URL", "").strip() or HAFAS_URL)


def _hafas_time(day: str, value: str | None) -> datetime | None:
    """'071000' или '01071000' (первые две цифры — сдвиг в днях) + дата 'ГГГГММДД'."""
    if not value:
        return None
    shift = 0
    if len(value) == 8:
        shift, value = int(value[:2]), value[2:]
    base = datetime.strptime(day + value[:4], "%Y%m%d%H%M")
    return base + timedelta(days=shift)


def parse_board(data: dict[str, Any], line: str = "") -> list[Departure]:
    res = data["svcResL"][0]
    if res.get("err") != "OK":
        raise TransitError(f"HAFAS: {res.get('err')} {res.get('errTxt', '')}".strip())
    r = res["res"]
    prods = r.get("common", {}).get("prodL", [])
    out = []
    for j in r.get("jnyL", []):
        stop = j.get("stbStop", {})
        prod = prods[j.get("prodX", 0)] if prods else {}
        name = prod.get("number") or re.sub(r"^\D+", "", prod.get("name", "")) or prod.get("name", "?")
        if line and name != line:
            continue
        planned = _hafas_time(j["date"], stop.get("dTimeS"))
        if planned is None:
            continue
        out.append(Departure(name, j.get("dirTxt", ""), planned, _hafas_time(j["date"], stop.get("dTimeR")),
                             bool(stop.get("dCncl") or j.get("isCncl"))))
    return sorted(out, key=lambda d: d.planned)


class Transit:
    def __init__(self, cfg: TransitConfig, *, fetch: Fetch = urllib_fetch,
                 now: Callable[[], datetime] = datetime.now):
        self.cfg = cfg
        self.fetch = fetch
        self.now = now
        self._stop_id = cfg.stop if cfg.stop.isdigit() else ""

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.stop)

    async def _call(self, meth: str, req: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps({**_BODY, "svcReqL": [{"cfg": {"polyEnc": "GPA"}, "meth": meth, "req": req}]}).encode()
        return json.loads(await self.fetch(self.cfg.url, body, {"Content-Type": "application/json"}))

    async def stop_id(self) -> str:
        if not self._stop_id:
            data = await self._call("LocMatch", {"input": {"loc": {"name": self.cfg.stop, "type": "S"},
                                                           "maxLoc": 3, "field": "S"}})
            res = data["svcResL"][0]
            locs = (res.get("res", {}).get("match", {}).get("locL") or [])
            if res.get("err") != "OK" or not locs:
                raise TransitError(f"остановка «{self.cfg.stop}» не найдена")
            self._stop_id = str(locs[0]["extId"])
            log.info("транспорт: остановка «%s» → %s (%s)", self.cfg.stop, self._stop_id, locs[0].get("name"))
        return self._stop_id

    async def departures(self, since: datetime | None = None, minutes: int = 90, limit: int = 12) -> list[Departure]:
        """Отправления не раньше `since` в ближайшие `minutes` минут; опоздавшие не теряются."""
        if not self.enabled:
            raise TransitError("остановка не задана (TRANSIT_STOP)")
        since = since or self.now()
        sid = await self.stop_id()
        data = await self._call("StationBoard", {
            "type": "DEP", "date": since.strftime("%Y%m%d"), "time": since.strftime("%H%M%S"),
            "stbLoc": {"lid": f"A=1@L={sid}@"}, "dur": minutes, "maxJny": limit * 3})
        return parse_board(data, self.cfg.line)[:limit]

    async def bus_before(self, when: datetime) -> Departure | None:
        """Последний автобус, который отходит не позже `when` (по расписанию)."""
        deps = await self.departures(when - timedelta(minutes=50), minutes=60, limit=30)
        return next((d for d in reversed(deps) if d.planned <= when), None)

    async def bus_at(self, when: datetime, tolerance_min: int = 4) -> Departure | None:
        """Рейс с плановым отправлением около `when`."""
        deps = await self.departures(when - timedelta(minutes=tolerance_min + 1), minutes=2 * tolerance_min + 3, limit=20)
        near = [d for d in deps if abs((d.planned - when).total_seconds()) <= tolerance_min * 60]
        return min(near, key=lambda d: abs((d.planned - when).total_seconds())) if near else None

    async def next_buses(self, n: int = 3) -> list[Departure]:
        deps = await self.departures(self.now() - timedelta(minutes=2), minutes=120, limit=n + 4)
        now = self.now()
        return [d for d in deps if (d.real or d.planned) >= now - timedelta(minutes=1)][:n]
