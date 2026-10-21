# Changelog

## 0.14.6
- **Forgotten Google Tasks are ignored**: tasks whose due date passed more than a month ago (or, without a due date, were
  not changed for a month) are no longer read aloud, counted or asked about, and no longer feed the self-learning.
  `JARVIS_TASKS_MAX_AGE_DAYS` (default 30, 0 turns it off).

## 0.14.5
- **Light colors by voice**: `home_set` knew only 22 color names, and an unknown one («crimson») was silently ignored: the
  lamps just turned on as they were and the answer was «done». Now ~70 names (Russian and English, endings like
  «малиновая», «тёмно-»/«светло-»/«ярко-» shades), `#RRGGBB` for anything else, and an unknown color is an error
  that leaves the lamps alone.

## 0.14.4 (firmware 0.7.2)
- **Odd-size audio frame dropped the speaker's connection** (rare, seen twice): the frame is now trimmed to whole samples
  and an exception in an audio handler is logged instead of closing the connection.
- **Firmware 0.7.2**: claps reach the server more reliably (3 s wait, 3 attempts) and the face reacts at once; the
  firmware image inside the add-on is now the 0.7.2 one (the 0.14.3 add-on still carried an early 0.7.1 build).

## 0.14.3 (firmware 0.7.1)
- **Speaker answered nothing after 0.14.0** (fixed in 0.14.1): the `Health` extension had no `TOOLS`, so building the
  Gemini tool list failed on every conversation. Regression test added.
- **Gemini 3.8 Live and the emotional mode** (0.14.2): the setup is accepted but the first reply closes the session with
  1007. The mode is now switched off for good on the first such close and the same question is repeated in a plain session.
- **Weather icon** (0.14.3): the server pushes weather to the screen on connect and every 15 minutes (`weatherfeed.py`),
  not only when someone asks.
- **Whisper response is off by default**: its thresholds were never tuned on real recordings and normal speech from a
  distance was taken for a whisper. Enable with `whisper on` after tuning.
- **Firmware 0.7.1**: «flinch» on a loud sound used `-(d - 350)` on an unsigned value, so its factor became infinity and
  never decayed: the eyes froze (they did not close in sleep mode) and the geometry became NaN (the 0.7.0 panics).
  Clap detector: gap between claps 0.2-1.0 s (was 0.25-0.8) and a log line for every decision of the detector.

## 0.13.1
- **Skill build limit** (Claude subscription): default 1 build per day, 3 per week, 4-hour pause;
  `/skills build on|off` and `SKILL_MAX_PER_DAY`, `SKILL_MAX_PER_WEEK`, `SKILL_MIN_GAP_H`. Single build:
  20 turns, $1.5, 12 minutes. The runner automatically finds the Claude application's `claude.exe`.
- **Backup schedule** for the alarm clock: if WebUntis blocks access (login via Microsoft), it calculates as
  usual: Mon-Fri first lesson at 8:00 (`WAKE_SCHOOL_START`), wake-up at 6:20 with 59 min prep time.
- «Колонка» app 0.4.0: haptic feedback on sliders and buttons (in APK), cover art palette, alarm clock.

## 0.13.0
Major merge of ten parallel efforts. Details for each: docs/changes/*.md.
- **Lighting** (`lights.py`, `lightstate.py`): «Мага, спокойной ночи» ("Maga, good night") smoothly turns off all lights in ~30 s and speaks
  the morning briefing; lamp colors are remembered and restored after sunrise (HA automation unchanged); desk
  backlight colors from track cover art (`music_light on|off`, requires phone app 0.3+); double clap
  toggles all lights in the room.
- **Screen and sound** (requires firmware 0.6.0): progress ring and «Ждёт тебя» ("Waiting for you") (`task_status`), games and reminder counter in a new style, eye reactions to sound and music; claps on the speaker itself, alarm and timers without server (`plan`).
- **Night and voice**: quieter and shorter at night; Gemini emotional mode by default; character by time of day;
  «Мага, не слушай час» ("Maga, don't listen for an hour") (`mute_listening`, return with word «вернись» ("come back")); fallback to backup model on 503.
- **German and games**: "let's speak German" mode with vocabulary and spaced repetition, voice games
  (cities, 20 questions, quiz).
- **Morning**: alarm by WebUntis schedule (`UNTIS_ICAL_URL`), gentle wake-up, school bus
  (`TRANSIT_STOP`, `TRANSIT_LINE`; ÖBB HAFAS), «возьми зонт» ("take an umbrella").
- **Habits, ideas, summaries**: random questions on Google Tasks, `save_idea` ("Ideas" list in Tasks),
  reading an email, weekly summary and copy of `brain.db` to Telegram, `health_report` («как ты?» ("how are you?")),
  `tools/wake_eval.py`.
- **Skills and Claude Code**: skill plugins (`skills/`), `propose_skill` with approval in Telegram and runner
  on laptop (`tools/skill_runner.py`); Claude Code progress hook (`tools/claude_hook.py`).
- **"Living" Maga** (`learning.py`): night reflection, prompt preferences, feedback («слишком язвительно» ("too sarcastic")), log and rollback, rare initiative.
- `deploy_addon.py` now passes all keys from `server/.env` to the addon, except service ones.


## 0.12.0
- Presence (`jarvis/presence.py`): while you are away, the speaker does not respond to the wake word
  and to `ask`. Reminders, timers, alarm, and background tasks sound as usual.
  "Here": computer is active; at night (sleep window) — if at home or HA is silent; phone in hands;
  "away": no one is home according to HA, or 3 hours without activity. No data — considered "here".
- Manually: `presence auto|here|away` in console and in Telegram (also `/presence here`), by voice
  «Мага, я ушёл» ("Maga, I left") (`set_presence`). Choice is stored in `brain.db`. Turn off completely: `JARVIS_PRESENCE=off`;
  thresholds `JARVIS_PRESENCE_PC_MIN/PHONE_MIN/IDLE_MIN`.
- Routines (`jarvis/routines.py`): the speaker starts a conversation itself, and Gemini calls the necessary
  tools in it (time, weather, calendar, tasks). Morning briefing once a day: when the alarm on the phone
  is turned off, or if not turned off, at `JARVIS_BRIEFING_AT` (08:00); not earlier than the end of
  the night, speaker busy with conversation — repeats up to an hour. Greeting when a person (HA `person.*`)
  returned home after an hour or longer: no more than once every 3 hours and not at night. Switch
  `JARVIS_ROUTINES=off`. Console: `routine briefing|home`.
- `tools/import_memory.py memory.json`: migration of the old assistant's memory to `brain.db`
  (profile and facts, future reminders, without duplicates and expired ones).

## 0.11.0
- Google directly, without HA (`jarvis/google.py`, instructions docs/google.md): calendar (all
  calendars), Gmail read-only, Tasks. Functions `google_calendar_list/add`, `google_mail_unread`,
  `google_tasks_list/add/done`. Work in voice and in Telegram. One-time login:
  `tools/google_auth.py` (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN in .env).
  Without these keys, Google is disabled.

## 0.10.5
- More lively and real: speech manner prompt rewritten (immediate reaction, laughter, sighs,
  «ага/хм/слушай» ("uh-huh/hm/listen"), varied sentence length, no «конечно» ("of course") and question retellings).
- Voice changes by voice: «Мага, смени голос» ("Maga, change voice") / «говори бодрее» ("speak more briskly") (`set_voice`, 30 Gemini voices),
  emotional mode on request (`set_affective`). Choice is remembered in `brain.db`, console
  `tts` and `affect` commands too.

## 0.10.4
- Less nagging about sleep: the jab «Подъём через… ч» ("Wake-up in... h") is now only when you are really sitting
  deep at night (from 01:30 until wake-up time, `remark_from` time) and once per night, not every
  half hour from 23:30. Evening check before sleep (23:00) unchanged.

## 0.10.3
- The "Sarcasm" slider in the app means something again across the entire scale: up to 45 calm,
  45-70 sarcasm in every second answer, 70-90 in almost every one, from 90 daring and harsh (without
  insults and profanity). Applied in Telegram immediately, in voice up to 2.5 minutes (template update).

## 0.10.2
- Character: Maga and the Telegram bot are equally sarcastic. In the general prompt, the character
  is described directly (answer to the point first, then a short sarcastic remark, with an example;
  softer when a person feels bad), rather than by a hint «можешь пошутить» ("you can joke"), which the voice model almost ignored.

## 0.10.1
- Search without a map: Tavily (TAVILY_API_KEY) and Serper (SERPER_API_KEY) added; choice —
  `JARVIS_AGENT_SEARCH=tavily|serper|brave|searxng|web|google`.

## 0.10.0
- Shared conversation history: Maga (voice) and the Telegram bot now know what they talked about
  with each other. Utterances of both channels are written to the messages table (`brain.db`) as the conversation goes;
  service prompts (reminders, background tasks) are not written. Both prompts receive a
  "Recent conversation" block (last utterances for 24 hours with source and time marks). The prepared
  Gemini session is recreated after each new utterance so that its history is fresh.

## 0.9.2
- Telegram responds again: the "large" `gemini-flash-latest` model on a free key gives
  20 requests per day and responded with 429. By default, it is now `gemini-flash-lite-latest`, and upon
  hitting the daily quota, the bot automatically switches to the backup model instead of waiting.

## 0.9.1
- Search for background tasks: DuckDuckGo now shows a CAPTCHA (it doesn't like bots), so
  Brave Search API (`JARVIS_AGENT_SEARCH=brave`, `BRAVE_API_KEY`) and own SearXNG
  (`JARVIS_AGENT_SEARCH=searxng`, `JARVIS_SEARXNG_URL`) have been added. Model query parsing is cleaner.
- Weather: whole degrees («от 6 до 18» ("from 6 to 18")), otherwise Gemini confused decimals when reading aloud.
- `JARVIS_AGENT_*`, `BRAVE_API_KEY`, `JARVIS_SEARXNG_URL`, `JARVIS_BARGE_*` now go into the build.

## 0.9.0
- Calendar and weather from Home Assistant: `home_calendar` («что у меня завтра?» ("what do I have tomorrow?")) collects events
  from all calendars, `home_weather` («какая погода, брать ли зонт?» ("what's the weather, should I take an umbrella?")) answers via `weather.*`
  with a five-day forecast and simultaneously updates the weather icon and temperature on the speaker screen.

## 0.8.5
- After a background task, does not repeat «уже ищу» ("already searching"): Gemini says one short phrase, and the conversation
  immediately goes into standby (does not wait for continuation for 6 s and does not insert «жду данные» ("waiting for data")). The speaker will voice the result itself as soon as it is free.

## 0.8.4
- Internet search without Google quota: the free key gives 20 requests per day for `gemini-flash`
  (and Google search closes with a 429 error). Now background agents take DuckDuckGo results
  and retell them with a cheap model (`JARVIS_AGENT_MODEL`, defaults to
  `gemini-flash-lite-latest`; `JARVIS_AGENT_SEARCH=google` restores Gemini search). If the task failed, Gemini states the reason («квота исчерпана» ("quota exhausted")).
- Fewer mid-word cutoffs: sentence end pause 650 ms instead of 450, speech end sensitivity
  low (`JARVIS_SILENCE_MS`).
- Barge-in stricter (firmware 0.5.1): threshold ×4 instead of ×3, voice from 200 ms continuously, pause in answer
  and echo tail are not counted until sound starts playing. Setting: `barge on <threshold>`.

## 0.8.3
- Wake word «Мага» ("Maga"): custom Russian word on Vosk (speech recognition with grammar from
  "maga" and similar decoy words, triggers on partial result, without waiting for the end of
  the phrase). "Hey Jarvis" works in parallel. List — `JARVIS_WAKE_WORDS` (default
  `мага,jarvis`). The Vosk model (~45 MB) is downloaded on first launch.
- The assistant's name is Maga (male grammatical gender). Speech manner is more lively: intonation by mood, pauses,
  «ага/хм/ой» ("uh-huh/hm/oh"). Console commands `tts <name>` (change voice on the fly) and `affect on|off`
  (Gemini emotional dialogue) — listen and choose; permanently via `JARVIS_VOICE` and `JARVIS_AFFECTIVE`.

## 0.7.2
- Voice barge-in (requires firmware 0.5.0): while the speaker is talking, it listens to the microphone
  locally and compares it with the volume of its response; voice noticeably louder than echo for longer than 160 ms —
  the speaker immediately shuts up, sends `barge_in`, and the conversation continues from your phrase (the speaker sends the beginning
  of the phrase to the server afterwards). The rest of Gemini's old response is discarded.
  Settings: `JARVIS_BARGE_IN=0` disables, `JARVIS_BARGE_RATIO` (default 3) - sensitivity;
  on the fly: console command `barge on|off [threshold]`.

## 0.8.3
- Telegram: up to five attempts on Gemini overload, pauses grow.

## 0.8.2
- Telegram: on Gemini overload (429/503), the request is retried up to three times.

## 0.8.1
- Telegram: fixed default model (`gemini-flash-latest`), the previous one was closed by Google.

## 0.8.0
- Telegram bot: type text in chat — Gemini responds with text using the same tools as voice
  (memory, reminders, timers, smart home). Does not respond to alien `chat_id`s. If the speaker
  failed to speak a reminder (absent or busy), it arrives in Telegram. Enabled by
  `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_IDS` variables in `.env`; in console — command `tg <text>`.

## 0.7.1
- Responsiveness: the Gemini session is now always open in advance (as soon as the speaker is online) and
  refreshed every 150 s and after each conversation. After the wake word, the conversation
  starts immediately, without 1.1-1.5 s for connection. A closed session is replaced by a new one.
- Time in the prompt is now "about"; precise time Gemini takes via the `current_time` function.
- In the log for each response: how long session capture took and when sound started.

## 0.7.0
- Background agents (`jarvis/agents.py`): long tasks (internet search, news, selections)
  go to the background via the `start_task` function; you can continue talking to the speaker, and it will voice the ready result
  itself as soon as it is free. Up to 3 tasks simultaneously, `list_tasks`, `cancel_task`.
  Agents model: `JARVIS_AGENT_MODEL` (defaults to `gemini-flash-latest`).
- Semantic standby mode: Gemini itself ends the conversation (`end_conversation`) when
  the user thanked or said goodbye, and the speaker does not wait 6 seconds for continuation.

## 0.6.4

- Voice more responsive: connection to Gemini opens while the wake word is still being pronounced.
- Gemini determines the end of a phrase after 450 ms of silence instead of 700 (`JARVIS_SILENCE_MS` in `.env`), speech sensitivity high.

## 0.6.3
- Fixed: «подсветка стола фиолетовая» ("desk backlight purple") fell on a neighboring number entity ("Desk backlight speed") and crashed with `number.turn_on not found`. Now exact name is more important than partial, light is more important than number/select/scene, unavailable devices go last, and only lamps are searched for color.

## 0.6.2
- Light list shows what each lamp can do (color, white only, brightness only,
  effects). If color is requested and the lamp cannot do it, Gemini receives the exact reason and a list
  of colored lamps in the same room, rather than refusing at random. "Do not refuse in advance, call the function" added to the prompt.

## 0.6.1
- Smart home without limits: light color (names and `#RRGGBB`), effects, smoothness, and any
  HA service over any device (`home_service`): climate, players, locks, vacuums, scenes,
  scripts, automations, number/select.

## 0.6.0
- Smart home: Gemini controls lights, sockets, curtains, and reads sensors via HA
  (`home_set`, `home_state`, `home_list`). Understands the room as a whole («свет в спальне» ("light in the bedroom")),
  brightness and warmth. The addon needs access to the HA core API — when updating, HA may
  ask to confirm.

## 0.5.0
- Brain: Gemini now calls functions — remember a fact (SQLite `brain.db`), set
  a reminder or timer. The speaker pronounces reminders on time itself; facts get
  into the beginning of each conversation.

## 0.4.0
- Over-the-air speaker update: `ota` command (`ota info` — versions), firmware image travels
  in the addon. Speaker firmware 0.4.0 is required — it is flashed via wire for the first time.

## 0.3.3
- The app sees what is currently on the speaker screen: waiting, sleeping, listening, thinking,
  speaking, timer, night, card, player (`settings` message -> `speaker.screen`).

## 0.3.2
- "Check sound" button in app 0.2.0 (short beep on the speaker).
- Default volume 50 (new speaker scale 0.3.0: 50% — loud and clean).

## 0.3.1
- Commands `last` (entire last conversation) and `log` (last lines of the log).
- Responds only in Russian, even if another language was heard.

## 0.3.0
- Voice: wake word "Hey Jarvis" on the server, conversation via Gemini Live —
  voice response immediately, phrase transcription on the screen, continuation without word for 6 s.
- Commands: `ask <question>` (ask without the word), `wake` (how it hears the word).
- Image on Debian (onnxruntime), word models downloaded on first launch.

## 0.2.0
- Sound: recording from the speaker microphone and playback on its speaker.
- Check commands: `rec`, `echo`, `beep`, `play`, `stop`, `vol`, `gain`.
- Speaker volume is stored in settings along with brightness.

## 0.1.0
- First version: speaker screen, bridge with phone and computer.
