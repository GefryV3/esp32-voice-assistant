"""Мост телефон ↔ колонка ↔ компьютер.

Телефон (приложение phone/) и компьютер (pc/agent.py) подключаются к тому же порту, что и
колонка: /phone и /pc, с тем же токеном. Здесь решается, что из происходящего на телефоне
показать на колонке, и хранятся настройки колонки. Формат сообщений — docs/protocol.md.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from . import __version__, protocol
from .audio import tone
from .config import Settings
from .devices import Device, DeviceHub
from .protocol import Hello, Message, ProtocolError

log = logging.getLogger(__name__)

PHONE_PATH = "/phone"
PC_PATH = "/pc"
PC_ACTIVE_S = 120         # за компьютером, если мышь или клавиатуру трогали последние 2 минуты
BATTERY_LOW = 20
BATTERY_REPEAT_S = 3600   # про одну и ту же батарею — не чаще раза в час

DEFAULTS: dict[str, Any] = {
    "sleep_after_min": 3, "sleep_clock": "top", "brightness": 100,
    "night_start": "23:30", "night_end": "07:00",
    "bedtime_check": True, "bedtime_at": "23:00", "remark_from": "01:30",
    "sarcasm": 60, "mirror": True, "mirror_only_pc": True, "alarm_watchdog_min": 5,
    "volume": 50,
}
RANGES = {"sleep_after_min": (1, 60), "brightness": (5, 100), "sarcasm": (0, 100),
          "alarm_watchdog_min": (1, 30), "volume": (0, 100)}
CHOICES = {"sleep_clock": ("top", "bottom")}
TIMES = ("night_start", "night_end", "bedtime_at", "remark_from")


def _valid_time(v: Any) -> bool:
    if not isinstance(v, str) or len(v) != 5 or v[2] != ":":
        return False
    h, m = v[:2], v[3:]
    return h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60


def _minutes(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:])


def in_window(now_min: int, start: str, end: str) -> bool:
    """Попадает ли время (минуты от полуночи) в окно start–end, в том числе через полночь."""
    s, e = _minutes(start), _minutes(end)
    return s <= now_min < e if s <= e else (now_min >= s or now_min < e)


class SettingsStore:
    """Настройки колонки в data/settings.json. Неизвестные и кривые значения отбрасываются."""

    def __init__(self, path: Path):
        self.path = path
        self.values: dict[str, Any] = dict(DEFAULTS)
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.values.update(self.clean(saved))
        except (OSError, ValueError):
            pass

    @staticmethod
    def clean(patch: dict) -> dict:
        out: dict[str, Any] = {}
        for k, v in patch.items():
            if k not in DEFAULTS:
                continue
            d = DEFAULTS[k]
            if isinstance(d, bool):
                if isinstance(v, bool):
                    out[k] = v
            elif isinstance(d, int):
                if isinstance(v, int) and not isinstance(v, bool):
                    lo, hi = RANGES.get(k, (v, v))
                    out[k] = min(max(v, lo), hi)
            elif k in CHOICES:
                if v in CHOICES[k]:
                    out[k] = v
            elif k in TIMES and _valid_time(v):
                out[k] = v
        return out

    def update(self, patch: dict) -> dict[str, Any]:
        self.values.update(self.clean(patch))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("настройки не сохранились: %s", e)
        return self.values


@dataclass
class Peer:
    """Телефон или компьютер на связи."""
    hello: Hello
    ws: ServerConnection
    state: dict[str, Any] = field(default_factory=dict)

    async def send(self, msg: Message) -> bool:
        try:
            await self.ws.send(protocol.encode(msg))
            return True
        except ConnectionClosed:
            return False


class Bridge:
    def __init__(self, hub: DeviceHub, settings: Settings, store: SettingsStore, *,
                 clock: Callable[[], float] = time.time,
                 local_now: Callable[[], datetime] = datetime.now):
        self.hub = hub
        self.store = store
        self._settings = settings
        self._clock = clock
        self._now = local_now
        self.phone: Peer | None = None
        self.pc: Peer | None = None
        self.phone_state: dict[str, Any] = {}
        self.notifs: dict[str, Message] = {}   # последние уведомления — чтобы было на что ответить
        self.bt: list[dict] = []
        self._last_warn: dict[str, float] = {}
        self._remark_night = ""     # за какую ночь колкость уже была: раз за ночь
        self._screen_off_at = 0.0
        self._watchdog: asyncio.Task | None = None
        self._bedtime_day = ""
        self._loop_task: asyncio.Task | None = None
        self.on_wake: Callable[[], None] | None = None   # будильник выключен — человек проснулся
        self.on_slept: Callable[[float, datetime], None] | None = None   # (часов сна, когда лёг) — для недельной сводки
        self._background: set[asyncio.Task] = set()
        self._last_screen: dict[str, Any] = {}
        hub.bridge = self
        hub.add_route(PHONE_PATH, self._handle_phone)
        hub.add_route(PC_PATH, self._handle_pc)
        hub.on_connect(self._on_speaker)
        hub.on_message(self._on_speaker_message)

    # ── жизненный цикл ──

    def start(self) -> None:
        self._loop_task = asyncio.create_task(self._clock_loop())

    async def stop(self) -> None:
        for t in (self._loop_task, self._watchdog):
            if t is not None:
                t.cancel()

    # ── колонка ──

    async def speaker(self, msg: Message) -> bool:
        dev = self.hub.get()
        if dev is None:
            return False
        try:
            await dev.send(msg)
            return True
        except ConnectionClosed:
            return False

    def speaker_config(self) -> list[Message]:
        v = self.store.values
        return [protocol.config(v["sleep_after_min"] * 60, v["sleep_clock"]),
                protocol.brightness(round(v["brightness"] * 255 / 100)),
                protocol.volume(v["volume"])]

    async def _on_speaker(self, dev: Device) -> None:
        for m in self.speaker_config():
            await dev.send(m)
        await self.push_settings()

    async def _on_speaker_message(self, dev: Device, msg: Message) -> None:
        if msg["type"] == "status":
            await self.push_settings()

    def screen_summary(self) -> dict[str, Any]:
        dev = self.hub.get()
        if dev is None:
            return {"mode": "offline", "title": "Не на связи", "detail": ""}
        return dev.screen.summary(time.time())   # те же часы, что у Device.send

    def screen_changed(self, dev: Device) -> None:
        """Колонке что-то отправили: если режим экрана поменялся — сказать телефону."""
        if self.phone is None or self.screen_summary() == self._last_screen:
            return
        task = asyncio.create_task(self.push_settings())   # не задерживаем того, кто шлёт колонке
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def push_settings(self) -> None:
        """Телефону — настройки колонки и её состояние, в том числе что сейчас на экране."""
        if self.phone is None:
            return
        dev = self.hub.get()
        self._last_screen = self.screen_summary()
        await self.phone.send({
            "type": "settings", "values": self.store.values,
            "speaker": {"online": dev is not None, "fps": int(dev.status.get("fps", 0)) if dev else 0,
                        "screen": self._last_screen},
            "pc": self.pc is not None,
        })

    # ── правила ──

    def night(self) -> bool:
        now = self._now()
        v = self.store.values
        return in_window(now.hour * 60 + now.minute, v["night_start"], v["night_end"])

    def pc_active(self) -> bool:
        return self.pc is not None and self.pc.state.get("idle", 1e9) < PC_ACTIVE_S

    def should_mirror(self) -> bool:
        """Показать уведомление с телефона на колонке: телефон не в руках, я за компьютером
        (если так настроено), и сейчас не ночь."""
        v = self.store.values
        if not v["mirror"] or self.night():
            return False
        if self.phone_state.get("screen", True):
            return False  # телефон в руках — и так видно
        return self.pc_active() or not v["mirror_only_pc"]

    # ── соединения телефона и компьютера ──

    async def _accept(self, ws: ServerConnection, what: str) -> Peer | None:
        try:
            raw = await asyncio.wait_for(ws.recv(), protocol.HELLO_TIMEOUT_S)
            hello = protocol.parse_hello(raw)
            if not hmac.compare_digest(hello.token, self._settings.device_token):
                raise ProtocolError("неверный токен", protocol.CLOSE_BAD_TOKEN)
        except TimeoutError:
            await ws.close(protocol.CLOSE_BAD_HELLO, "no hello")
            return None
        except ProtocolError as e:
            log.warning("%s: отказ %s: %s", what, ws.remote_address, e)
            await ws.close(e.close_code, str(e)[:120])
            return None
        except ConnectionClosed:
            return None
        await ws.send(protocol.encode(protocol.welcome(self._clock(), self._settings.tz, __version__)))
        return Peer(hello, ws)

    async def _serve(self, ws: ServerConnection, peer: Peer, handler: Callable) -> None:
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    continue
                try:
                    msg = protocol.decode(raw)
                except ProtocolError as e:
                    log.warning("%s: %s", peer.hello.device, e)
                    continue
                try:
                    await handler(msg)
                except Exception:
                    log.exception("сообщение %s от %s", msg.get("type"), peer.hello.device)
        except ConnectionClosed:
            pass

    def _replace(self, old: Peer | None) -> None:
        if old is not None:
            task = asyncio.create_task(old.ws.close(protocol.CLOSE_REPLACED, "replaced"))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _handle_phone(self, ws: ServerConnection) -> None:
        peer = await self._accept(ws, "телефон")
        if peer is None:
            return
        old, self.phone = self.phone, peer
        self._replace(old)
        log.info("подключился телефон (приложение %s)", peer.hello.fw)
        await self.push_settings()
        try:
            await self._serve(ws, peer, self.on_phone)
        finally:
            if self.phone is peer:
                self.phone = None
                log.info("телефон отключился")

    async def _handle_pc(self, ws: ServerConnection) -> None:
        peer = await self._accept(ws, "компьютер")
        if peer is None:
            return
        old, self.pc = self.pc, peer
        self._replace(old)
        log.info("подключился компьютер")
        await self.push_settings()
        try:
            await self._serve(ws, peer, self.on_pc)
        finally:
            if self.pc is peer:
                self.pc = None
                log.info("компьютер отключился")
                await self.push_settings()

    # ── сообщения телефона ──

    async def on_phone(self, msg: Message) -> None:
        t = msg["type"]
        now = self._clock()
        if t == "state":
            was_on = self.phone_state.get("screen")
            self.phone_state.update({k: v for k, v in msg.items() if k != "type"})
            on = self.phone_state.get("screen")
            if was_on and on is False:
                self._screen_off_at = now
            if on and not was_on:
                await self._maybe_remark()
            await self._check_battery("Телефон", self.phone_state.get("battery", -1),
                                      bool(self.phone_state.get("charging")))
        elif t == "notif":
            key = str(msg.get("key", ""))
            self.notifs[key] = msg
            while len(self.notifs) > 50:
                self.notifs.pop(next(iter(self.notifs)))
            if self.should_mirror():
                await self.speaker(protocol.notify(
                    "msg", str(msg.get("title", "")), str(msg.get("text", "")), app=str(msg.get("app_name", ""))))
        elif t == "notif_gone":
            self.notifs.pop(str(msg.get("key", "")), None)
        elif t == "call":
            state = msg.get("state")
            if state == "ringing":
                who = str(msg.get("name") or msg.get("number") or "Неизвестный номер")
                await self.speaker(protocol.notify("call", who, "звонит", ms=45_000))
            else:
                await self.speaker(protocol.notify_hide())
        elif t == "media":
            state = msg.get("state")
            if (lights := getattr(self.hub, "lights", None)) is not None:
                await lights.on_media(msg)       # свет под музыку: palette обложки
            await self.speaker(protocol.player(
                state if state in ("playing", "paused") else "none",
                str(msg.get("title", "")), str(msg.get("artist", "")),
                int(msg.get("pos", 0) or 0), int(msg.get("dur", 0) or 0)))
        elif t == "alarm":
            if msg.get("event") == "dismissed":
                await self._alarm_dismissed(now)
        elif t == "bt":
            devices = msg.get("devices")
            self.bt = [d for d in devices if isinstance(d, dict)] if isinstance(devices, list) else []
            for d in self.bt:
                await self._check_battery(str(d.get("name", "Наушники")), d.get("level", -1), False)
        elif t == "share":
            text = str(msg.get("text", "")).strip()
            if text and self.pc is not None:
                await self.pc.send({"type": "open_url", "url": text})
            elif text:
                log.info("ссылка с телефона, но компьютер не на связи: %s", text)
        elif t == "settings":
            values = msg.get("values")
            if isinstance(values, dict):
                self.store.update(values)
                for m in self.speaker_config():
                    await self.speaker(m)
                await self.push_settings()
        elif t == "test":
            await self.speaker(protocol.notify("msg", "Мама", "Ужин готов, спускайся", app="Telegram"))
        elif t == "beep":   # «Проверить звук» в приложении
            dev = self.hub.get()
            if dev is not None and self.hub.audio is not None and "speaker" in dev.hello.caps:
                await self.hub.audio.play(dev, tone(660, 150) + bytes(1600) + tone(880, 220))

    # ── сообщения компьютера ──

    async def on_pc(self, msg: Message) -> None:
        if self.pc is None:
            return
        if msg["type"] == "pc":
            self.pc.state.update({k: v for k, v in msg.items() if k != "type"})
            await self._check_battery("Ноутбук", self.pc.state.get("battery", -1),
                                      bool(self.pc.state.get("charging")))
        elif msg["type"] == "to_phone":
            url = str(msg.get("url", "")).strip()
            if url and self.phone is not None:
                await self.phone.send({"type": "open_url", "url": url})

    # ── сон, будильник, батареи ──

    def _sarcastic(self) -> bool:
        return self.store.values["sarcasm"] >= 45

    async def _maybe_remark(self) -> None:
        """Сидит с телефоном глубокой ночью (с remark_from, по умолчанию 01:30), а будильник
        скоро — одна колкость за ночь. Короткие подъёмы вечером и под утро не трогаем."""
        now = self._clock()
        local = self._now()
        v = self.store.values
        if not in_window(local.hour * 60 + local.minute, v["remark_from"], v["night_end"]):
            return
        night = (local - timedelta(hours=12)).strftime("%Y-%m-%d")
        if night == self._remark_night:
            return
        left = self.phone_state.get("next_alarm", 0) / 1000 - now
        if not 0 < left < 10 * 3600:
            return
        self._remark_night = night
        h, m = divmod(int(left // 60), 60)
        title = f"Подъём через {h} ч {m:02d} мин"
        text = "Просто к сведению." if self._sarcastic() else "Пора спать."
        await self.speaker(protocol.notify("remark", title, text, ms=10_000))

    async def _alarm_dismissed(self, at: float) -> None:
        if self.on_wake is not None:
            self.on_wake()
        # «спал X ч»: от последнего выключения экрана ночью до будильника
        slept = at - self._screen_off_at
        if 3 * 3600 < slept < 14 * 3600:
            lay = datetime.fromtimestamp(self._screen_off_at)
            if self.on_slept is not None:
                self.on_slept(slept / 3600, lay)
            h, m = divmod(int(slept // 60), 60)
            await self.speaker(protocol.notify("info", f"Спал {h} ч {m:02d} мин", f"Лёг в {lay:%H:%M}", ms=15_000))
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._watchdog = asyncio.create_task(self._watch_alarm(at))

    async def _watch_alarm(self, dismissed_at: float) -> None:
        """Будильник выключен, но телефон так и не разблокирован — значит, уснул обратно."""
        await asyncio.sleep(self.store.values["alarm_watchdog_min"] * 60)
        unlocked = self.phone_state.get("unlocked_at", 0) / 1000
        if unlocked >= dismissed_at or self.pc_active():
            return
        title = "Будильник выключен, а ты спишь"
        text = "Вставай. Я всё вижу." if self._sarcastic() else "Пора вставать."
        await self.speaker(protocol.emotion("surprise", 4000))
        await self.speaker(protocol.notify("wake", title, text, ms=60_000))
        log.info("разбудил: будильник выключен в %s, телефон не разблокирован",
                 datetime.fromtimestamp(dismissed_at).strftime("%H:%M"))

    async def _check_battery(self, name: str, level: Any, charging: bool) -> None:
        if not isinstance(level, int) or isinstance(level, bool) or level < 0 or level > BATTERY_LOW or charging:
            return
        now = self._clock()
        if now - self._last_warn.get(name, 0) < BATTERY_REPEAT_S or self.night():
            return
        self._last_warn[name] = now
        await self.speaker(protocol.notify("battery", name, f"{level} % · поставь на зарядку", ms=12_000, level=level))

    def bedtime_summary(self) -> tuple[str, str]:
        parts = []
        alarm = self.phone_state.get("next_alarm", 0)
        parts.append(f"будильник {datetime.fromtimestamp(alarm / 1000):%H:%M}" if alarm else "будильник не заведён")
        battery = self.phone_state.get("battery", -1)
        if isinstance(battery, int) and battery >= 0:
            parts.append(f"телефон {battery} %" + (", заряжается" if self.phone_state.get("charging") else ""))
        for d in self.bt:
            parts.append(f"{d.get('name', 'наушники')} {d.get('level')} %")
        if self.pc is not None and self.pc.state.get("battery", -1) >= 0:
            parts.append(f"ноутбук {self.pc.state['battery']} %")
        return "Перед сном", " · ".join(parts)

    async def _clock_loop(self) -> None:
        """Раз в 5 с: не уснула ли колонка, не кончился ли таймер или карточка (сказать
        телефону) и не пора ли показать проверку перед сном."""
        while True:
            await asyncio.sleep(5)
            if self.phone is not None and self.screen_summary() != self._last_screen:
                await self.push_settings()
            v = self.store.values
            now = self._now()
            day = now.strftime("%Y-%m-%d")
            if v["bedtime_check"] and now.strftime("%H:%M") == v["bedtime_at"] and day != self._bedtime_day:
                self._bedtime_day = day
                title, text = self.bedtime_summary()
                await self.speaker(protocol.notify("check", title, text, ms=30_000))

    # ── команды консоли ──

    async def command(self, line: str) -> str | None:
        """Команды для телефона и компьютера; None — это не команда моста."""
        parts = line.split(maxsplit=1)
        if not parts:
            return None
        cmd, rest = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        phone = self.phone
        if cmd in ("ring", "reply", "decline", "player", "link") and phone is None:
            return "телефон не на связи"
        match cmd:
            case "ring":
                await phone.send({"type": "ring", "on": rest.strip() != "off"})
                return "→ телефон: ring"
            case "reply":
                key = next((k for k, n in reversed(self.notifs.items()) if n.get("can_reply")), None)
                if key is None:
                    return "нет уведомления, на которое можно ответить"
                await phone.send({"type": "reply", "key": key, "text": rest})
                n = self.notifs[key]
                return f"→ ответ в {n.get('app_name')} ({n.get('title')}): {rest}"
            case "decline":
                await phone.send({"type": "call", "sms": rest})
                return "→ телефон: сбросить звонок" + (f" и SMS «{rest}»" if rest else "")
            case "player":
                await phone.send({"type": "media", "cmd": rest.strip() or "toggle"})
                return f"→ телефон: плеер {rest.strip() or 'toggle'}"
            case "link":
                await phone.send({"type": "open_url", "url": rest.strip()})
                return "→ телефон: ссылка"
            case "pcurl":
                if self.pc is None:
                    return "компьютер не на связи"
                await self.pc.send({"type": "open_url", "url": rest.strip()})
                return "→ компьютер: ссылка"
            case "vol":
                if not rest.strip().isdigit():
                    return f"vol 0..100, сейчас {self.store.values['volume']}"
                level = self.store.update({"volume": int(rest)})["volume"]
                await self.speaker(protocol.volume(level))
                await self.push_settings()
                return f"→ колонка: громкость {level}"
            case "notify":
                await self.speaker(protocol.notify("msg", "Проверка", rest or "Уведомление с телефона", app="Консоль"))
                return "→ колонка: уведомление"
            case "card":  # показать любую карточку: card call|battery|remark|wake|info|check
                kind = rest.strip() or "msg"
                samples = {
                    "msg": ("Мама", "Ужин готов, спускайся. И телефон захвати", "Telegram", None),
                    "call": ("Мама", "звонит", "", None),
                    "battery": ("Nothing Ear (a)", "15 % · поставь на зарядку", "", 15),
                    "remark": ("Подъём через 6 ч 10 мин", "Просто к сведению.", "", None),
                    "wake": ("Будильник выключен, а ты спишь", "Вставай. Я всё вижу.", "", None),
                    "info": ("Спал 6 ч 40 мин", "Лёг в 00:52", "", None),
                    "check": ("Перед сном", "будильник 07:30 · телефон 40 %, заряжается · Nothing Ear (a) 55 % · ноутбук 85 %", "", None),
                }
                if kind not in samples:
                    return "card msg|call|battery|remark|wake|info|check"
                title, text, app, level = samples[kind]
                await self.speaker(protocol.notify(kind, title, text, app=app, ms=12_000, level=level))
                return f"→ колонка: карточка {kind}"
            case "song":  # проверить мини-плеер: song on|pause|off
                state = {"on": "playing", "pause": "paused", "off": "none"}.get(rest.strip() or "on", "playing")
                await self.speaker(protocol.player(state, "Nights", "Frank Ocean", 42_000, 307_000))
                return f"→ колонка: плеер {state}"
            case "check":
                title, text = self.bedtime_summary()
                await self.speaker(protocol.notify("check", title, text, ms=30_000))
                return f"→ колонка: {text}"
            case "phone":
                return (f"телефон: {'на связи' if phone else 'нет'} {self.phone_state}\n"
                        f"компьютер: {'на связи ' + str(self.pc.state) if self.pc else 'нет'}\n"
                        f"наушники: {self.bt}\nпоказывать уведомления сейчас: {self.should_mirror()}")
        return None
