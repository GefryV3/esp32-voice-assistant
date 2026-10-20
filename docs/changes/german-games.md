# german-games: German training (#4) and voice games (#25)

## What was done

**New modules** `server/jarvis/german.py`, `server/jarvis/games.py`; edits in `brain.py` (wiring, dispatcher, `end_conversation`, `long_talk`, `command`), `voice.py` (prompt, pause, reset, commands), `console.py` (help).

### German
- Gemini function `set_language_mode(mode=german|off, level, situation)`. The model receives mode rules in the **function result** (the prompt of an open Gemini Live session cannot be modified) and follows them until the end of the conversation. If the session opens with the mode already enabled (console `de on`), the same rules go into the system prompt: `system_prompt(language=...)` replaces «Говори только по-русски» ("Speak only in Russian") with a bilingual paragraph.
- The mode lasts for one conversation: `Voice._run` calls `brain.end_conversation()`. «Хватит немецкого» ("Enough German") = `mode=off`, this is not the end of the conversation.
- Default level is A2-B1 (pref `de_level`, changed by voice: «проще/сложнее» ("simpler/harder")).
- Error correction with a short rule in Russian, "word of the day" (once a day, pref `de_wotd`), roleplay scenarios (school, shop, transport, phone…), hint about the «Поучить немецкий» ("Learn German") task and completing it via `google_tasks_done` only after consent (if Google is connected).
- Table `de_words(word, translation, misses, last_seen, due)` + `key`, `hits`, `box`. Leitner boxes 0/1/3/7/14/30 days; error → box 0 and repeat after 10 minutes; questions prioritize the most confusing words first. Functions: `de_word_save`, `de_word_review`, `de_words_due`.
- Pauses between turns in German and games are longer (`LONG_FOLLOW_UP_S` = 14 s instead of 6).

### Games
- `game_start` (cities / twenty / quiz, `source=de` for a quiz on words from `de_words`), `game_move`, `game_answer`, `game_stop`, `game_scores`. The game is run by the model; the server tracks named cities (duplicates, "wrong letter"), counts questions in "20 questions" and quiz points, and writes the overall score to `game_scores` (brain.db).
- Screen: existing messages only: `caption reply` («Города: Анапа → А» ("Cities: Anapa → A"), «Викторина 2:1» ("Quiz 2:1"), final score) and `emotion`. No new types.

### Console
`de on [level]|off|words|due|add word = translation|ok|miss word|level|prompt`, `game score|reset|win|lose|draw <game>`.

## No new `.env` variables.

## Tests
`tests/test_german.py`, 23 tests: word storage, spaced repetition, mode prompts, games, score. Full suite: 218 passed, 2 skipped.

## Not tested live
How Gemini Live will actually behave: whether it follows rules from a function result, whether it distinguishes «хватит немецкого» ("enough German") from «хватит» ("enough"), whether it keeps the "hidden secret" in mind in 20 questions, German pronunciation with the chosen voice (another voice might be needed). The 14 s pauses also need to be tuned by ear. Deployment and flashing have not been done.

## Left to do / ideas
- Random scheduled questions (handled by another session): can call `brain.german.due_words()`.
- Displaying words on screen using a new message type (not designed yet, waiting for the display session).
