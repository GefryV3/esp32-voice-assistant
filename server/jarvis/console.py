"""Консоль разработчика: команды с клавиатуры → колонке. Пока нет мозга, это главный пульт."""

from __future__ import annotations

import asyncio
import hmac
import logging
import sys
from collections import deque

from websockets.asyncio.server import ServerConnection
from websockets.exceptions import ConnectionClosed

from . import protocol
from .devices import DeviceHub
from .protocol import Message

log = logging.getLogger(__name__)

ADMIN_PATH = "/admin"
RECENT_LOG: deque[str] = deque(maxlen=500)   # журнал для команды log: в аддоне его иначе не видно


class RecentLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            RECENT_LOG.append(self.format(record))
        except Exception:
            pass

HELP = """\
Команды (уходят первой подключённой колонке):
  voice idle|listening|thinking|speaking
  emotion happy|skeptic|roll|side|surprise|sorry [мс]
  say heard|said <текст>        say reply [light] <текст>        say none
  timer <сек> [название]        timer 0 — снять
  agenda <цветом> | <дальше>    agenda — убрать
  sleep <сек> [top|bottom]      через сколько засыпать и где часы во сне
  weather <cond -1..7> <темп>   night on|off        bright 0..255
  coin 0|1   dice 1..6   wheel <секторов> <выпало>   rps <мой 0..2> <исход 0..2>
  magic8 1..10   ttt X.O......   rem <число>   (игры и счётчик новый экран пока не рисует)
Звук:
  rec [сек]   echo [сек]        записать микрофон в WAV / записать и сразу проиграть
  beep [Гц] [мс]   play <wav>   тон / файл на динамике        stop — замолчать
  vol 0..100                    громкость        gain 0..30 — усиление микрофона, дБ
Голос (Gemini Live):
  tg <текст>                    спросить как через Telegram, ответ текстом здесь
  ask <вопрос>                  спросить без слова пробуждения, ответ голосом на колонке
  wake                          слушает ли слово пробуждения и как близко было за минуту
  last                          последний разговор: что расслышал и что ответил
  tts [имя]                     голос Gemini на лету (Puck, Fenrir, Zephyr, Charon, Orus…)
  affect on|off                 эмоциональный диалог Gemini: отвечает в тон интонации
  log [строк]                   последние строки журнала сервера (по умолчанию 40)
Рутины:
  routine briefing|home        утренний брифинг / приветствие дома сразу, без ожидания срока
Прошивка:
  ota [info|force]              обновить колонку по воздуху образом с сервера / версии / залить ту же
Телефон и компьютер:
  phone                         что известно о телефоне и компьютере
  ring [off]                    «где мой телефон»
  reply <текст>                 ответить на последнее уведомление с кнопкой «Ответить»
  decline [текст SMS]           сбросить звонок (и написать SMS)
  player toggle|next|prev       плеер на телефоне
  link <url>   pcurl <url>      открыть ссылку на телефоне / на компьютере
  notify <текст>   check        карточка на колонке / проверка перед сном
  devices             help                 quit"""


class CommandError(Exception):
    pass


def parse_command(line: str) -> Message:
    """Строка консоли → сообщение протокола. Ошибки — CommandError с понятным текстом."""
    parts = line.split()
    if not parts:
        raise CommandError("пустая команда")
    cmd, args = parts[0].lower(), parts[1:]

    def tail(n: int) -> str:
        """Всё после первых n слов строки — свободный текст."""
        rest = line.strip().split(maxsplit=n)
        return rest[n] if len(rest) > n else ""

    def number(s: str) -> int:
        try:
            return int(s)
        except ValueError:
            raise CommandError(f"{cmd}: «{s}» — не число") from None

    def ints(n_min: int, n_max: int | None = None) -> list[int]:
        n_max = n_min if n_max is None else n_max
        if not n_min <= len(args) <= n_max:
            raise CommandError(f"{cmd}: нужно аргументов {n_min}" + (f"…{n_max}" if n_max != n_min else ""))
        try:
            return [int(a) for a in args]
        except ValueError:
            raise CommandError(f"{cmd}: аргументы должны быть числами") from None

    try:
        match cmd:
            case "voice" if len(args) == 1:
                return protocol.voice(args[0])
            case "emotion" if 1 <= len(args) <= 2:
                ms = number(args[1]) if len(args) == 2 else 2600
                return protocol.emotion(args[0], ms)
            case "say" if args:
                if args[0] == "reply" and len(args) > 1 and args[1] == "light":
                    return protocol.caption("reply", tail(3), "light")
                return protocol.caption(args[0], tail(2))
            case "timer" if args:
                return protocol.timer(number(args[0]), tail(2))
            case "agenda":
                accent, _, text = tail(1).partition("|")
                return protocol.agenda(accent.strip(), text.strip())
            case "sleep" if 1 <= len(args) <= 2:
                return protocol.config(number(args[0]), args[1] if len(args) == 2 else None)
            case "coin":
                return protocol.coin(*ints(1))
            case "dice":
                return protocol.dice(*ints(1))
            case "wheel":
                return protocol.wheel(*ints(2))
            case "rps":
                return protocol.rps(*ints(2))
            case "magic8":
                return protocol.magic8(*ints(1))
            case "ttt" if len(args) == 1:
                return protocol.ttt(args[0].upper())
            case "weather":
                return protocol.weather(*ints(2))
            case "night" if len(args) == 1 and args[0] in ("on", "off"):
                return protocol.night(args[0] == "on")
            case "bright":
                return protocol.brightness(*ints(1))
            case "rem":
                return protocol.reminders(*ints(1))
    except ValueError as e:
        raise CommandError(str(e)) from None
    raise CommandError(f"не понял «{line.strip()}», help — список команд")


Reply = str | tuple[str, bytes]   # текст, или текст и WAV (rec, echo)


async def execute(hub: DeviceHub, line: str) -> Reply:
    """Выполнить строку консоли и вернуть ответ (общий код для клавиатуры и /admin)."""
    line = line.strip()
    if line == "help":
        return HELP
    if line == "log" or line.startswith("log "):
        n = line[4:].strip()
        return "\n".join(list(RECENT_LOG)[-(int(n) if n.isdigit() else 40):]) or "журнал пуст"
    if line == "devices":
        if not hub.devices:
            return "колонок нет"
        return "\n".join(f"{d.id}  fw={d.hello.fw}  {d.status}" for d in hub.devices.values())
    if hub.audio is not None:
        reply = await hub.audio.command(line)
        if reply is not None:
            return reply
    for module in (hub.voice, hub.ota, hub.telegram, getattr(hub, "presence", None), hub.routines,
                   getattr(hub, "skills", None), getattr(hub, "claude", None)):
        if module is not None:
            out = await module.command(line)
            if out is not None:
                return out
    if hub.bridge is not None:
        out = await hub.bridge.command(line)
        if out is not None:
            return out
    try:
        msg = parse_command(line)
    except CommandError as e:
        return str(e)
    dev = hub.get()
    if dev is None:
        return "колонок нет — команда никуда не ушла"
    await dev.send(msg)
    return f"→ {dev.id}: {protocol.encode(msg)}"


async def run_console(hub: DeviceHub) -> None:
    print(HELP)
    while True:
        try:
            line = await asyncio.to_thread(sys.stdin.readline)
        except (EOFError, KeyboardInterrupt):
            line = ""
        if not line:            # ввод закрыт — консоль больше не нужна, а сервер работает дальше
            log.info("консоль закрыта, сервер продолжает работать (Ctrl+C — остановить)")
            await asyncio.Event().wait()
        line = line.strip()
        if not line:
            continue
        if line in ("quit", "exit"):
            return
        reply = await execute(hub, line)
        print(reply[0] if isinstance(reply, tuple) else reply)


def add_admin_route(hub: DeviceHub, token: str) -> None:
    """Служебный вход ws://…/admin для tools/send.py: первый кадр — токен, дальше каждый
    текстовый кадр — строка консоли, в ответ — её вывод. Если команда сделала запись,
    перед текстом приходит бинарный кадр с WAV."""

    async def handle(ws: ServerConnection) -> None:
        try:
            first = await asyncio.wait_for(ws.recv(), protocol.HELLO_TIMEOUT_S)
        except (TimeoutError, ConnectionClosed):
            return
        if not isinstance(first, str) or not hmac.compare_digest(first, token):
            await ws.close(protocol.CLOSE_BAD_TOKEN, "bad token")
            return
        try:
            async for line in ws:
                if isinstance(line, str):
                    reply = await execute(hub, line)
                    if isinstance(reply, tuple):
                        text, wav = reply
                        await ws.send(wav)
                        reply = text
                    await ws.send(reply)
        except ConnectionClosed:
            pass

    hub.add_route(ADMIN_PATH, handle)
