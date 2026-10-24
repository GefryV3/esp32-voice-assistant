# LED screen: demo status

The demo on the speaker shows only what is already redrawn in the LED style. Everything else waits here,
in the order we agreed to pick it up (mockups: `screen-rest.html`, face: `screen-variants-9.html`).

## Redrawn and shown in the demo

- [x] Idle: time in dots, date and temperature line, agenda cross-fade
- [x] Listening: full-rainbow edge wave, heard text (up to three lines)
- [x] Thinking: slow comet, quiet background rain
- [x] Speaking: white-to-cyan wave, reply (one phrase, big font)
- [x] Emotions (eye shapes): skeptic, surprise, sorry, tired, bored, roll, side (checked: skeptic, surprise)
- [x] Falling asleep (drowsy eyes, yawn) - approved
- [x] Happy (web mockup approved): thick arch eyes that wiggle and squash, warm colour, no mouth
- [x] Sleep screen (web mockup approved): thin breathing eye slits, dim red time, red "Z" drifting up-right
- [x] Timer (web mockup approved): big eyes, small digits under them, ring of dots that drains clockwise, amber pulse + edge wave when it fires
- [x] Phone cards (web mockup approved): message, incoming call, battery gauge, summary, before-sleep list, night remark, "get up"
- [x] Music player (web mockup approved): title, artist, dot progress bar between the eyes and the title
- [x] Reminders badge (web mockup approved): soft-shaped bell swaying above the eyes, count to its right
- [x] Quiet mode (web mockup approved): sleepy eyes and a crossed-out speaker, no label, no moon
- [x] Tasks (web mockup approved): working, needs input, done, failed
- [x] Games (web mockup approved): coin (toss + shadow), dice, wheel (lowered), rock-paper-scissors (real scissors), magic 8, tic-tac-toe (bigger, win line)
- [x] No-connection icon (web mockup approved): red Wi-Fi crossed out through the middle, clear of the eyes
- [x] Power-on sweep (only at boot)
- [x] Text outline instead of a backing plate; grid reaches the edge of the circle (r = 118)

## Not redrawn yet (to do, do not show in the demo)

- [ ] Reply with a light icon ("Свет включён" + bulb): web mockup drawn (`screen-rest.html`, "Мелочи"), waits for approval
- [ ] Heard/said text while a card or game is on screen (check the outline against card text)

Rejected: smile element (emotions are shown by the eyes only, no mouth).
Same look as "needs input" (no separate redraw): the `NeedsPermission` task state.

## Demo rules

- Send one state at a time and end every demo with `voice idle; say none; timer 0; sleep 180`.
- A game cannot be closed by a command: only a speaker reset (RTS on COM3) clears it. After a reset the server
  keeps the dead connection for about a minute.
