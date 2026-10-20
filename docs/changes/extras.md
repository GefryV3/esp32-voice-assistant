=== File: extras.md ===
# Extras: Telegram voice on the speaker, translator, topic-based news, breaks

Branch `claude/extras-6449ee`. Not deployed, version and CHANGELOG left untouched. Tests: `server/tests/test_extras.py`
(25 tests on mocks, without network); entire suite: 452 passed, 2 skipped.

## 1. Telegram Voice on the Speaker (`jarvis/tg_voice.py`, updates to `telegram.py`)
- The bot accepts `voice` and `audio` only from `TELEGRAM_CHAT_IDS`, downloads the file (getFile + download),
  decodes it into 24 kHz mono PCM, and plays it via `Audio.stream` with a short cue signal before the message.
- Decoder: **ffmpeg from apt** (`addon/Dockerfile`: `tzdata ffmpeg`). Reason: there is no pure-python Opus decoder,
  libopus via ctypes + custom Ogg parsing is fragile and lacks mp3/m4a support; ffmpeg reads everything, and required
  Python dependencies are absent. The image will grow by about 100-150 MB.
- Rules: no speaker / speaker in a conversation / silence mode ("do not listen" («не слушай»)) → message goes into the queue
  (up to 3 items, lives for an hour, then tells the bot "timed out" («не дождался»)); at night, volume is lower for the duration
  of the message (as in a conversation). The bot replies "Played." («Озвучено.») or gives the reason. Longer than 60 s
  and files larger than 4 MB are rejected.
- `/say <text>` speaks the text in Maga's («Мага») voice via `Voice.announce` (Gemini, verbatim, at night in a half-whisper),
  up to 600 characters.
- Switch `JARVIS_TG_VOICE=on|off` (default on). When off, voice messages are silently ignored, as before.
- Console: `tgvoice` (status and queue).

## 2. Universal Translator (`jarvis/translate.py`)
- Gemini function `translate_mode(mode, language)`: `phrase` (one-off), `interpret` (Russian ⇄ language until "stop translating" («хватит
  переводить»)), `off`. Rules result from the function, just like for German; when opening a session in a mode (console
  `tr on <language>`), they go into the system prompt instead of "Russian only" (`voices._new_live`).
- Doesn't break German: enabling one disables the other (`brain._call`), the mode lives for one conversation
  (`Brain.end_conversation`), `long_talk` takes the translator into account. Language name is validated (letters, up to 40 characters).
- Console: `tr on <language>|off|status|prompt`.

## 3. Topic-Based News (`jarvis/news.py`, `Routines.briefing_extra`)
- Topics in `brain.db` (`news_topics`); voice function `news_topics` ("follow news about: tech, Austria" («следи за новостями про: техника, Австрия»));
  console: `news topics|add|del|now|on|off`.
- The regular morning briefing gets up to 3 news items (by phrase, topics rotated by day of the year) via
  `brain.agents.generate` (Tavily/Serper/DDG search). This is an addition to the briefing prompt, there is no separate announcement;
  we search once a day (cache), yesterday's news is not repeated. Soft wake-up (SOFT_PROMPT) does not receive news.
- `JARVIS_NEWS`: not set = turns on automatically when topics are named (stays silent without topics); `off` = completely disabled;
  `on` = enabled. You (the user) need to name the topics.

## 4. Breaks (`jarvis/breaks.py`)
- Based on `bridge.pc.state["idle"]`: inactivity no more than 5 min = work continues; after `JARVIS_BREAK_MIN` minutes
  (default 120, 0 = completely disabled) once per period, a short prompt via announce. Not at night, not in
  silence, no more than once every 90 minutes (marker in prefs), speaker busy — repeat at the next step.
- Disabled by default. Enabling: "remind me about breaks" («напоминай про перерывы») (function `breaks_mode`) or console `break on|off`.

## New .env Variables (all optional, in `.env.example`)
`JARVIS_TG_VOICE`, `JARVIS_NEWS`, `JARVIS_BREAK_MIN`.

## To Verify Live (none of this has been tested)
- The addon image includes `ffmpeg`; a voice message from Telegram is decoded (OGG/Opus) and plays on the speaker, the cue signal is not
  deafening, quieter at night; queue works during "do not listen" («не слушай») and in conversation.
- `/say` speaks text verbatim (Gemini may add extra stuff: tighten the prompt if necessary).
- Translator: "translate from Russian to English" («переводи с русского на английский») holds the mode and exits upon "stop translating" («хватит переводить»);
  quality of rare languages on the live voice model.
- News: search actually returns a fresh phrase; the addition does not bloat the briefing.
- Breaks: `idle` from the PC app arrives in seconds (as in `PC_ACTIVE_S`).

## Remaining / Notes
- `docs/market-research.md` was not found in the repository, implemented based on the task description.
- The "silence mode" («режим тишины») gate takes `Voice.quiet.active`; the "presence away" mode does not block voice messages.
- To bring: news topics; decide whether to enable breaks.
