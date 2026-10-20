# Light Scenarios (branch claude/lights-d8c500)

New modules: `server/jarvis/lightstate.py` (light snapshots), `server/jarvis/lights.py` (scenarios),
`server/tests/test_lights.py` (13 tests using HA and WLED mocks).

## What Was Done

1. **«Спокойной ночи» ("Good night") (#1)**: Gemini function `goodnight` and console command `goodnight`. Room lights fade out over 30 s
   (`light.turn_off` with `transition` to all room lights + direct WLED `{"on":false,"transition":300}`),
   with a check after 32 s: whatever is still on is turned off immediately, without transition (two attempts). Then `Voice.announce`
   with reminders: alarm from `bridge.phone_state.next_alarm`, phone not charging and battery low, tomorrow's first
   event via `google_calendar_list` (if no Google, but HA is available — via `home_calendar`). After speech finishes,
   the screen dims in steps of `brightness` 200→0, then `night on`, then `brightness 255` (for the morning).
   The function returns a response immediately (dimming runs in the background), `voice.py` ends the conversation after `goodnight`.
2. **Sunrise (#20)**: `lightstate.py` saves the state of turned-on lights once per minute (WLED `/json/state`:
   `seg` col/fx/sx/ix/pal/bri; dimmers: brightness), file `data/lightstate.json`. Sunrise is detected by
   `script.sunrise` in `on` state or by `last_triggered` of the automation with `id` 1756302078808 (younger than 15 min):
   while it runs, snapshots are frozen. When sunrise finishes and all lights are turned off, WLED lights are restored to
   `{"on":false,"seg":[...],"bri":...}`. **Did not modify the HA automation.** Limitation: dimmers (ESPHome) do not accept
   brightness while off; they only restore upon turning on (clap). Risk: if a snapshot happens during the
   first seconds of sunrise before detection (check interval is 20 s), white light will be saved. The most reliable solution would be
   to trigger an `input_boolean` flag in the automation before `script.sunrise`, but that requires modifying HA, which I didn't do.
3. **Claps (#7)**: message `clap` {count}; `count == 2` toggles all lights: if anything is on — smooth fade out
   over 3 s, otherwise turn on: at night warm (2200 K) at 15%, during the day to the saved state (`lightstate.apply`), lights without
   saved state — normal `turn_on`. 3 s debounce. Console: `clap [n]`. The night state is not captured in snapshots (held for 1 h).
   Did not touch firmware or `devices.py` (only the `hub.lights` field).
4. **Music-sync lights (#19)**: the `phone/` app (`NotifListener.kt`, `Media.coverPalette`, dependency
   `androidx.palette:palette-ktx:1.0.0`) adds a `palette` field (up to 3 hex values, cached per track) to the `media` message.
   `bridge.py` forwards the message to `Lights.on_media`. ONLY `light.podsvetka_stola` (WLED) is colored, effect
   «Palette» with palette «* Colors Only» (names looked up in `/json/effects` and `/json/palettes`, fallback IDs 65/5),
   speed 40, brightness ≤ 140/255, color change with `transition` 2 s, does not turn on at night. If music is paused for 20 s —
   the light reverts to its saved state (if it was off — turns off with its previous colors).
   Activation: Gemini function `music_light` (`on|off|status`), console `music_light on|off|status`; state
   is stored in `brain` pref `music_light` (disabled by default). Console `lights` displays lights and their saved states.

## Changes to Shared Files (minimal, additive)
`brain.py` (`goodnight`/`music_light` tools, `lights` field), `voice.py` (reminder and ending conversation
after `goodnight`), `console.py` (`hub.lights` module in the command loop), `bridge.py` (2 lines in `media`),
`devices.py` (`lights` field), `__main__.py` (instantiation and startup of `Lights`/`LightState`).

## .env Variables (optional)
`JARVIS_LIGHT_AREA` (room area_id, default `spalnia`), `JARVIS_SUNRISE_SCRIPT` (`script.sunrise`),
`JARVIS_SUNRISE_AUTOMATION_ID` (`1756302078808`), `JARVIS_MUSIC_LAMP` (`light.podsvetka_stola`).

## Not Tested Live
Everything was tested only with mock-based unit tests. To verify live: that WLED addresses are resolved from `configuration_url`
(`console: lights` will show "WLED 192.168.178.x"), that an off WLED accepts colors (`on:false` + `seg`),
how sunrise affects light coloring and that `script.sunrise`/`last_triggered` detect it properly, that `night`
after dimming does not wake the screen with brightness 255, album art colors on the real LED strip, sending `palette` from the phone.

## What Remains
«Лианы» ("Lianas") dimmers do not fade smoothly if ESPHome lacks transition support (forced off hard). Bedside lamp is often
unavailable: we skip it.
