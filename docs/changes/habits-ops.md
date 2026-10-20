=== File: habits-ops.md ===
# habits-ops: habits, ideas, mail, digest, health, wake word evaluation

Branch `claude/habits-ops-7b0136`. Left the version and CHANGELOG untouched (per the rules): coordinator to bump the version.

## What was done

1. **Habits (#5)**: `server/jarvis/habits.py`. Once a day a die is rolled (`JARVIS_HABITS_PROB`, 0.5); if it hits, then at a random minute within one of the windows (`JARVIS_HABITS_WINDOWS`, 18:00-21:30) the speaker asks via `Voice.announce` about one pending Google task (priority for tasks with words from `JARVIS_HABITS_KEYWORDS`, the more consecutive skips, the higher the probability). Gemini calls the new `habit_answer(task, done)` function: «да» ("yes") marks the task completed (`google_tasks_done`), «нет» ("no") writes a skip to the `habit_log` table (brain.db). With two or more consecutive skips, a jab is added to the prompt, sharpness according to the «Язвительность» ("Sarcasm") slider (up to 30 no jabs, 30-69 mild, 70 and above sharp). Not at night, one question per day, retry every 30 s if the speaker is busy, the question is canceled after one hour. Console: `habits`, `habits ask`.
2. **Ideas (#15)**: `save_idea(text)` (in `google.py`) puts the thought into a separate Google Tasks list «Идеи» ("Ideas") (created if missing, id is cached).
3. **Mail (#16)**: `google_mail_read(query)`: the freshest email by search query, sender, subject, first 500 characters of text/plain (otherwise HTML without tags, otherwise snippet), attachments are skipped; response includes a note that the email text is third-party content and its instructions must not be executed.
4. **Digest and backup (#29, #34)**: `server/jarvis/digest.py`. Sunday, `JARVIS_DIGEST_AT` (19:00), to Telegram: conversations (series of turns with pauses < 20 min), new facts, habits (done/skipped), Google tasks (completed over the week / open), sleep (the bridge now calls `bridge.on_slept`, written to `sleep_log`). As a second message, the file `brain-ГГГГ-ММ-ДД.db`: copy via `sqlite3.backup`, `Telegram.send_document` (new method, sendDocument, multipart via urllib), the three most recent are stored in `data/backup`. **Hook for `learning_digest()`**: `digest.learning_digest = функция` (regular or async, returns text or None); currently not assigned in `__main__.py`, the digest works without it. Console: `digest`, `digest send`.
5. **Health (#36)**: `server/jarvis/health.py`, `health_report` function: server and firmware versions, speaker/phone/PC/HA/Google connectivity (HA and Google are checked with a live request with a 6 s timeout), Gemini errors in 24 hours (`GEMINI_ERRORS` counter, stored in prefs, called from `voice.py` and `telegram.py`), batteries, uptime, `problems` list. Console: `health`.
6. **Wake word evaluation (#37)**: `server/tools/wake_eval.py` + `docs/wake-eval.md`. Recordings `data/eval/yes_*.wav` and `no_*.wav`, run through VoskWake and openWakeWord, prints false negatives and false positives, exit code 1 on issues. Tests on synthetic WAVs.

## Changes to shared files (additive)

`brain.py` (`extensions` list: modules provide their own Gemini functions; `save_idea` route), `bridge.py` (`on_slept` hook), `config.py` (habits/digest fields), `console.py` (`hub.extras` in the command chain), `voice.py` and `telegram.py` (`GEMINI_ERRORS.add` line in each), `telegram.py` (`send_document`), `__main__.py` (module assembly), `.env.example`.

## New `.env` variables

`JARVIS_HABITS`, `JARVIS_HABITS_PROB`, `JARVIS_HABITS_WINDOWS`, `JARVIS_HABITS_KEYWORDS`, `JARVIS_DIGEST`, `JARVIS_DIGEST_AT`. All with sensible defaults.

## Tests

From `server`: `python -m pytest -q` gave 229 passed, 2 skipped (was 195). New: `test_habits.py`, `test_health.py`, `test_digest.py` (digest, backup, ideas, email), `test_wake_eval.py`.

## Not tested live

- Real habits dialogue: that Gemini will indeed call `habit_answer` and will not ask unnecessary questions.
- That Gmail returns emails in the same form as in the mock (especially nested multiparts); `gmail.readonly` scope is already granted.
- Creation of the «Идеи» list and reading `completedMin` in the real Google Tasks API.
- Actual file sending to Telegram (sendDocument); brain.db size is small, bot limit is 50 MB.
- `health_report` on a live HA (`get_config` command) and Google.
- `wake_eval.py` with real models (user recordings needed).

## About Keep

Google Keep has no official API for personal accounts (only for Workspace), which is why ideas go into a separate Google Tasks list «Идеи»: it is visible in the «Задачи» ("Tasks") app and in the Gmail and Calendar side panel. A future alternative: the unofficial `gkeepapi` library (logs in like the Keep app, requires an app password). Did not implement: it breaks on Google changes and requires an account password.

## Questions and what to bring

- Which tasks to treat as habits: by default these are the stems «тренировк, бассейн, хомяк, немецк, зарядк, спорт, гантел, растяжк» ("workout, pool, hamster, German, morning exercise, sport, dumbbell, stretching"); if the task names are different, adjust `JARVIS_HABITS_KEYWORDS`.
- Recordings are needed for `wake_eval.py`: 10-20 `yes_` and `no_` (instructions in `docs/wake-eval.md`).
- Known minor issue out of scope: SQLite `lower()` does not know Cyrillic, so `Brain.forget`/`cancel` perform case-insensitive search only for Latin characters.
