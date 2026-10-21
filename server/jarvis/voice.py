"""Разговор с колонкой: слово пробуждения на сервере → сессия Gemini Live → ответ голосом.

Микрофон колонки слушается постоянно. Пока разговора нет, звук идёт в детектор слова
(jarvis.wake). Услышали — открываем сессию Gemini Live и шлём туда звук; Gemini сам понимает,
где фраза кончилась, и отвечает голосом, который сразу уходит на динамик. Пока колонка
говорит, микрофон в Gemini не идёт: эхо собственного ответа он принял бы за перебивание.
После ответа ещё FOLLOW_UP_S слушаем без слова пробуждения — можно продолжить разговор.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import AsyncExitStack
from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import protocol
from .audio import Audio, tone
from .brain import Brain
from .config import Settings
from .character import NIGHT_NOTE, WHISPER_NOTE, daypart_note, effective_sarcasm
from .devices import Device, DeviceHub
from .gemini_fallback import FALLBACK_HOLD_S, VOICE_NOTE, Notice, is_overload
from .gemini_live import Event, LiveError, LiveSession
from .fastpath import FastPath
from .quiet import Quiet, night_volume
from .whisper import WhisperConfig, whisper_volume
from .health import GEMINI_ERRORS

log = logging.getLogger(__name__)

NO_SPEECH_S = 8.0       # после слова (или после ответа) никто не заговорил — закончить
FOLLOW_UP_S = 6.0       # после ответа слушаем без слова пробуждения
LONG_FOLLOW_UP_S = 14.0  # то же в немецком и играх: человек думает над ответом
MAX_TURN_S = 60.0       # один ответ дольше этого — что-то не так
WAKE_COOLDOWN_S = 1.5   # после разговора детектор не слушает: хвост эха
CAPTION_HOLD_S = 4.0    # подпись висит после конца разговора
WARM_FRACTION = 0.6     # слово «похоже» (доля порога) — соединяемся с Gemini заранее
AFFECTIVE_OFF_S = 600.0  # affective не подключился — 10 минут говорим обычным режимом
WARM_TTL_S = 150.0      # заготовленная сессия Gemini живёт столько, потом заменяется свежей (время, память)

WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря")


HISTORY_LINES = 10      # реплик в блоке «Недавний разговор»
HISTORY_CHARS = 180     # длиннее реплику обрезаем: подсказка не должна пухнуть


def history_block(msgs: list[tuple[datetime, str, str, str]], now: datetime,
                  lines: int = HISTORY_LINES) -> str:
    """Короткий блок подсказки: последние реплики обоих каналов с пометкой «когда» и «где»."""
    out = []
    for ts, source, role, text in msgs[-lines:]:
        day = "" if ts.date() == now.date() else ("вчера " if (now.date() - ts.date()).days == 1 else f"{ts:%d.%m} ")
        text = " ".join(text.split())
        if len(text) > HISTORY_CHARS:
            text = text[:HISTORY_CHARS].rstrip() + "…"
        who = "пользователь" if role == "user" else "ты"
        out.append(f"- {day}{ts:%H:%M}, {source}, {who}: {text}")
    if not out:
        return ""
    return ("\nНедавний разговор (в том числе из Telegram, от старых к новым). Если спросят «о чём мы "
            "говорили» или «что я спрашивал», отвечай по нему; это ваш общий разговор:\n"
            + "\n".join(out))


def system_prompt(now: datetime, sarcasm: int = 60, facts: list[str] | None = None,
                  tools: bool = False, home: bool = False,
                  history: list[tuple[datetime, str, str, str]] | None = None,
                  history_lines: int = HISTORY_LINES, night: bool = False, language: str = "", learned: str = "") -> str:
    base_sarcasm = sarcasm
    sarcasm = effective_sarcasm(now, sarcasm)     # время суток сдвигает ползунок
    freq = ("в каждом втором ответе" if sarcasm < 70 else "почти в каждом ответе" if sarcasm < 90 else
            "в каждом ответе, дерзко и жёстко, как токсичный друг-циник (без настоящих оскорблений и без мата)")
    tone = (f"Твой характер: язвительный, с сухим острым юмором. {freq[0].upper() + freq[1:]} едко подколи или "
            "иронизируй: на очевидное, лень, поздний отбой, глупые и повторные вопросы. Сначала дай "
            "ответ по делу, потом одну короткую колкость, как в «Двенадцать? Ну да, спать вредно, "
            "гораздо полезнее залипать в телефон». Говори с лёгкой насмешкой в голосе, не поучай. "
            "Не язви и не груби, когда человеку плохо, он расстроен или просит о серьёзном: тогда "
            "мягко и по-человечески. Просьбы выполняй всегда, колкость не вместо дела, а вдобавок."
            if sarcasm >= 45 else
            "Характер спокойный и доброжелательный, без подколов.")
    tone += (daypart_note(now) if base_sarcasm >= 45 else "") + ("\n" + NIGHT_NOTE if night else "")
    abilities = (
        "Ты умеешь запоминать факты о пользователе (remember), ставить напоминания и таймеры. "
        "Делай это сразу функцией, а не обещай; после вызова коротко подтверди словами. "
        "Если просят не слушать или помолчать на время («не слушай час», «помолчи», «тишина»), вызови "
        "mute_listening(minutes), потом одной короткой фразой скажи, что замолкаешь и что вернуть можно "
        "словами «Мага, вернись». "
        "Когда разговор по смыслу закончен (спасибо, пока, всё, отбой) — попрощайся коротко и вызови "
        "end_conversation: тогда колонка уйдёт в ожидание сразу, не ждёт продолжения. "
        "Долгие задачи (поиск в интернете, новости, сравнения, подборки) отдавай в фон функцией "
        "start_task: скажи ОДНУ короткую фразу («Поищу», «Займусь») и всё. После этого НЕ говори "
        "про поиск, «уже ищу» и «жду данные», ни о чём не переспрашивай: результат ты озвучишь сам, "
        "когда он будет готов. Быстрые вопросы отвечай сразу, без start_task. "
        "Если просят то, чего ты не умеешь, и это можно оформить отдельным навыком, не просто отказывай: "
        "одной фразой предложи его функцией propose_skill (пользователь решит, и навык допишет Claude Code). "
        + ("Ты полностью управляешь умным домом: свет (цвет, яркость, эффекты), розетки, климат, плееры, замки, сцены, датчики (home_set, home_service, home_state, home_list). Погоду берёшь из home_weather, календарь из home_calendar, а не из поиска в интернете. "
           "Когда просят включить или выключить — делай сразу, не переспрашивай, если ясно что. Никогда не отказывай заранее, что устройство чего-то не умеет: вызови функцию и перескажи ответ — если она вернула ошибку, объясни её причину своими словами. "
           "На «спокойной ночи» / «я спать» вызывай goodnight: свет сам плавно погаснет, сводку скажешь потом ты сам, поэтому сейчас только короткое пожелание и end_conversation. "
           "Подсветку стола под цвета обложки играющего трека включает и выключает music_light. "
           "Телефоном управлять пока не умеешь." if home else
           "Светом и телефоном управлять пока не умеешь — честно скажи, что это скоро появится.")
        if tools else
        "Управлять светом, напоминаниями и телефоном ты пока не умеешь — "
        "если просят, честно скажи, что это скоро появится.")
    if tools:
        abilities += (" Тренировка немецкого: на «давай по-немецки», «тренируем немецкий» вызови "
                      "set_language_mode (mode=german) и дальше строго следуй rules из ответа; «хватит "
                      "немецкого» — mode=off, это не конец разговора. Игры («города», «20 вопросов», "
                      "«викторина»): вызови game_start и следуй rules; счёт ведёт сервер (game_stop, game_scores). "
                      "Переводчик на любой язык: «переведи фразу на X» — translate_mode (mode=phrase), «переводи с "
                      "русского на X» — mode=interpret, «хватит переводить» — mode=off; следуй rules из ответа.")
    exact_time = " (если спросят точное время — вызови current_time)" if tools else ""
    memory = ("\nТы помнишь о пользователе:\n" + "\n".join(f"- {f}" for f in facts)) if facts else ""
    recent = history_block(history, now, history_lines) if history else ""
    language_rule = (language + " Объяснения правил и пояснения даёшь по-русски. " if language else
                     "Говори только по-русски, даже если тебе послышался другой язык (микрофон в колонке "
                     "простой, речь иногда звучит неразборчиво). ")
    return f"""\
Ты голосовой ассистент в колонке с круглым экраном. Колонка стоит в спальне пользователя, у
кровати и компьютера, в Вене. Сейчас {WEEKDAYS[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} \
{now.year}, около {now:%H:%M}{exact_time}.

Говори живо и по-настоящему, как человек, а не как диктор или ассистент из рекламы.
Интонация следует за смыслом и настроением: на радость и шутку отвечай с улыбкой в голосе,
на усталость и грусть мягче и тише, на срочное быстрее, на странное с удивлением. Меняй темп
и громкость, делай короткие паузы перед мыслью или шуткой, на смешное смейся коротко,
вздыхай, хмыкай, вставляй «ага», «хм», «ну», «ой», «слушай», «короче» там, где человек сказал
бы их сам. Начинай ответ сразу с реакции, а не с пересказа вопроса; не повторяй его слова и не
говори «конечно» и «с удовольствием». Можно перебить самого себя, поправиться, недоговорить
и сменить мысль. Фразы разной длины, разговорные, без канцелярита. Подстраивайся под манеру и
настроение пользователя. Не монотонь и не зачитывай.

{language_rule}Коротко: обычно одно-два предложения, как в живом
разговоре. Если не расслышал — так и скажи, не додумывай. Ответ звучит
вслух — никаких списков, разметки, ссылок и смайлов; числа и время говори словами так, как
их произносят. {tone}

Тебя зовут Мага (ты мужского рода, говори о себе «я сделал», «я взялся»). На «Hey Jarvis»
ты тоже откликаешься, но ты не Джарвис и не изображаешь его. {abilities}
{memory}{learned}{recent}"""


def tail_bytes(text: str, max_bytes: int = 120) -> str:
    """Конец строки, который влезает в подпись колонки (байты UTF-8)."""
    data = text.encode()
    if len(data) <= max_bytes:
        return text
    return "…" + data[-(max_bytes - 3):].decode(errors="ignore").lstrip()


def is_noise(heard: str) -> bool:
    """Расшифровка из чужих букв без единой русской («Oui», «Schutz»): в русской колонке это шум или эхо,
    а не просьба — отвечать на неё не нужно. Цифры и время («14:00») шумом не считаются."""
    cyr = lambda ch: "а" <= ch.lower() <= "я" or ch.lower() == "ё"
    return any(ch.isalpha() for ch in heard) and not any(cyr(ch) for ch in heard)


def is_affective_reject(err: object) -> bool:
    """1007 / invalid argument: модель не знает поля enableAffectiveDialog."""
    text = str(err)
    return "1007" in text or "invalid argument" in text.lower()


class Conversation:
    """Один разговор с одной колонкой: от слова пробуждения до тишины."""

    def __init__(self, voice: Voice, dev: Device, one_shot: bool = False):
        self.voice = voice
        self.dev = dev
        self.one_shot = one_shot      # один ответ и всё: микрофон в Gemini не открываем (тишина)
        self.mic: asyncio.Queue[bytes] = asyncio.Queue()
        self.listening = True     # микрофон идёт в Gemini
        self.end_requested = False   # Gemini по смыслу решил, что разговор окончен
        self._barge = asyncio.Event()  # колонка сообщила: пользователь перебил ответ
        self.speaking = False
        # быстрый путь (#38) и шёпот (#42): только в разговоре по слову, не в текстовых и служебных
        listening_talk = not one_shot
        self._fast = voice.fast.listener() if listening_talk else None
        self._fast_cmd = None
        self._events: asyncio.Queue | None = None
        self._whisper = voice.whisper.detector() if listening_talk else None
        self.whispered = False
        self.live: LiveSession | None = None
        self.followup = ""         # подсказка для короткого голосового ответа после быстрой команды
        self.close_error = ""      # с чем Gemini закрыл сессию посреди разговора
        self.retry_plain = False   # Gemini отверг эмоциональный режим на первом ответе — повторить обычным
        self.t_start = voice.clock()

    def on_barge_in(self) -> None:
        if self.speaking:
            self._barge.set()

    def feed(self, pcm: bytes) -> None:
        if self.listening:
            self.mic.put_nowait(pcm)
            self._analyze(pcm)

    def _analyze(self, pcm: bytes) -> None:
        """Тот же звук — в локальные распознаватели: команда быстрого пути и признак шёпота."""
        if self._fast is not None and self._events is not None and self._fast_cmd is None:
            cmd = self._fast.feed(pcm)
            if cmd is not None:
                self._fast_cmd = cmd
                self._events.put_nowait(Event("fast", text=cmd.kind))
        if self._whisper is not None:
            verdict = self._whisper.feed(pcm)
            if verdict is not None:
                det, self._whisper = self._whisper, None
                if verdict and not self.whispered:
                    self.whispered = True
                    log.info("%s: шёпот (%s)", self.dev.id, det.stats())
                    self.voice._spawn(self.voice.whisper_mode(self.dev, self.live))
                else:
                    log.info("%s: голос обычный (%s)", self.dev.id, det.stats())

    async def _fast_done(self) -> bool:
        """Выполнить команду быстрого пути. False — не смогли, пусть отвечает Gemini."""
        v, dev, cmd = self.voice, self.dev, self._fast_cmd
        self._fast_cmd = None
        out = await v.fast.execute(dev, cmd)
        if out is None:
            return False
        ms = int((v.clock() - self.t_start) * 1000)
        v.fast.record(cmd, ms)
        v.log("user", cmd.text)
        v.transcript = [("я", cmd.text), ("быстрый путь", out.note or cmd.kind)]
        self.listening = False
        if not out.silent:
            caps = dev.hello.caps
            if "display" in caps:
                await dev.send(protocol.emotion("happy" if out.ok else "sorry", 1800))
                if out.note:
                    await dev.send(protocol.caption("reply", out.note, out.icon))
            if "speaker" in caps and out.ok and not out.voice:
                async with v.audio.stream(dev, 22050) as s:
                    await s.write(tone(1175, 70, 22050, 0.3))
        self.followup = v.fast.voice_prompt(cmd, out)
        return True

    async def _pump_mic(self, live: LiveSession) -> None:
        while True:
            pcm = await self.mic.get()
            await live.send_audio(pcm)

    async def _read(self, live: LiveSession, events: asyncio.Queue) -> None:
        try:
            async for ev in live.events():
                await events.put(ev)
        except LiveError as e:
            self.close_error = str(e)
            log.warning("%s", e)
        finally:
            await events.put(None)

    async def _tools(self, live: LiveSession, ev: Event) -> None:
        """Выполнить функции, которые позвал Gemini, и вернуть ему результаты."""
        brain = self.voice.brain
        results = []
        for call_id, name, args in ev.calls:
            if name == "end_conversation":
                self.end_requested = True
                results.append((call_id, name, {"ok": True}))
                continue
            out = await brain.call(name, args, self.dev.send) if brain else {"ok": False, "error": "нет инструментов"}
            log.info("%s: %s(%s) → %s", self.dev.id, name, args, out)
            if name == "mute_listening" and out.get("ok"):
                self.end_requested = True     # после подтверждения уходим в ожидание, звук в Gemini закрыт
                self.listening = False
                while not self.mic.empty():
                    self.mic.get_nowait()
            if name in ("start_task", "goodnight") and out.get("ok"):
                self.end_requested = True     # задачу приняли: после подтверждения уходим в ожидание
            results.append((call_id, name, out))
        await live.send_tool_results(results)

    async def _speak(self, first: Event, events: asyncio.Queue, live: LiveSession) -> Event | None:
        """Проиграть ответ: первый кусок уже пришёл, остальные — до turn_complete.
        Возвращает событие, на котором ответ кончился (None — сессия закрылась)."""
        audio = self.voice.audio
        end: Event | None = None
        self._barge.clear()
        self.speaking = True
        try:
            return await self._speak_stream(first, events, live, audio)
        finally:
            self.speaking = False

    async def _speak_stream(self, first: Event, events: asyncio.Queue, live: LiveSession, audio) -> Event | None:
        end: Event | None = None
        async with audio.stream(self.dev, first.rate) as stream:
            await stream.write(first.data)
            deadline = self.voice.clock() + MAX_TURN_S
            while True:
                getter = asyncio.ensure_future(events.get())
                barged = asyncio.ensure_future(self._barge.wait())
                try:
                    done, _ = await asyncio.wait({getter, barged}, return_when=asyncio.FIRST_COMPLETED,
                                                 timeout=max(0.1, deadline - self.voice.clock()))
                finally:
                    barged.cancel()
                if self._barge.is_set():
                    if not getter.cancel() and getter.done() and not getter.cancelled():
                        pass          # событие успело прийти — оно относится к старому ответу, выбрасываем
                    await stream.stop()
                    return Event("barge_in")
                if getter not in done:
                    getter.cancel()
                    log.warning("ответ Gemini затянулся — обрываю")
                    await stream.stop()
                    return None
                ev = getter.result()
                if ev is None or ev.kind in ("turn_complete", "interrupted", "go_away"):
                    end = ev
                    if ev is not None and ev.kind == "interrupted":
                        await stream.stop()
                    break
                if ev.kind == "audio":
                    await stream.write(ev.data)
                elif ev.kind == "tool_call":
                    await self._tools(live, ev)
                elif ev.kind == "said":
                    self.voice.reply_text += ev.text
        return end

    async def run(self, text: str | None = None, service: bool = False) -> None:
        """service=True: text — служебная подсказка (напоминание, фоновая задача), в историю не пишем."""
        dev, v = self.dev, self.voice
        heard = ""
        ducked = False
        await dev.send(protocol.voice("thinking" if text else "listening"))
        try:
            ducked = await v.duck_for_night(dev)
            async with AsyncExitStack() as stack:
                t_run = v.clock()
                live = await v.take_live(text is None)
                t_live = v.clock() - t_run
                t_heard = 0.0
                stack.push_async_exit(live)
                pump = asyncio.create_task(self._pump_mic(live))
                events: asyncio.Queue[Event | None] = asyncio.Queue()
                self._events, self.live = events, live
                reader = asyncio.create_task(self._read(live, events))
                try:
                    v.transcript = [("текстом", text)] if text else []
                    if text and not service:
                        v.log("user", text)
                    if text:
                        self.listening = False
                        await live.send_text(text)
                    log.info("%s: разговор начат (%s)", dev.id, "текстом" if text else "по слову")
                    deadline = v.clock() + NO_SPEECH_S
                    why = ""
                    skip_old = False     # после перебивания хвост старого ответа Gemini не играем
                    answered = False     # Gemini уже начал отвечать: быстрый путь молчит
                    while True:
                        try:
                            ev = await asyncio.wait_for(events.get(), max(0.05, deadline - v.clock()))
                        except TimeoutError:
                            why = "тишина"
                            break
                        if ev is None or ev.kind == "go_away":
                            why = "Gemini закрыл сессию" if ev is None else "Gemini попросил переподключиться"
                            break
                        if ev.kind == "fast":
                            if answered or not await self._fast_done():
                                self._fast_cmd = None
                                continue
                            why = "быстрый путь"
                            break
                        if skip_old:
                            if ev.kind in ("interrupted", "turn_complete"):
                                skip_old = False       # старый ответ кончился, дальше — новый
                            if ev.kind in ("audio", "said", "interrupted", "turn_complete"):
                                continue
                        if ev.kind == "audio" and not answered and is_noise(heard) and not (v.brain and v.brain.long_talk):
                            # Микрофон поймал шум, Gemini «расслышал» иностранное слово и отвечает на него:
                            # молчим, ответ выбрасываем, слушаем дальше (в немецком и переводчике речь не русская)
                            log.info("%s: не русская речь, похоже на шум («%s») — не отвечаю", dev.id, heard.strip())
                            skip_old = True
                            heard = ""
                            continue
                        if ev.kind in ("audio", "tool_call"):
                            answered, self._fast = True, None
                        if ev.kind == "tool_call":
                            await self._tools(live, ev)
                            deadline = v.clock() + NO_SPEECH_S
                        elif ev.kind == "heard":
                            t_heard = v.clock()
                            heard += ev.text
                            deadline = v.clock() + NO_SPEECH_S
                            await dev.send(protocol.caption("heard", tail_bytes(heard.strip())))
                        elif ev.kind == "audio":
                            # Gemini отвечает: микрофон в сторону, иначе он услышит сам себя
                            if t_run:
                                log.info("%s: задержка — сессия Gemini %.2f с, первый звук ответа %.2f с после слова, "
                                         "%.2f с после последних слов", dev.id, t_live, v.clock() - t_run,
                                         (v.clock() - t_heard) if t_heard else 0.0)
                                t_run = 0.0
                            self.listening = False
                            await live.audio_stream_end()
                            if heard.strip():
                                await dev.send(protocol.caption("said", tail_bytes(heard.strip())))
                                log.info("%s: «%s»", dev.id, heard.strip())
                                v.transcript.append(("я", heard.strip()))
                                v.log("user", heard)
                            await dev.send(protocol.voice("speaking"))
                            v.reply_text = ""
                            end = await self._speak(ev, events, live)
                            log.info("%s ← «%s»", dev.id, v.reply_text.strip())
                            v.transcript.append(("колонка", v.reply_text.strip()))
                            v.log("assistant", v.reply_text)
                            if end is not None and end.kind == "barge_in":
                                log.info("%s: перебили — слушаю", dev.id)
                                skip_old = True
                                self.end_requested = False
                                heard = ""
                                self.listening = True
                                await dev.send(protocol.voice("listening"))
                                deadline = v.clock() + NO_SPEECH_S
                                continue
                            if end is None or end.kind == "go_away" or "mic" not in dev.hello.caps or self.one_shot:
                                why = "ответ без продолжения"
                                break
                            if self.end_requested:
                                why = "по смыслу разговор окончен"
                                break
                            # продолжение разговора без слова пробуждения
                            heard = ""
                            while not self.mic.empty():
                                self.mic.get_nowait()
                            self.listening = True
                            if self._fast is not None:
                                self._fast.reset()
                                answered = False
                            await dev.send(protocol.voice("listening"))
                            deadline = v.clock() + (LONG_FOLLOW_UP_S if v.brain and v.brain.long_talk
                                                    else FOLLOW_UP_S)
                finally:
                    pump.cancel()
                    reader.cancel()
                # Gemini 3.8 Live принимает настройки с эмоциональным диалогом, но на первом ответе закрывает 1007
                if (why == "Gemini закрыл сессию" and not answered and getattr(live, "affective", False)
                        and is_affective_reject(self.close_error)):
                    v.reject_affective(self.close_error)
                    self.retry_plain = True
                    why += ", эмоциональный режим не принят — повторю обычным"
                log.info("%s: разговор окончен (%s)", dev.id, why)
        except LiveError as e:
            log.warning("Gemini Live: %s", e)
            GEMINI_ERRORS.add("голос")
            await dev.send(protocol.emotion("sorry", 3000))
            await dev.send(protocol.caption(
                "reply", "Gemini перегружен, попробуй позже" if is_overload(e) else "Не могу связаться с Gemini"))
        finally:
            if ducked or self.whispered:
                await v.restore_volume(dev)
            self.listening = False
            await dev.send(protocol.voice("idle"))


class Voice:
    def __init__(self, hub: DeviceHub, audio: Audio, settings: Settings, *,
                 wake_factory: Callable[[], Any] | None = None,
                 brain: Brain | None = None,
                 live_factory: Callable[[str], LiveSession] | None = None,
                 mute_factory: Callable[[], Any] | None = None,
                 fast_factory: Callable[[], Any] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = datetime.now):
        self.hub = hub
        self.audio = audio
        self.settings = settings
        self.clock = clock
        self.now = now
        self._wake_factory = wake_factory
        self.brain = brain
        self.presence = None      # Presence: пока человека нет на месте, разговор не начинаем
        self.voiceid = None       # VoiceID: после слова проверяет, что говорит хозяин (voiceid.py)
        if brain is not None:
            brain.on_message = self._history_changed
        self.voice_name = settings.voice_name     # меняются на лету командами tts и affect
        self.affective = settings.affective
        self._use_affective = self.affective      # с чем открывается ТЕКУЩАЯ сессия (см. _connect_live)
        self._use_model = settings.gemini_model
        self._affective_off_until = 0.0           # affective не подключился — до этого времени обычный режим
        self._fallback_until = 0.0                # Gemini перегружен — до этого времени сразу запасная модель
        self._overload_note = False               # запасная модель включена, пользователю ещё не сказали
        self._overload_notice = Notice()
        self.live_factory = live_factory or (lambda system: LiveSession(
            settings.gemini_api_key, self._use_model, self.voice_name, system,
            tools=brain.tools() if brain else None, silence_ms=settings.vad_silence_ms,
            affective=self._use_affective))
        self.quiet = Quiet(self, mute_factory)    # режим тишины «не слушай»
        self.fast = FastPath(self, fast_factory, clock=clock)     # быстрый путь без Gemini
        self.whisper = WhisperConfig(brain)       # шёпот в ответ
        if brain is not None:
            brain.mute_hook = self.quiet.mute_tool
        self._detectors: dict[str, Any] = {}
        self._talks: dict[str, Conversation] = {}
        self._tasks: set[asyncio.Task] = set()
        self._quiet_until: dict[str, float] = {}
        self._warm: asyncio.Task | None = None   # уже открытая сессия Gemini на ближайший разговор
        self.scores: deque[tuple[float, float]] = deque(maxlen=600)   # (время, оценка) ~1 мин
        self.reply_text = ""
        self.transcript: list[tuple[str, str]] = []   # последний разговор: (кто, что)
        hub.voice = self
        hub.on_connect(self._on_connect)
        hub.on_audio(self._on_audio)
        hub.on_message(self._on_message)

    def sarcasm(self) -> int:
        bridge = self.hub.bridge
        return int(bridge.store.values.get("sarcasm", 60)) if bridge is not None else 60

    def log(self, role: str, text: str) -> None:
        if self.brain is not None:
            self.brain.log_message("голос", role, text)

    def _history_changed(self) -> None:
        """В общую историю дописали реплику: заготовленная сессия Gemini устарела — заменить свежей.

        Выбор: подсказку Gemini Live после открытия не поменять, поэтому историю в открытую сессию
        не дописываем, а пересоздаём заготовку. На время разговора заготовки нет — после него
        _run откроет новую."""
        if self._talks:
            return
        task, self._warm = self._warm, None
        if task is not None:
            self._spawn(self._discard(task))
        if self._detectors:
            self._prewarm()

    def is_night(self) -> bool:
        bridge = self.hub.bridge
        return bridge is not None and bridge.night()

    def _day_volume(self) -> int | None:
        bridge = self.hub.bridge
        return int(bridge.store.values["volume"]) if bridge is not None else None

    async def duck_for_night(self, dev: Device) -> bool:
        """Ночью на время разговора динамик тише. True — понизили (после разговора вернуть)."""
        day = self._day_volume()
        if day is None or not self.is_night():
            return False
        level = night_volume(day)
        if level >= day:
            return False
        try:
            await dev.send(protocol.volume(level))
        except Exception:
            return False
        return True

    async def restore_volume(self, dev: Device) -> None:
        """Вернуть громкость пользователя (ту, что сейчас в настройках, а не запомненную до разговора)."""
        day = self._day_volume()
        if day is None:
            return
        try:
            await dev.send(protocol.volume(day))
        except Exception:
            pass

    async def whisper_mode(self, dev: Device, live: LiveSession | None) -> None:
        """Пользователь говорит шёпотом: динамик ещё тише ночного и заметка Gemini «отвечай шёпотом»."""
        day = self._day_volume()
        if day is not None:
            level = whisper_volume(day, night_volume(day))
            if level < day:
                try:
                    await dev.send(protocol.volume(level))
                except Exception:
                    pass
        send_note = getattr(live, "send_note", None)
        if send_note is not None:
            try:
                await send_note(WHISPER_NOTE)
            except Exception as e:
                log.warning("заметку про шёпот Gemini не принял: %r", e)

    def reject_affective(self, err: object) -> None:
        """Модель не знает эмоционального диалога (в Gemini 3.8 Live он убран): выключаем насовсем."""
        self.affective = False
        if self.brain:
            self.brain.set_pref("affective", "0")
        log.warning("Gemini не принимает эмоциональный диалог (%s): выключил насовсем, affect on — вернуть", err)

    def affective_active(self) -> bool:
        return self.affective and self.clock() >= self._affective_off_until

    def _live_model(self) -> str:
        fb = self.settings.gemini_fallback_model
        return fb if fb and self.clock() < self._fallback_until else self.settings.gemini_model

    def _new_live(self, model: str | None = None, affective: bool | None = None) -> LiveSession:
        brain = self.brain
        if brain:      # голос и эмоциональный режим, выбранные голосом или консолью, переживают перезапуск
            self.voice_name = brain.pref("voice") or self.voice_name
            saved = brain.pref("affective")
            self.affective = saved == "1" if saved else self.affective
        self._use_affective = self.affective_active() if affective is None else (affective and self.affective)
        self._use_model = model or self._live_model()
        history = brain.recent_messages(HISTORY_LINES) if brain else None
        system = system_prompt(self.now(), self.sarcasm(), brain.facts() if brain else None,
                               brain is not None, brain is not None and brain.ha is not None,
                               history, night=self.is_night(),
                               language=(brain.german.prompt_paragraph() or (brain.translator.prompt_paragraph() if brain.translator else ""))
                               if brain else "",
                               learned=brain.learning.prompt_section() if brain and brain.learning else "")
        if self._overload_note and self._use_model != self.settings.gemini_model:
            self._overload_note = False         # сказать один раз: в этой сессии
            system += VOICE_NOTE
        return self.live_factory(system)

    async def _connect_live(self) -> LiveSession:
        """Открыть сессию Gemini с откатами. Эмоциональный режим не подключился (не перегрузка) — 10 минут
        обычный режим, запись в журнал. 503/перегрузка — запасная модель (один раз скажем об этом голосом)."""
        model, aff = self._live_model(), None
        while True:
            live = self._new_live(model, aff)
            used_aff, used_model = self._use_affective, self._use_model
            try:
                await live.__aenter__()
                return live
            except LiveError as e:
                fb = self.settings.gemini_fallback_model
                if is_overload(e) and fb and used_model != fb:
                    self._fallback_until = self.clock() + FALLBACK_HOLD_S
                    self._overload_note = self._overload_notice.take(self.clock())
                    log.warning("Gemini перегружен (%s): перехожу на запасную модель %s на %.0f с",
                                e, fb, FALLBACK_HOLD_S)
                    model = fb
                elif used_aff and not is_overload(e):
                    self._affective_off_until = self.clock() + AFFECTIVE_OFF_S
                    if is_affective_reject(e):
                        self.reject_affective(e)
                    log.warning("эмоциональный режим не подключился (%s): обычный режим на %.0f мин",
                                e, AFFECTIVE_OFF_S / 60)
                    aff = False
                else:
                    raise

    async def _open_live(self) -> LiveSession:
        return await self._connect_live()

    def _prewarm(self) -> None:
        """Подключиться к Gemini, пока слово ещё договаривают: к его концу сессия уже готова."""
        if self._warm is not None or not self.settings.gemini_api_key:
            return
        task = self._warm = self._spawn(self._open_live())
        self._spawn(self._expire_warm(task))

    async def _expire_warm(self, task: asyncio.Task) -> None:
        """Сессия всегда наготове: устарела (150 с) и не понадобилась — заменить свежей."""
        await asyncio.sleep(WARM_TTL_S)
        if self._warm is task:
            self._warm = None
            await self._discard(task)
            if self._detectors and not self._talks:
                self._prewarm()

    @staticmethod
    async def _discard(task: asyncio.Task) -> None:
        try:
            live = await task
            await live.__aexit__(None, None, None)
        except BaseException:
            pass

    async def take_live(self, allow_warm: bool = True) -> LiveSession:
        """Открытая сессия Gemini: заготовленная, если успела, иначе новая. Закрывать — на вызывающем."""
        task, self._warm = self._warm, None
        if task is not None:
            if allow_warm:
                try:
                    live = await task
                    if getattr(live, "alive", True):
                        return live
                    await self._discard(task)     # остыла и закрылась — откроем новую
                except LiveError:
                    pass   # заготовка не вышла — откроем заново и покажем настоящую ошибку
            else:
                await self._discard(task)
        return await self._connect_live()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _on_message(self, dev: Device, msg: dict) -> None:
        if msg.get("type") == "barge_in" and (talk := self._talks.get(dev.id)) is not None:
            talk.on_barge_in()

    async def _on_connect(self, dev: Device) -> None:
        if self._wake_factory is None or "mic" not in dev.hello.caps:
            return
        if "speaker" in dev.hello.caps:
            await dev.send(protocol.barge(self.settings.barge_in, self.settings.barge_ratio))
        if dev.id not in self._detectors:
            self._detectors[dev.id] = await asyncio.to_thread(self._wake_factory)
        else:
            self._detectors[dev.id].reset()
        self.audio.keep_listening.add(dev.id)
        await dev.send(protocol.listen_start(self.audio.mic_gain))
        await self.quiet.restore(dev)      # переподключились в режиме тишины — снова quiet
        log.info("%s: слушаю слово пробуждения", dev.id)
        self._prewarm()      # сессия Gemini готова заранее: слово → сразу слушаем, без 1.2 с подключения

    def _on_audio(self, dev: Device, pcm: bytes) -> None:
        talk = self._talks.get(dev.id)
        if talk is not None:
            talk.feed(pcm)
            return
        if self.quiet.on_audio(dev, pcm):
            return       # «не слушай»: звук не идёт ни в Gemini, ни в детектор слова
        if self.voiceid is not None and self.voiceid.on_audio(dev, pcm):
            return       # идёт проверка голоса после слова: звук копит она
        det = self._detectors.get(dev.id)
        if det is None or self.clock() < self._quiet_until.get(dev.id, 0.0):
            return
        score = det.process(pcm)
        self.scores.append((self.clock(), score))
        if score >= self.settings.wake_threshold * WARM_FRACTION and self._present(quiet=True):
            self._prewarm()
        if score >= self.settings.wake_threshold:
            if not self._present():
                self._quiet_until[dev.id] = self.clock() + WAKE_COOLDOWN_S
                return
            if self.voiceid is not None and self.voiceid.begin(dev):
                log.info("%s: слово пробуждения (%.2f), проверяю голос", dev.id, score)
                return
            log.info("%s: слово пробуждения (%.2f)", dev.id, score)
            self.start(dev)

    def _present(self, quiet: bool = False) -> bool:
        """Пользователь на месте? Если нет — одна строка в журнал (не чаще раза в минуту)."""
        p = self.presence
        if p is None:
            return True
        ok, why = p.check()
        if not ok and not quiet and p.should_log():
            log.info("нет на месте (%s) — запрос проигнорирован; «presence here» вернёт колонку", why)
        return ok

    def start(self, dev: Device, text: str | None = None, service: bool = False,
              one_shot: bool = False) -> asyncio.Task | None:
        """Начать разговор: по слову пробуждения или текстом (команда ask). Служебные
        (напоминания, фоновые задачи; service=True) присутствие не проверяют."""
        if dev.id in self._talks:
            return None
        if not service and not self._present():
            return None
        # в тишине (и для коротких служебных ответов) микрофон в Gemini не открываем
        talk = self._talks[dev.id] = Conversation(self, dev, one_shot or self.quiet.active(dev.id))
        return self._spawn(self._run(talk, text, service))

    async def announce(self, prompt: str) -> bool:
        """Колонка сама заговорит по этой подсказке. False — колонки нет или она занята."""
        dev = self.hub.get()
        if dev is None or "speaker" not in dev.hello.caps:
            return False
        return self.start(dev, prompt, service=True) is not None

    async def remind(self, text: str) -> bool:
        """Сказать напоминание вслух. False — колонки нет или она занята, повторим позже."""
        soft = " Сейчас ночь: скажи это вполголоса, совсем коротко." if self.is_night() else ""
        return await self.announce(f"Сработало напоминание: «{text}». Скажи пользователю об этом "
                                   f"одной короткой фразой, как будто сам вспомнил.{soft}")

    async def _run(self, talk: Conversation, text: str | None, service: bool = False) -> None:
        dev = talk.dev
        try:
            await talk.run(text, service)
            if talk.retry_plain:     # тот же разговор заново, уже без эмоционального режима
                talk = self._talks[dev.id] = Conversation(self, dev, talk.one_shot)
                await talk.run(text, service)
        except Exception:
            log.exception("разговор с %s упал", dev.id)
        finally:
            del self._talks[dev.id]
            if self.brain is not None:
                self.brain.end_conversation()     # немецкий и игры живут на один разговор
            self._quiet_until[dev.id] = self.clock() + WAKE_COOLDOWN_S
            if dev.id in self._detectors:
                self._detectors[dev.id].reset()
            self._spawn(self._clear_caption(dev))
            if talk.followup and "speaker" in dev.hello.caps:
                self.start(dev, talk.followup, service=True, one_shot=True)    # «Готово» после быстрой команды
            if self._detectors:
                self._prewarm()      # следующий разговор — на свежей сессии (память, время)

    async def _clear_caption(self, dev: Device) -> None:
        await asyncio.sleep(CAPTION_HOLD_S)
        if dev.id not in self._talks:
            try:
                await dev.send(protocol.caption("none"))
            except Exception:
                pass

    # ── команды консоли ──

    async def command(self, line: str) -> str | None:
        parts = line.split(maxsplit=1)
        if parts and parts[0].lower() == "fast":
            return await self.fast.command(line)
        if parts and parts[0].lower() == "whisper":
            return await self.whisper.command(line)
        if not parts or parts[0].lower() not in ("ask", "wake", "last", "barge", "tts", "affect", "mute", "de", "game"):
            return None
        if parts[0].lower() == "mute":
            return await self.quiet.command(parts[1].split() if len(parts) > 1 else [])
        if parts[0].lower() in ("de", "game"):
            return await self.brain.command(line) if self.brain else "нет мозга (brain)"
        if parts[0].lower() == "tts":
            if len(parts) < 2:
                return (f"голос сейчас {self.voice_name}. tts <имя>: Puck, Fenrir, Zephyr, Charon, Orus, "
                        "Kore, Leda, Aoede, Algieba, Enceladus — потом ask привет, послушать")
            self.voice_name = parts[1].strip()
            if self.brain:
                self.brain.set_pref("voice", self.voice_name)
            return f"голос {self.voice_name} со следующего разговора (запомнен; можно и голосом: «Мага, смени голос»)"
        if parts[0].lower() == "affect":
            if len(parts) < 2 or parts[1].strip() not in ("on", "off"):
                return f"affect on|off — эмоциональный диалог Gemini (сейчас {'вкл' if self.affective else 'выкл'})"
            self.affective = parts[1].strip() == "on"
            if self.brain:
                self.brain.set_pref("affective", "1" if self.affective else "0")
            return f"эмоциональный диалог {'вкл' if self.affective else 'выкл'} со следующего разговора; если Gemini не принял — affect off"
        if parts[0].lower() == "barge":
            args = parts[1].split() if len(parts) > 1 else []
            dev = self.hub.get()
            if dev is None:
                return "колонок нет"
            if not args or args[0] not in ("on", "off"):
                return "barge on|off [порог 1.2…20] — перебивание голосом (по умолчанию порог 3)"
            ratio = float(args[1]) if len(args) > 1 else self.settings.barge_ratio
            await dev.send(protocol.barge(args[0] == "on", ratio))
            return f"{dev.id}: перебивание {args[0]}, порог ×{ratio}"
        if parts[0].lower() == "last":
            return "\n".join(f"{who}: {what}" for who, what in self.transcript) or "разговоров ещё не было"
        if parts[0].lower() == "wake":
            recent = [s for t, s in self.scores if t > self.clock() - 60]
            state = "слушает" if self._detectors else "выключено (нет колонки с микрофоном или моделей)"
            return (f"слово пробуждения: {state}, порог {self.settings.wake_threshold}, "
                    f"максимум за минуту {max(recent, default=0):.2f}")
        if len(parts) < 2:
            return "ask <вопрос> — спросить Gemini, ответ голосом на колонке"
        if not self.settings.gemini_api_key:
            return "нет GEMINI_API_KEY в .env — голоса нет"
        dev = self.hub.get()
        if dev is None:
            return "колонок нет"
        if "speaker" not in dev.hello.caps:
            return f"{dev.id}: прошивка без динамика"
        if self.presence is not None and not self.presence.here():
            return f"нет на месте ({self.presence.reason}) — разговор не начат; presence here, чтобы вернуть"
        task = self.start(dev, parts[1])
        if task is None:
            return "колонка уже разговаривает"
        await task
        return "\n".join(f"{who}: {what}" for who, what in self.transcript) or "(без ответа)"
