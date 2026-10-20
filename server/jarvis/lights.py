"""Сценарии света спальни: «спокойной ночи», хлопок-хлопок, свет под музыку.

Всё, что красит лампы, держится на lightstate.py: перед сценой запоминаем вид ламп, на время сцены
замораживаем снимки (чтобы наша ночная подсветка или цвет обложки не стали «любимым видом»), потом
возвращаем. Сценарии зовутся из функций Gemini (`goodnight`, `music_light`), из консоли и из
сообщения колонки `clap`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from . import protocol
from .devices import Device, DeviceHub
from .ha import HAError, HomeAssistant
from .lightstate import Lamp, LightState

log = logging.getLogger(__name__)

GOODNIGHT_FADE_S = 30.0
CLAP_FADE_S = 3.0
CLAP_COOLDOWN_S = 3.0
NIGHT_KELVIN = 2200
NIGHT_PCT = 15
MUSIC_LAMP = "light.podsvetka_stola"
MUSIC_BRI = 110                  # из 255: комфортно вечером
MUSIC_STOP_AFTER_S = 20.0        # пауза дольше этого — считаем, что музыка кончилась
MUSIC_TRANSITION_S = 2.0
SPEECH_WAIT_S = 90.0
SCREEN_STEPS = (200, 150, 100, 60, 30, 10, 0)

TOOLS: list[dict[str, Any]] = [
    {"name": "goodnight", "description": "«Спокойной ночи»: весь свет спальни плавно гаснет за полминуты, "
     "потом ты сама произнесёшь короткую сводку (будильник, завтрашнее первое дело), экран темнеет. "
     "Зови на «спокойной ночи», «я спать», «пора спать». Сам ничего не говори про сводку: скажи только "
     "короткое «спокойной ночи» и вызови end_conversation.",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "music_light", "description": "Подсветка стола окрашивается в цвета обложки играющего на телефоне "
     "трека. action: on, off или status. Когда музыка кончится, лампа вернёт прежний вид.",
     "parameters": {"type": "OBJECT", "properties": {"action": {"type": "STRING"}}, "required": ["action"]}},
]


def clean_palette(raw: Any, limit: int = 3) -> list[list[int]]:
    """['#RRGGBB', ...] из обложки → список [r, g, b] (до limit штук), тёмные подтянуты к яркости."""
    out: list[list[int]] = []
    for item in raw if isinstance(raw, list) else []:
        h = str(item).strip().lstrip("#")
        if len(h) != 6:
            continue
        try:
            rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
        except ValueError:
            continue
        top = max(rgb)
        if top == 0:
            continue
        if top < 160:                      # тёмный цвет на ленте выглядит грязным
            rgb = [min(255, round(c * 160 / top)) for c in rgb]
        if rgb not in out:
            out.append(rgb)
        if len(out) == limit:
            break
    return out


class Lights:
    def __init__(self, hub: DeviceHub, ha: HomeAssistant, state: LightState, *, bridge=None, voice=None,
                 brain=None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 now: Callable[[], datetime] = datetime.now, clock: Callable[[], float] | None = None):
        self.hub, self.ha, self.state = hub, ha, state
        self.bridge, self.voice, self.brain = bridge, voice, brain
        self._sleep = sleep
        self.now = now
        self._clock = clock or asyncio.get_running_loop().time
        self._last_clap = -1e9
        self._goodnight: asyncio.Task | None = None
        self.music_lamp = os.environ.get("JARVIS_MUSIC_LAMP", MUSIC_LAMP)
        self.music_on = bool(brain and brain.pref("music_light") == "1")
        self._music_palette: list[list[int]] = []
        self._music_playing = False
        self._music_active = False           # лампа сейчас покрашена нами
        self._music_before_on = False
        self._music_stop: asyncio.Task | None = None
        self._effects: dict[str, tuple[dict[str, int], dict[str, int]]] = {}
        hub.lights = self
        hub.on_message(self._on_message)

    # ── общее ──

    def night(self) -> bool:
        if self.bridge is not None:
            return bool(self.bridge.night())
        return self.now().hour >= 23 or self.now().hour < 7

    async def _call(self, service: str, entity_ids: list[str], data: dict[str, Any] | None = None) -> None:
        if not entity_ids:
            return
        try:
            await self.ha.request("call_service", domain="light", service=service, service_data=data or {},
                                  target={"entity_id": entity_ids})
        except HAError as e:
            log.warning("свет: %s %s: %s", service, entity_ids, e)

    async def fade_out(self, seconds: float) -> list[str]:
        """Плавно погасить ВСЕ лампы комнаты. Возвращает те, что не удалось погасить."""
        lamps = [l for l in await self.state.lamps(fresh=True) if l.alive]
        ids = [l.entity_id for l in lamps]
        tenths = int(seconds * 10)
        await asyncio.gather(
            self._call("turn_off", ids, {"transition": seconds}),
            *(self.state.wled_post(l.host, {"on": False, "transition": tenths})
              for l in await self.state.wled_lamps() if l.alive and l.on))
        await self._sleep(seconds + 2)
        left: list[Lamp] = []
        for attempt in range(2):          # HA-переход бывает ненадёжен: добиваем без плавности
            left = [l for l in await self.state.lamps(fresh=True) if l.on]
            if not left:
                break
            log.warning("свет не погас сам: %s — гашу сразу", [l.name for l in left])
            await self._call("turn_off", [l.entity_id for l in left])
            for l in left:
                if l.is_wled:
                    await self.state.wled_post(l.host, {"on": False, "transition": 0})
            await self._sleep(1)
        else:
            left = [l for l in await self.state.lamps(fresh=True) if l.on]
        return [l.name for l in left]

    # ── «спокойной ночи» ──

    async def goodnight(self) -> dict:
        if self._goodnight and not self._goodnight.done():
            return {"ok": True, "note": "уже гашу"}
        self._goodnight = asyncio.create_task(self._goodnight_run())
        return {"ok": True, "note": "свет погаснет за полминуты, сводку скажу сама; ты скажи только "
                                    "короткое «спокойной ночи» и вызови end_conversation"}

    def summary_prompt(self) -> str:
        phone = getattr(self.bridge, "phone_state", {}) or {}
        alarm = phone.get("next_alarm", 0)
        parts = [f"будильник на {datetime.fromtimestamp(alarm / 1000):%H:%M}" if alarm
                 else "будильник не заведён"]
        if phone and phone.get("charging") is False and phone.get("battery", 100) < 30:
            parts.append(f"телефон сел до {phone['battery']} процентов и не на зарядке")
        cal = ""
        if self.brain is not None and getattr(self.brain, "google", None):
            cal = ("Вызови google_calendar_list и назови первое дело на завтра (если есть), одной короткой частью фразы. ")
        elif self.brain is not None and getattr(self.brain, "ha", None):
            cal = "Вызови home_calendar и назови первое дело на завтра (если есть), коротко. "
        return ("Пользователь ложится спать, свет в комнате уже погас. Скажи ОДНУ короткую фразу-сводку вполголоса, "
                f"спокойно: {', '.join(parts)}. {cal}Ничего не выдумывай, в конце пожелай спокойной ночи. "
                "После этого вызови end_conversation.")

    async def _goodnight_run(self) -> None:
        try:
            await self.state.capture(force=True)
            self.state.hold(GOODNIGHT_FADE_S + 120)
            left = await self.fade_out(GOODNIGHT_FADE_S)
            if left:
                log.error("спокойной ночи: не погасли %s", left)
            if self.voice is not None:
                for _ in range(6):
                    if await self.voice.announce(self.summary_prompt()):
                        break
                    await self._sleep(3)
                await self._wait_speech_end()
            await self._dim_screen()
        except Exception:
            log.exception("спокойной ночи упала")

    async def _wait_speech_end(self) -> None:
        talks = getattr(self.voice, "_talks", {})
        await self._sleep(2)
        waited = 0.0
        while talks and waited < SPEECH_WAIT_S:
            await self._sleep(1)
            waited += 1

    async def _dim_screen(self) -> None:
        dev = self.hub.get()
        if dev is None:
            return
        for level in SCREEN_STEPS:
            await dev.send(protocol.brightness(level))
            await self._sleep(1.2)
        await dev.send(protocol.night(True))
        await self._sleep(1)
        await dev.send(protocol.brightness(255))      # яркость на утро; экран спит в ночном режиме

    # ── хлопки ──

    async def _on_message(self, dev: Device, msg: dict) -> None:
        if msg.get("type") == "clap":
            await self.clap(int(msg.get("count", 0) or 0))

    async def clap(self, count: int) -> str:
        if count != 2:
            return f"хлопков {count}: игнорирую"
        t = self._clock()
        if t - self._last_clap < CLAP_COOLDOWN_S:
            return "слишком часто"
        self._last_clap = t
        return await self.toggle_room()

    async def toggle_room(self) -> str:
        lamps = [l for l in await self.state.lamps(fresh=True) if l.alive]
        if not lamps:
            return "ламп нет"
        if any(l.on for l in lamps):
            await self.state.capture(force=True)
            self.state.hold(60)
            left = await self.fade_out(CLAP_FADE_S)
            return "погасил" + (f", не погасли: {left}" if left else "")
        if self.night():
            self.state.hold(3600)
            await self._call("turn_on", [l.entity_id for l in lamps],
                             {"color_temp_kelvin": NIGHT_KELVIN, "brightness_pct": NIGHT_PCT, "transition": 1})
            return "включил тёплым приглушённым (ночь)"
        plain = []
        for l in lamps:
            if not await self.state.apply(l, on=True, transition_s=1):
                plain.append(l.entity_id)
        await self._call("turn_on", plain, {"transition": 1})
        return "включил" + (f" ({len(plain)} без запомненного вида)" if plain else ", вид вернул")

    async def room(self, action: str) -> dict:
        """Быстрые команды света для голоса без Gemini: on, off, dim, bright. → {ok, note|error}."""
        lamps = [l for l in await self.state.lamps(fresh=True) if l.alive]
        if not lamps:
            return {"ok": False, "error": "не нашёл ни одной лампы"}
        lit = [l for l in lamps if l.on]
        if action == "on":
            if lit:
                return {"ok": True, "note": "Свет уже горит"}
            await self.toggle_room()
            return {"ok": True, "note": "Свет включён"}
        if action == "off":
            if not lit:
                return {"ok": True, "note": "Свет уже выключен"}
            await self.state.capture(force=True)
            self.state.hold(60)
            await asyncio.gather(
                self._call("turn_off", [l.entity_id for l in lit], {"transition": 0.5}),
                *(self.state.wled_post(l.host, {"on": False, "transition": 5}) for l in lit if l.is_wled))
            return {"ok": True, "note": "Свет выключен"}
        if action in ("dim", "bright"):
            if not lit:
                return {"ok": False, "error": "свет выключен"}
            step = -25 if action == "dim" else 25
            await self._call("turn_on", [l.entity_id for l in lit], {"brightness_step_pct": step, "transition": 0.5})
            return {"ok": True, "note": "Потемнее" if action == "dim" else "Поярче"}
        return {"ok": False, "error": f"нет такой команды света: {action}"}

    # ── свет под музыку ──

    async def on_media(self, msg: dict) -> None:
        """Сообщение телефона `media` (трек, пауза, palette обложки)."""
        playing = msg.get("state") == "playing"
        self._music_playing = playing
        palette = clean_palette(msg.get("palette"))
        if playing and self._music_stop:
            self._music_stop.cancel()
            self._music_stop = None
        if not self.music_on:
            return
        if playing:
            if palette and palette != self._music_palette or (palette and not self._music_active):
                self._music_palette = palette
                await self._music_paint()
        elif self._music_active and self._music_stop is None:
            self._music_stop = asyncio.create_task(self._music_stop_later())

    async def _music_stop_later(self) -> None:
        try:
            await self._sleep(MUSIC_STOP_AFTER_S)
            await self.music_restore()
        finally:
            self._music_stop = None

    async def _effect_ids(self, host: str) -> tuple[int, int]:
        if host not in self._effects:
            fx, pal = {}, {}
            try:
                names = await self.state.http("GET", f"http://{host}/json/effects") or []
                fx = {n: i for i, n in enumerate(names)}
                names = await self.state.http("GET", f"http://{host}/json/palettes") or []
                pal = {n: i for i, n in enumerate(names)}
            except Exception as e:
                log.debug("WLED %s: список эффектов не получен: %r", host, e)
            self._effects[host] = (fx, pal)
        fx, pal = self._effects[host]
        fx_id = next((fx[n] for n in ("Palette", "Gradient", "Fade") if n in fx), 65 if not fx else 0)
        pal_id = next((pal[n] for n in ("* Colors Only", "* Color Gradient") if n in pal), 5)
        return fx_id, pal_id

    async def _music_lamp(self) -> Lamp | None:
        for l in await self.state.lamps():
            if l.entity_id == self.music_lamp and l.is_wled and l.alive:
                return l
        return None

    async def _music_paint(self) -> str:
        if self.night():
            return "ночь: не включаю"
        lamp = await self._music_lamp()
        if lamp is None or not self._music_palette:
            return "лампа недоступна"
        if not self._music_active:
            await self.state.capture(force=True)
            self._music_before_on = lamp.on
            self._music_active = True
        self.state.hold(3600)
        st = await self.state.wled_state(lamp.host)
        segs = [s.get("id", i) for i, s in enumerate((st or {}).get("seg") or [{}])]
        fx, pal = await self._effect_ids(lamp.host)
        cols = list(self._music_palette)
        while len(cols) < 3:
            cols.append(cols[len(cols) % len(self._music_palette)])
        bri = min((st or {}).get("bri") or MUSIC_BRI, 140) if lamp.on else MUSIC_BRI
        body = {"on": True, "bri": bri, "transition": int(MUSIC_TRANSITION_S * 10),
                "seg": [{"id": i, "col": cols, "fx": fx, "sx": 40, "ix": 128, "pal": pal} for i in segs]}
        ok = await self.state.wled_post(lamp.host, body)
        return "покрасил" if ok else "WLED не ответил"

    async def music_restore(self) -> str:
        if self._music_stop and self._music_stop is not asyncio.current_task():
            self._music_stop.cancel()
        self._music_stop = None
        if not self._music_active:
            return "нечего возвращать"
        self._music_active = False
        self._music_palette = []
        lamp = await self._music_lamp()
        self.state.release()
        if lamp is None:
            return "лампа недоступна"
        if not await self.state.apply(lamp, on=self._music_before_on, transition_s=2):
            await self.state.wled_post(lamp.host, {"on": self._music_before_on, "transition": 20})
        return "вернул прежний вид" if self._music_before_on else "вернул вид и выключил"

    async def music_light(self, action: str) -> dict:
        action = action.strip().casefold()
        if action in ("on", "вкл", "1", "включи"):
            self.music_on = True
            if self.brain:
                self.brain.set_pref("music_light", "1")
            return {"ok": True, "music_light": True, "note": "заработает, когда на телефоне заиграет трек с обложкой"}
        if action in ("off", "выкл", "0", "выключи"):
            self.music_on = False
            if self.brain:
                self.brain.set_pref("music_light", "0")
            back = await self.music_restore()
            return {"ok": True, "music_light": False, "lamp": back}
        if action == "status":
            return {"ok": True, "music_light": self.music_on, "playing": self._music_playing,
                    "painted": self._music_active}
        return {"ok": False, "error": "action: on, off или status"}

    # ── вход из Gemini и консоли ──

    async def call(self, name: str, args: dict[str, Any]) -> dict:
        if name == "goodnight":
            return await self.goodnight()
        if name == "music_light":
            return await self.music_light(str(args.get("action", "status")))
        return {"ok": False, "error": f"нет такого инструмента: {name}"}

    async def command(self, line: str) -> str | None:
        parts = line.split()
        if not parts or parts[0].lower() not in ("goodnight", "music_light", "clap", "lights"):
            return None
        cmd, args = parts[0].lower(), parts[1:]
        if cmd == "goodnight":
            return "→ " + (await self.goodnight())["note"]
        if cmd == "music_light":
            return str(await self.music_light(args[0] if args else "status"))
        if cmd == "clap":
            return await self.clap(int(args[0]) if args and args[0].isdigit() else 2)
        lamps = await self.state.lamps(fresh=True)
        return "\n".join(f"{l.name} ({l.entity_id}): {l.state}{' WLED ' + l.host if l.host else ''}"
                         f"{' [вид запомнен]' if l.entity_id in self.state.views else ''}" for l in lamps) \
            or "ламп нет"
