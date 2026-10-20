# night-voice: night voice, character, quiet mode, fallback on 503

Branch `claude/night-voice-85eb78`. Tests: `server/tests/test_night_voice.py` (17 new), 212 passed in total, 2 skipped.
**Nothing has been verified live** (Gemini, smart speaker, Vosk).

## What has been done

1. **Quieter and shorter at night (#2).** The `night_start..night_end` window from `SettingsStore`. For the duration of
   a conversation (`Conversation.run`), speaker volume = 45% of daytime volume (not lower than 8, not louder than
   daytime); after the conversation, the current `volume` from settings is restored (settings themselves do not
   change). A prompt block has been added: "night: quietly, briefly, in a single phrase" (`character.NIGHT_NOTE`);
   reminders at night ask for «вполголоса» ("in an undertone"). The alarm clock does not go through voice, it is
   unaffected. The prompt of a pre-warmed session can lag behind the night boundary by up to 150 s; volume is decided
   at the moment of the conversation.
2. **Affective by default (#22).** `JARVIS_AFFECTIVE` defaults to `1`; a saved pref (`affect off`) takes precedence.
   If an affective session fails to connect (not an overload), `Voice._connect_live` immediately opens a standard one
   and does not attempt affective for 10 minutes (`AFFECTIVE_OFF_S`); a `WARNING` entry is logged.
3. **Character by time of day (#23).** `character.py`: morning (5–11) −15 to sarcasm and a gentle note, evening
   (18–23) +15, from 23:00 to 5 −30 ("calm"). If the slider is below 45, the number is left untouched (only the note
   is omitted). Also works in the Telegram prompt (which is shared).
4. **"Do not listen" (#31).** Function `mute_listening(minutes)` (1…480, default 60) in `brain.TOOLS`, executed via
   `Brain.mute_hook` → `quiet.Quiet`. Sends `{"type":"quiet","on":true}` to the speaker, audio does not go to Gemini
   or the wake word detector; the current conversation ends after confirmation. Exit: timer, the phrase «вернись»
   ("come back"), console `mute off`.
   **Design choice:** in quiet mode, the server itself listens with local Vosk only for the words «мага» ("maga") and
   «вернись» (`quiet.VoskWords`). «Вернись» → `quiet off` + `listen_start` and a brief «Слушаю» ("Listening"); «мага» →
   «Я молчу. Скажи: Мага, вернись» ("I'm keeping quiet. Say: Maga, come back") (voiced by Gemini via text prompt with
   the microphone closed, `Conversation(one_shot=True)`; no more than once per 20 s). Reminders and background results
   still play audio in quiet mode, but without opening the microphone.
   **Contract discrepancy:** `protocol-next.md` states that in `quiet` mode the speaker does not send audio at all,
   meaning the server will not hear the word «вернись». The firmware needs to keep streaming audio to the server in
   `quiet` (only drawing the dot and «Тишина» ("Quiet")), or exiting must rely solely on the timer/console/Telegram.
   Pass this on to the firmware session.
   Quiet state lives in memory: a server restart clears it.
5. **Fallback on 503 (#35).**
   - Voice: `LiveSession.__aenter__` now turns a handshake rejection (`InvalidStatus`, 503) into `LiveError`. On
     overload, `Voice._connect_live` tries `JARVIS_GEMINI_FALLBACK_MODEL` and sticks with it for 120 s; Gemini announces
     this once per 10 minutes itself (phrase in the session prompt). If there is no fallback model or it also fails:
     screen caption «Gemini перегружен, попробуй позже» ("Gemini is overloaded, try again later").
   - Background agents: on 503/overload, the same request is sent to `JARVIS_FALLBACK_TEXT_MODEL`; the result is
     voiced with a notice once per 10 minutes; without fallback, the reason is «Gemini перегружен».
   - Telegram: existing logic untouched; new: on the second consecutive 503 (and if a fallback model is set), switch
     to the fallback, with «⚠ Основная модель перегружена…» ("⚠ Primary model is overloaded…") appended to the reply
     once per 10 minutes.

## New `.env` variables (all optional)

- `JARVIS_AFFECTIVE` (now defaults to `1`).
- `JARVIS_GEMINI_FALLBACK_MODEL` (defaults to `gemini-live-2.5-flash-preview`; **name unverified**, empty = no fallback).
- `JARVIS_FALLBACK_TEXT_MODEL` (defaults to `gemini-flash-latest`, empty = no fallback).

## Console

`mute <minutes>` / `mute off` / `mute` (remaining time).

## Affected shared files (additive)

`voice.py` (prompt, volume, `_connect_live`, `one_shot`, quiet hooks), `brain.py` (tool + hook), `config.py`,
`gemini_live.py` (503 → LiveError), `agents.py`, `telegram.py`, `__main__.py` (quiet listener, fallback models),
`.env.example`. New modules: `character.py`, `quiet.py`, `gemini_fallback.py`.

## Verify live

- At night: volume drops during conversations and restores afterward; phrases are short.
- Affective is actually accepted by the chosen model (otherwise the log will show fallback; set `JARVIS_AFFECTIVE=0` if
  it falls back permanently).
- «Мага, не слушай час» ("Maga, do not listen for an hour"): what the speaker sent in `quiet` and whether the server
  hears «вернись» (see discrepancy above).
- Vosk accepts «вернись» (grammar of target words + lures, misdetections possible).
- Name of the fallback Live model and that it accepts the same setup.

## Remaining / open questions

- Resolve the `quiet` discrepancy with the firmware (above).
- Confirm the fallback Live model name (must be accessible to the user).
