# LED screen: demo status

The demo on the speaker shows only what is already redrawn in the LED style. Everything else waits here,
in the order we agreed to pick it up (mockups: `screen-rest.html`, face: `screen-variants-9.html`).

## Redrawn and shown in the demo

- [x] Idle: time in dots, date and temperature line, agenda cross-fade
- [x] Listening: full-rainbow edge wave, heard text (up to three lines)
- [x] Thinking: slow comet, quiet background rain
- [x] Speaking: white-to-cyan wave, reply (one phrase, big font)
- [x] Emotions (eye shapes): skeptic, surprise, sorry, tired, bored, roll, side (checked: skeptic, surprise)
- [x] Timer: big dot digits, dot ring that counts down, label, amber pulse when it fires
- [x] Sleep: closed eyes, dim red time, floating "Z"
- [x] Power-on sweep (only at boot)
- [x] Text outline instead of a backing plate; grid reaches the edge of the circle (r = 118)

## Not redrawn yet (to do, do not show in the demo)

Emotions, redraw in the web mockup first:
- [ ] Happy: the current eye shape (top half with a concave cut) does not read as joy - redesign
- [ ] Smile: add a smile as a new element (an LED arc under the eyes), used with happy and friendly replies

Phone cards (old smooth drawing on top of the grid):
- [ ] Message card
- [ ] Incoming call (green edge wave)
- [ ] Battery card (LED cell gauge)
- [ ] Info summary
- [ ] Before-sleep check list (icons)
- [ ] Night remark (red mode)
- [ ] "Get up" alarm card

Other:
- [ ] Music player (title, artist, dot progress) - wave itself is done, text and progress are old
- [ ] Task: working / needs input / done / failed
- [ ] Games: coin, dice, wheel, rock-paper-scissors, magic 8, tic-tac-toe
- [ ] Reminders badge (bell + count at the top) - drawn in dots, return to it later together with the web mockup
- [ ] Quiet mode ("Тишина" label, still smooth text at the top)
- [ ] No-connection icon (red Wi-Fi, still smooth drawing at the top left)
- [ ] Heard/said text when a card or game is on screen (check the outline against card text)

## Demo rules

- Send one state at a time and end every demo with `voice idle; say none; timer 0; sleep 180`.
- A game cannot be closed by a command: only a speaker reset (RTS on COM3) clears it. After a reset the server
  keeps the dead connection for about a minute.
