# alive-learning: user learning and «живость» ("liveliness")

New module `server/jarvis/learning.py`, tests `server/tests/test_learning.py` (13 tests, using Gemini mocks).

## What was done
- **Night reflection** (around 04:00 or after 3 h of silence, once a day): Gemini (`agent_model`) reads messages for the past 24 hours, returns JSON: fact / style / correction / habit / request / followup. Tables in brain.db: `learned(kind, text, confidence, source, ts, applied, status, due)` and `changelog`. Confidence >= 0.7 is applied immediately, 0.4–0.7 goes to Telegram as «Заметил: …. Запомнить?» ("Noticed: .... Remember?") (reply «да» ("yes") / «нет» ("no") right there or by voice via `confirm_learning`), below that is discarded.
- **Injection**: `voice.system_prompt(..., learned=)` (additively) receives the block «Что ты о нём понял» ("What you understood about him") (up to 8 items, up to 700 chars) + a mood/opinion line. Works for both voice and Telegram.
- **Feedback**: Gemini function `feedback(kind, note)` (wrong, too_sarcastic, too_long, too_short, good) updates rules immediately; `what_changed` — changelog; `undo_change` — rollback of the last change or by substring. `forget` (Gemini tool) now also forgets learned items. Secrets (passwords, keys, tokens, card numbers) are filtered out by `looks_secret`.
- **Liveliness**: `mood()` (energy based on time of day and utterance count, curiosity based on "loose ends"), `persona()` in the prompt. Spontaneous utterances (`maybe_speak`): not at night (`routines.night`), only when `presence.here()`, no more than 3 per day, interval >= 3 h, ~25% chance per minute. Trigger: a matured "loose end" (an event from reflection with `due`) or a light remark (weather / previous topic). Kill switch: pref `initiative=off`.
- **`Learning.digest(days=7)`** — a string for the weekly summary (learned, accepted, rejected/reverted). Will be wired up by habits-ops.

## Changes in shared files (minimal)
`brain.py` (`learning` field, tools, forget), `voice.py` (`learned` parameter), `telegram.py` («да/нет» ["yes/no"], `/learned`), `console.py` (module in the command list), `__main__.py` (instantiation and startup).

## Console / Telegram
`learned` (list + log), `learned reflect`, `learned undo [text]`, `learned digest`, `learned yes|no`, `learned speak`.

## No new .env variables
The model is taken from `JARVIS_AGENT_MODEL`. Enabled if GEMINI_API_KEY and voice are present.

## Not tested live
Actual Gemini response to REFLECT_PROMPT (JSON format, quality of items), live sound of spontaneous utterances, Telegram «да/нет» dialog.
Tests: full server suite — 207 passed, 2 skipped.

## Remaining / at the boundaries
- "What's new" notification in the summary: call `brain.learning.digest()` from habits-ops.
- Mood is currently only a text string in the prompt (not sent to the screen).
- No voice command for «не слушай» ("don't listen"): initiative stays silent during `presence away` and pref `initiative=off`.
- The Live prompt is updated on a new session, so a new rule learned via voice takes effect starting from the next conversation.
