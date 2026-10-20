"""Снимки света спальни: «последний пользовательский вид» каждой лампы.

Зачем: рассвет (автоматизация HA «Рассвет к будильнику») красит все лампы в белый и гасит их, а
прежние цвета сами не возвращаются. Мы раз в минуту запоминаем вид включённых ламп (пока идёт
рассвет или наша сцена, снимки заморожены), а когда рассвет кончился и свет выключен, возвращаем
цвета лампам в ВЫКЛЮЧЕННОМ состоянии: WLED принимает {"on": false, "seg": [...]}, так что при
следующем включении лампа прежнего цвета.

WLED-лампы ходят по HTTP напрямую (адрес — configuration_url устройства в реестре HA),
всё остальное (ESPHome-диммеры «только яркость») — через HA, там запоминается яркость.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .ha import HAError, HomeAssistant

log = logging.getLogger(__name__)

TICK_S = 20.0
CAPTURE_EVERY_S = 60.0       # рассвет проверяем каждый тик, снимок — раз в минуту
LAMPS_TTL_S = 300.0
HTTP_TIMEOUT_S = 4.0
DAWN_MAX_S = 45 * 60          # рассвет не длится дольше (защита от «залипшего» флага)
DAWN_FRESH_S = 15 * 60        # автоматизация сработала недавно — идёт рассвет
GROUP_ID = "light.spalnia"
SEG_KEYS = ("id", "start", "stop", "grp", "spc", "on", "bri", "col", "fx", "sx", "ix", "pal", "c1", "c2", "c3",
            "sel", "rev", "mi", "o1", "o2", "o3")
SEG_VIEW = ("id", "col", "fx", "sx", "ix", "pal", "bri", "on")      # что запоминаем и возвращаем

Http = Callable[..., Awaitable[Any]]


async def default_http(method: str, url: str, body: Any = None) -> Any:
    def go() -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            raw = r.read()
        return json.loads(raw) if raw else None
    return await asyncio.to_thread(go)


@dataclass
class Lamp:
    entity_id: str
    name: str
    state: str                  # on / off / unavailable
    host: str = ""              # адрес WLED; пусто — не WLED
    brightness: int | None = None   # 0..255 по данным HA

    @property
    def is_wled(self) -> bool:
        return bool(self.host)

    @property
    def on(self) -> bool:
        return self.state == "on"

    @property
    def alive(self) -> bool:
        return self.state not in ("unavailable", "unknown")


class LightState:
    def __init__(self, ha: HomeAssistant, *, path: Path | str | None = None, http: Http = default_http,
                 area: str | None = None, clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now, sunrise_script: str | None = None,
                 sunrise_automation_id: str | None = None):
        self.ha = ha
        self.http = http
        self.path = Path(path) if path else None
        self.area = area or os.environ.get("JARVIS_LIGHT_AREA", "spalnia")
        self.sunrise_script = sunrise_script or os.environ.get("JARVIS_SUNRISE_SCRIPT", "script.sunrise")
        self.sunrise_auto = sunrise_automation_id or os.environ.get("JARVIS_SUNRISE_AUTOMATION_ID", "1756302078808")
        self._clock = clock
        self.now = now
        self.views: dict[str, dict[str, Any]] = {}      # entity_id → вид
        self._lamps: tuple[float, list[Lamp]] | None = None
        self._hold_until = 0.0
        self._dawn_since: float | None = None
        self._captured_at = -1e9
        self._task: asyncio.Task | None = None
        self._load()

    # ── файл ──

    def _load(self) -> None:
        if self.path and self.path.exists():
            try:
                self.views = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                self.views = {}

    def _save(self) -> None:
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(json.dumps(self.views, ensure_ascii=False), "utf-8")
            except OSError as e:
                log.warning("lightstate: не записал %s: %s", self.path, e)

    # ── лампы комнаты ──

    async def lamps(self, fresh: bool = False) -> list[Lamp]:
        """Отдельные лампы комнаты (без группы light.spalnia), с адресом WLED и текущим состоянием."""
        if self._lamps and not fresh and self._clock() - self._lamps[0] < LAMPS_TTL_S:
            known = {l.entity_id: l for l in self._lamps[1]}
            states = {s["entity_id"]: s for s in await self.ha.request("get_states")}
            for eid, lamp in known.items():
                if s := states.get(eid):
                    lamp.state = s["state"]
                    lamp.brightness = s.get("attributes", {}).get("brightness")
            return list(known.values())
        states, reg, devices = await asyncio.gather(
            self.ha.request("get_states"), self.ha.request("config/entity_registry/list"),
            self.ha.request("config/device_registry/list"))
        by_id = {s["entity_id"]: s for s in states}
        dev_info = {d["id"]: d for d in devices}
        info = {r["entity_id"]: r for r in reg}
        group = by_id.get(GROUP_ID, {}).get("attributes", {}).get("entity_id") or []
        wanted = set(group)
        for eid, r in info.items():
            dev = dev_info.get(r.get("device_id"), {})
            if eid.startswith("light.") and (r.get("area_id") or dev.get("area_id")) == self.area:
                wanted.add(eid)
        out: list[Lamp] = []
        for eid in sorted(wanted):
            s = by_id.get(eid)
            if s is None or eid == GROUP_ID or not eid.startswith("light."):
                continue
            attrs = s.get("attributes", {})
            if attrs.get("entity_id"):           # вложенная группа
                continue
            r = info.get(eid, {})
            if r.get("hidden_by") or r.get("disabled_by"):
                continue
            host = ""
            if r.get("platform") == "wled" or (dev_info.get(r.get("device_id"), {}).get("manufacturer") or "").lower() == "wled":
                url = dev_info.get(r.get("device_id"), {}).get("configuration_url") or ""
                host = urlparse(url).hostname or ""
            out.append(Lamp(eid, attrs.get("friendly_name") or eid, s["state"], host, attrs.get("brightness")))
        self._lamps = (self._clock(), out)
        return list(out)

    async def wled_lamps(self) -> list[Lamp]:
        """По одной записи на адрес (у лампы могут быть сегменты-сущности)."""
        seen, out = set(), []
        for l in await self.lamps():
            if l.is_wled and l.host not in seen:
                seen.add(l.host)
                out.append(l)
        return out

    # ── WLED ──

    async def wled_state(self, host: str) -> dict | None:
        try:
            return await self.http("GET", f"http://{host}/json/state")
        except Exception as e:
            log.debug("WLED %s не ответил: %r", host, e)
            return None

    async def wled_post(self, host: str, body: dict) -> bool:
        try:
            await self.http("POST", f"http://{host}/json/state", body)
            return True
        except Exception as e:
            log.warning("WLED %s: %r", host, e)
            return False

    @staticmethod
    def wled_view(st: dict) -> dict:
        return {"kind": "wled", "on": bool(st.get("on")), "bri": st.get("bri"),
                "seg": [{k: seg[k] for k in SEG_VIEW if k in seg} for seg in st.get("seg") or []]}

    # ── удержание: не запоминать, пока идёт наша сцена или рассвет ──

    def hold(self, seconds: float) -> None:
        self._hold_until = max(self._hold_until, self._clock() + seconds)

    def release(self) -> None:
        self._hold_until = 0.0

    @property
    def held(self) -> bool:
        return self._clock() < self._hold_until

    # ── снимки ──

    async def capture(self, force: bool = False) -> list[str]:
        """Запомнить вид включённых ламп. Пока удержание (или рассвет), без force ничего не пишет."""
        if not force and (self.held or self._dawn_since is not None):
            return []
        saved = []
        for lamp in await self.lamps():
            if not lamp.on:
                continue
            if lamp.is_wled:
                st = await self.wled_state(lamp.host)
                if st is None or not st.get("on"):
                    continue
                view = self.wled_view(st)
            else:
                if lamp.brightness is None:
                    continue
                view = {"kind": "ha", "bri": lamp.brightness}
            self.views[lamp.entity_id] = view
            saved.append(lamp.entity_id)
        # у лампы с несколькими сущностями вид один на адрес — раздаём остальным
        if saved:
            self._save()
        return saved

    def view_of(self, lamp: Lamp) -> dict | None:
        if view := self.views.get(lamp.entity_id):
            return view
        if lamp.is_wled:                              # вид мог быть записан под другой сущностью той же лампы
            for l in self._lamps[1] if self._lamps else ():
                if l.host == lamp.host and l.entity_id in self.views:
                    return self.views[l.entity_id]
        return None

    async def apply(self, lamp: Lamp, *, on: bool, transition_s: float = 0.0) -> bool:
        """Вернуть лампе запомненный вид; on=False — только краски, лампа остаётся выключенной."""
        view = self.view_of(lamp)
        if view is None:
            return False
        if lamp.is_wled and view.get("kind") == "wled":
            body: dict[str, Any] = {"on": on, "seg": view["seg"], "transition": int(transition_s * 10)}
            if view.get("bri"):
                body["bri"] = view["bri"]
            return await self.wled_post(lamp.host, body)
        if not on:
            return False                        # диммер не примет яркость в выключенном виде
        data: dict[str, Any] = {"brightness": view.get("bri") or 255}
        if transition_s:
            data["transition"] = transition_s
        await self.ha.request("call_service", domain="light", service="turn_on", service_data=data,
                              target={"entity_id": [lamp.entity_id]})
        return True

    async def restore_all(self, *, on: bool, transition_s: float = 0.0) -> list[str]:
        done = []
        wled_hosts = set()
        for lamp in await self.lamps():
            if not lamp.alive or (lamp.is_wled and lamp.host in wled_hosts):
                continue
            if await self.apply(lamp, on=on, transition_s=transition_s):
                done.append(lamp.entity_id)
                if lamp.is_wled:
                    wled_hosts.add(lamp.host)
        return done

    # ── рассвет ──

    async def dawn_active(self) -> bool:
        """Идёт ли сейчас рассвет: скрипт включён или автоматизация сработала недавно."""
        states = await self.ha.request("get_states")
        for s in states:
            eid = s["entity_id"]
            if eid == self.sunrise_script and s["state"] == "on":
                return True
            if eid.startswith("automation.") and str(s.get("attributes", {}).get("id")) == self.sunrise_auto:
                last = s.get("attributes", {}).get("last_triggered")
                if last:
                    try:
                        at = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - at).total_seconds() < DAWN_FRESH_S:
                            return True
                    except ValueError:
                        pass
        return False

    async def tick(self) -> str:
        """Один шаг фоновой работы. Возвращает, что сделали (для журнала и тестов)."""
        try:
            dawn = await self.dawn_active()
        except HAError as e:
            log.debug("lightstate: HA недоступен: %s", e)
            return "ha-down"
        now = self._clock()
        if dawn:
            if self._dawn_since is None:
                log.info("lightstate: рассвет начался, снимки заморожены")
            self._dawn_since = self._dawn_since if self._dawn_since is not None else now
            if now - self._dawn_since > DAWN_MAX_S:
                self._dawn_since = None
            return "dawn"
        if self._dawn_since is not None:
            lamps = await self.lamps(fresh=True)
            if all(not l.on for l in lamps) or now - self._dawn_since > DAWN_MAX_S:
                self._dawn_since = None
                if all(not l.on for l in lamps):
                    done = await self.restore_all(on=False)
                    log.info("lightstate: после рассвета вернул цвета: %s", done)
                    return "restored"
                return "dawn-timeout"
            return "dawn-lights-on"
        if now - self._captured_at < CAPTURE_EVERY_S:
            return "idle"
        self._captured_at = now
        saved = await self.capture()
        return f"captured {len(saved)}"

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("lightstate: шаг не удался")
            await asyncio.sleep(TICK_S)
