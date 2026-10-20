# fastpath: fast command path (#38) and whisper response (#42)

Branch `claude/fastpath-3b5831`. Tests: `server/tests/test_fastpath.py` (59), total 485 passed, 2 skipped.
**Nothing has been verified in real life** (actual Vosk with this grammar, satellite device, Gemini, whisper thresholds).
`docs/market-research.md`, mentioned in the issue, is not in the repository: decisions were taken from the issue description.

## 1. Fast Path (`server/jarvis/fastpath.py`)

After the wake word, audio goes both to Gemini and to local Vosk with a grammar of command words (small vocabulary,
numbers as words: "пять" (five), "пятнадцать" (fifteen), "двадцать пять" (twenty-five), "сто двадцать" (one hundred twenty), "полчаса" (half an hour), "четверть часа" (quarter of an hour), "час" (hour)). A command
triggers only if ALL conditions are met: Vosk returned a complete phrase (by pause, not a partial
result), confidence of each word ≥ 0.7, no `[unk]` in the phrase, the whole phrase (without the "мага" (Maga) tail at the beginning and
"пожалуйста" (please) at the end) is exactly equal to one of the commands, and Gemini has not started answering/calling functions yet. Otherwise, the phrase goes
to Gemini as before. Choosing "complete result only": partial is faster, but "включи свет… (пауза) …в
коридоре" (turn on the light… (pause) …in the corridor) would trigger on the half-phrase.

Commands: room lights (turn on/off/dimmer/brighter/"turn off everything", via the new `Lights.room()` over
existing `lightstate`/HA), timer ("таймер на N минут/секунд/час/полчаса" (timer for N minutes/seconds/hour/half an hour), "отмени таймер" (cancel timer), via
`brain.set_timer`), "стоп/тихо/замолчи/хватит/перестань" (stop/quiet/shut up/enough/stop), "который час" (what time is it), "сколько до будильника" (how long until the alarm) (from
`bridge.phone_state.next_alarm`), "отвечай на команды вслух/молча" (answer commands aloud/silently). The list is expanded via the `PHRASES` table and
the branch in `FastPath.execute`.

Confirmation: by default a short 1175 Hz tone (70 ms) + `happy` mimic + screen caption, without voice.
With voice "Готово" (Ready) (via a separate short conversation by text through Gemini, after closing the main one) we speak
only for timers and upon error ("не нашёл ни одной лампы" (did not find any lamps)). Time and alarm in off mode — caption only.
Log: line `быстрый путь: «…» (вид) выполнено за N мс после слова пробуждения` (fast path: «…» (type) executed in N ms after wake word), console `fast log`.

Settings (prefs in SQLite, survive restart): `fastpath` on/off (on by default if model is present),
`fastpath_voice` off|short|full (off by default). Console: `fast on|off|status|log`, `fast voice off|short|full`.
By voice: "отвечай на команды вслух" (answer commands aloud) (short) / "молча" (silently) (off). In German and games, the fast path is disabled.

## 2. Whisper (`server/jarvis/whisper.py`)

Sign by PCM (16 kHz, 40 ms frames): average level of active frames below threshold (default −36 dBFS)
AND no more than 25% of frames with fundamental frequency (normalized autocorrelation at 50–400 Hz > 0.5). Decision made once
per conversation, after ~0.5 s of speech. When whispering: speaker volume = 25% of daytime level (not lower than 5, not louder than nighttime level),
restored afterwards; Gemini receives a `WHISPER_NOTE` note ("отвечай шёпотом, одной короткой фразой" (answer in a whisper, in one short phrase))
via `LiveSession.send_note` (clientContent with turnComplete=false). Console: `whisper on|off|status|<dBFS>`
(on by default). Statistics are written to the log ("средний уровень … звонких кадров …" (average level … voiced frames …)) — use it to tune the threshold.

## Affected Shared Files (Additively)

`voice.py` (listeners in `Conversation`, event `fast`, `whisper_mode`, `fast_factory`, console commands,
voice confirmation after conversation), `lights.py` (`room()`), `gemini_live.py` (`send_note`), `character.py`
(`WHISPER_NOTE`), `__main__.py` (fast path factory). New modules: `fastpath.py`, `whisper.py`.
No new `.env` variables. The Vosk model is the same as for the wake word (already downloaded).

## Check in Live

- Vosk with this grammar: does it recognize commands in a real room and how often does it confuse foreign speech (confidence threshold
  `MIN_CONF` = 0.7 was not tuned). Check logs and `fast log`.
- Latency: how many ms to execution vs 0.6–0.8 s for Gemini; does Vosk phrase-end beat Gemini.
- Voice "Готово" (Ready) after a timer runs via a new Gemini session by text (~1–2 s): slower than the command itself.
- Whisper: real values of "average level / voiced frames" for whispering and normal speech, adjust `whisper <dBFS>`.
- Does the selected Live model accept `clientContent` with `turnComplete=false` in the middle of a session (otherwise the note won't work, only volume reduction will remain; a warning will appear in the log).
- `Lights.room("dim"/"bright")` uses `brightness_step_pct` ±25 via HA on turned-on lamps in the room.

## Remaining

- Vosk partial results (even faster) intentionally not used; can be added if confidence in accuracy is high.
- Commands with lamp/room names ("выключи свет на кухне" (turn off the kitchen light)) go to Gemini.
