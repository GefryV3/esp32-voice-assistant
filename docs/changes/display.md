# Screen: Tasks, Games, Eyes (display session)

Branch `claude/display-8a16f1`. Firmware is built, **not tested on the speaker** (flashing was not possible).

## Size and Resources
- `jarvis.bin` 0x12d870 = 1,230,960 bytes with a 0x1c0000 (1.75 MB) slot: 33% free.
- No new large buffers; internal RAM is barely touched (growth comes only from Input/Scene structures and animation state, hundreds of bytes; all tables remained as they were). Free RAM and frame rates on the speaker were not measured: after flashing, check the `кадры:` ("frames:") line in the log.
- Frame rate estimate: idle/conversation are unchanged (the task ring is a single ring_band strip; the microphone part is a few multiplications per 20 ms frame). Games are more expensive: the wheel and dice calculate pixels using a formula (~10–20k pixels), expecting 35–50 fps during a game, which is brief. Verify live.

## 1. task_status
- Server: `protocol.task_status(state, label, pct)` (label ≤31 bytes, pct −1…100), console `task <state> [pct] [label]`, tests in `tests/test_protocol.py`.
- Firmware: parsed in `net/uplink.cpp` → `ui::set_task`; R=115 ring along the edge: working — fraction or running arc with a tail (pct −1), needs_* — pulsing amber ring + edge pulse + «Ждёт тебя» ("Waiting for you"), done — green flash, fades after 4 s, failed — red double flash, fades after 6 s. Label and status are at the bottom, eyes shrink, clock is hidden.
- Priority: call/card/conversation/timer > game > task > player > clock and sleep. needs_* wakes from sleep; night mode hides the task. In sleep mode, working/done/failed are visible as a thin ring without a label. While waiting for you, the speaker does not go to sleep.

## 2. Games and Counter (#25)
File `firmware/main/face/games.inc` (included in render.cpp). Scene is centered, small eyes on top watch and react (in rock-paper-scissors they rejoice/get upset).
- coin (toss with parabola, flip, shadow, «О»/«Р», then «Орёл/Решка» ("Heads/Tails")), dice (dice with pips rotates and hops, faces flash by, settles), wheel (colored sectors, rotation with deceleration, pointer on top, winning sector highlighted), rps (fist rocks «раз… два… три!» ("one… two… three!"), then icon and outcome), magic8 (ball shakes, «8» switches to an answer colored by sentiment), ttt (marks with bounce, winning line).
- Counter `reminders`: pill badge with a bell and number below the clock.
- Old durations from protocol.md are preserved (coin 3.2 s, dice 2.4, wheel 3.4, rps 3.6, magic8 3.6, ttt 45 s).

## 3. Eyes (#26)
- `audio.cpp`: hearing is calculated using the on-speaker microphone (audio is not sent out): `mic_level()`, `bang_count()` (sharp loud sound, not counted while speaker is talking), `beat_count()`.
- Startle reaction to clap/knock (jolt, eyes widen, blink; in sleep, eyes half-open).
- Listening: shape and gaze follow the real voice level.
- Emotions: twitches on happy/surprise/sorry start, irony (second "eyebrow"), new `tired` and `bored` (**missing in server `EMOTIONS` — add during merge**, firmware understands them), spontaneous boredom during prolonged inactivity and tiredness after 23:00.
- Micro-gazes (±2 px), quick glance to the side and back, irregular blink, double blink, blink on sharp turn, slow blink when tired.
- Yawn before sleep (in Drowsy).
- Music (`player` playing): bounce on beats (microphone detects rhythm, otherwise steady ~107 bpm) and blink every 4th beat.

## Test Live
Everything. Especially clap/rhythm thresholds (`Ear` in audio.cpp: 6× background and ≥2200; rhythm 2.5× background), frame rates in games, ball text readability, task ring along the edge. Commands: `task working 40 Сборка`, `task needs_input`, `task done`, `task failed`, `coin 1`, `dice 4`, `wheel 6 2`, `rps 1 1`, `magic8 3`, `ttt XO.X.O...`, `rem 3`. Not done: HTML mockup for games; sending task_status from Claude Code (separate session).

## 4. quiet (Quiet Mode)
- `net/uplink.cpp`: `{"type":"quiet","on":bool}` → `ui::set_quiet`; `listen_start` also cancels quiet mode (per contract).
- Screen: dot + «Тишина» ("Quiet") on top, weather is hidden, clock stays, eyes are calm (eyelids .42, do not turn off and do not fall asleep). Lower priority than call/card/conversation/game/task; not drawn in night mode.
- Microphone stream to the server is untouched (nothing was changed in audio.cpp).
- Size after revision: 0x12efd0 = 1,233,872 bytes, 32% free. Not tested live: `mute` in console.
