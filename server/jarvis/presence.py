"""«Пользователь на месте?» Пока его нет, колонка не отзывается на слово и на ask.

Напоминания, таймеры, будильник и фоновые задачи это не касается: Voice проверяет
presence только для разговора, начатого человеком.

Логика по умолчанию (режим auto), сверху вниз, первое сработавшее правило решает:
  1. Ручное переопределение here/away (brain.db, ключ presence) — главнее всего.
  2. Компьютер активен (мышь/клавиатура за последние pc_min минут) — на месте:
     компьютер стоит дома, телефон же может быть в кармане на улице.
  3. HA говорит, что никого нет дома (все person.* не home) — нет на месте.
  4. Ночь (окно night_start..night_end из моста) и человек дома или HA молчит — на месте:
     колонка стоит у кровати, спящего «нет признаков активности» не должны отключать.
  5. Телефон: экран включён или разблокирован за последние phone_min минут — на месте.
  6. Нет ни одного признака больше idle_min минут, при этом мост на связи (телефон или
     компьютер подключены) или HA ответил — нет на месте (idle_min=0 отключает это правило).
  7. Иначе — на месте.
Если данных нет совсем (HA не настроен/молчит, телефон и компьютер не подключались),
считаем «на месте»: лучше лишний раз ответить, чем молчать.
Сильнее всего сигнал колонки room (BLE-маячок телефона или радар, docs/room-presence.md):
  room.here=true свежее ROOM_STALE_S — на месте (сразу после ручного переопределения);
  room.here=false дольше room_away_min минут — «в комнате нет» (но ночью, если HA не говорит «нет дома»
  и телефон на зарядке, этот сигнал игнорируется — человек мог оставить телефон в другой комнате);
  src=none или сообщений нет дольше ROOM_STALE_S — сигнал не используется.
JARVIS_PRESENCE=off — колонка всегда «на месте», команда presence тоже ничего не меняет.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from . import protocol

log = logging.getLogger(__name__)

MODES = ("auto", "here", "away")
PREF = "presence"
BLE_PREF = "ble_scan"
CACHE_S = 5.0            # here() зовут на каждый кусок звука — пересчитываем не чаще
HA_REFRESH_S = 60.0
HA_STALE_S = 600.0       # ответу HA старше 10 минут не верим
ROOM_STALE_S = 120.0     # колонка шлёт room раз в 30 с; молчит дольше 2 минут — сигналу не верим
ROOM_SOURCES = ("ble", "radar")


class Presence:
    def __init__(self, mode: str = "auto", *, bridge: Any = None, ha: Any = None, brain: Any = None,
                 pc_min: float = 10, phone_min: float = 10, idle_min: float = 180, room_away_min: float = 5, ble_uuid: str = "",
                 clock: Callable[[], float] = time.time):
        self.enabled = mode != "off"
        self.bridge, self.ha, self.brain = bridge, ha, brain
        self.hub: Any = None                 # DeviceHub: для команды ble
        self.pc_s, self.phone_s, self.idle_s = pc_min * 60, phone_min * 60, idle_min * 60
        self.room_away_s = room_away_min * 60
        self.ble_uuid = ble_uuid
        self._room: dict | None = None       # последнее room: here, rssi, src, at
        self._room_away_since: float | None = None
        self._clock = clock
        self._last_signal = clock()          # старт сервера считаем признаком жизни
        self._home: bool | None = None       # по HA: None — не знаем
        self._home_at = 0.0
        self._cache: tuple[float, bool, str] | None = None
        self._task: asyncio.Task | None = None
        self._last_log = -1e9

    # ── переопределение ──

    @property
    def override(self) -> str:
        v = self.brain.pref(PREF, "auto") if self.brain is not None else "auto"
        return v if v in MODES else "auto"

    def set_override(self, value: str) -> str:
        if value not in MODES:
            raise ValueError(f"presence {'|'.join(MODES)}")
        if self.brain is not None:
            self.brain.set_pref(PREF, value)
        self._cache = None
        return value

    # ── сигнал колонки room ──

    @property
    def ble_scan(self) -> bool:
        return self.brain is not None and self.brain.pref(BLE_PREF, "off") == "on"

    async def on_connect(self, dev: Any) -> None:
        """Колонка подключилась: сказать ей, сканировать ли BLE (в прошивке по умолчанию выключено)."""
        if self.ble_scan:
            await dev.send(protocol.ble_scan(True, self.ble_uuid))

    async def on_room(self, dev: Any, msg: dict) -> None:
        """Подписка на сообщения колонки: hub.on_message(presence.on_room)."""
        if msg.get("type") != "room":
            return
        src = msg.get("src")
        here = msg.get("here")
        if not isinstance(here, bool):
            return
        rssi = msg.get("rssi")
        now = self._clock()
        self._room = {"here": here, "src": src if isinstance(src, str) else "none",
                      "rssi": rssi if isinstance(rssi, int) and not isinstance(rssi, bool) else None,
                      "at": now}
        if self._room["src"] in ROOM_SOURCES and not here:
            if self._room_away_since is None:
                self._room_away_since = now
        else:
            self._room_away_since = None
        self._cache = None

    def _room_signal(self) -> bool | None:
        """True — колонка видит человека; False — не видит дольше room_away_min; None — не знаем."""
        r = self._room
        if r is None or r["src"] not in ROOM_SOURCES or self._clock() - r["at"] > ROOM_STALE_S:
            return None
        if r["here"]:
            return True
        if self._room_away_since is not None and self._clock() - self._room_away_since >= self.room_away_s:
            return False
        return None

    # ── HA ──

    async def refresh(self) -> None:
        """Спросить HA, кто дома. Любая ошибка — «не знаем»."""
        if self.ha is None:
            return
        try:
            ents = await self.ha.entities()
        except Exception as e:
            log.debug("presence: HA не ответил: %s", e)
            self._home = None
            return
        states = [e.state for e in ents if e.domain == "person"] \
            or [e.state for e in ents if e.domain == "device_tracker"]
        states = [s for s in states if s not in ("unknown", "unavailable")]
        self._home = (any(s == "home" for s in states)) if states else None
        self._home_at = self._clock()
        self._cache = None

    def _ha_home(self) -> bool | None:
        if self._home is None or self._clock() - self._home_at > HA_STALE_S:
            return None
        return self._home

    def start(self) -> None:
        if self.ha is not None and self.enabled:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await self.refresh()
            await asyncio.sleep(HA_REFRESH_S)

    # ── решение ──

    def _signals(self) -> tuple[bool, bool, bool]:
        """(компьютер активен, телефон в руках, есть ли вообще источники)"""
        b = self.bridge
        if b is None:
            return False, False, False
        now = self._clock()
        pc_on = b.pc is not None
        pc = pc_on and b.pc.state.get("idle", 1e9) < self.pc_s
        st = b.phone_state
        unlocked = st.get("unlocked_at", 0) / 1000
        phone = bool(st.get("screen")) or (unlocked > 0 and now - unlocked < self.phone_s)
        return pc, phone, pc_on or b.phone is not None

    def _night(self) -> bool:
        try:
            return bool(self.bridge is not None and self.bridge.night())
        except Exception:
            return False

    def _sleeping_at_home(self, home: bool | None) -> bool:
        """Ночь, HA не говорит «нет дома», телефон на зарядке: «колонка не видит» не отключает."""
        if not self._night() or home is False or self.bridge is None or self.bridge.phone is None:
            return False
        return bool(self.bridge.phone_state.get("charging"))

    def _decide(self) -> tuple[bool, str]:
        if not self.enabled:
            return True, "проверка присутствия выключена"
        ov = self.override
        if ov != "auto":
            return ov == "here", f"вручную: {'я на месте' if ov == 'here' else 'меня нет'}"
        pc, phone, connected = self._signals()
        home = self._ha_home()
        room = self._room_signal()
        if room is True:
            self._last_signal = self._clock()
            return True, f"колонка видит человека ({self._room['src']})"
        if room is False and not self._sleeping_at_home(home):
            return False, f"колонка не видит человека {round(self.room_away_s / 60)}+ мин"
        if pc:
            self._last_signal = self._clock()
            return True, "компьютер активен"
        if home is False:
            return False, "по HA никого нет дома"
        if self._night():
            return True, "ночь, колонка у кровати"
        if phone:
            self._last_signal = self._clock()
            return True, "телефон в руках"
        if self.idle_s > 0 and (connected or home is not None):
            quiet = self._clock() - self._last_signal
            if quiet > self.idle_s:
                return False, f"{round(quiet / 60)} мин нет признаков активности"
        return True, "признаков отсутствия нет"

    def check(self) -> tuple[bool, str]:
        now = self._clock()
        if self._cache is None or now - self._cache[0] > CACHE_S:
            ok, why = self._decide()
            self._cache = (now, ok, why)
        return self._cache[1], self._cache[2]

    def here(self) -> bool:
        return self.check()[0]

    @property
    def reason(self) -> str:
        return self.check()[1]

    def should_log(self) -> bool:
        """Не чаще раза в минуту: писать ли в журнал, что слово проигнорировано."""
        now = self._clock()
        if now - self._last_log < 60:
            return False
        self._last_log = now
        return True

    def describe(self) -> str:
        ok, why = self.check()
        ha = {True: "дома", False: "нет дома", None: "нет данных"}[self._ha_home()]
        r = self._room
        if r is None:
            room = "нет данных"
        else:
            age = round(self._clock() - r["at"])
            rssi = "" if r["rssi"] is None else f", {r['rssi']} дБм"
            room = f"{'здесь' if r['here'] else 'нет'} ({r['src']}{rssi}, {age} с назад)"
        return (f"{'на месте' if ok else 'НЕТ на месте'} ({why}); переопределение: {self.override}; "
                f"HA: {ha}; колонка: {room}")

    async def command(self, line: str) -> str | None:
        """Консоль и Telegram: presence [auto|here|away]."""
        parts = line.split()
        if parts and parts[0].lower() == "room":
            return self.describe()
        if parts and parts[0].lower() == "ble":
            if len(parts) != 2 or parts[1].lower() not in ("on", "off"):
                return "ble on|off — BLE-сканирование маячка телефона на колонке (сейчас: "                        f"{'on' if self.ble_scan else 'off'}); смотри room"
            on = parts[1].lower() == "on"
            if self.brain is not None:
                self.brain.set_pref(BLE_PREF, "on" if on else "off")
            if self.hub is None or self.hub.get() is None:
                return f"ble {'on' if on else 'off'} сохранено, колонка не подключена: применится при подключении"
            await self.hub.get().send(protocol.ble_scan(on, self.ble_uuid))
            return f"ble {'on' if on else 'off'}: отправлено колонке"
        if not parts or parts[0].lower() != "presence":
            return None
        if not self.enabled:
            return "проверка присутствия выключена (JARVIS_PRESENCE=off)"
        if len(parts) > 1:
            arg = parts[1].lower()
            if arg not in MODES:
                return "presence auto|here|away — auto: сам решает; here: я на месте; away: меня нет"
            self.set_override(arg)
        return self.describe()
