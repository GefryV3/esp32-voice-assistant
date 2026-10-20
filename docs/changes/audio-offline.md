# audio-offline: Claps on the Speaker (#7) and Speaker Without Server (#33)

Branch `claude/audio-offline-836994`. The firmware has **not been flashed**, has not been uploaded to the add-on.

## 1. Claps on the Device

- `firmware/main/audio/clap.h` — pure logic without ESP-IDF (`clap::Detector` class). 5 ms blocks,
  sharp front above background (slow absolute mean ~1 s) + absolute minimum, high-frequency ratio
  (|x[n]-x[n-1]|/|x[n]| ≥ 0.45: speech is ≈0.1–0.3, Wi-Fi power clicks below 150 Hz are filtered out), decay to one-third
  within 50 ms (speech and music hold sustained energy), a series of two with a 0.25–0.8 s interval, a 1.5 s pause after triggering.
  While the speaker is playing (`s_speaking`, including the built-in tone) and for 0.6 s after, as well as during the first second
  after power-on, the detector stays silent. Operates without network or wake word (in `mic_task`, audio after the same
  100 Hz filter, but without user gain).
- Sensitivity 1…10 (default is 5): peak-above-background threshold `20 − 1.5·s`, minimum peak `6000 − 450·s`
  (at 5: peak ≥ 3750 out of 32768 and 12.5 times above background).
- Verification: `python firmware/tools/clap_sim.py` — the same algorithm in Python, 20 synthetic signals
  (two claps at 0.3/0.4/0.7 s, single clap, too long, echo, three in a row, noisy room, reverberation, quiet and
  medium claps across different sensitivity settings, silence, speech with "p" sounds for 8 s, loud speech, music with a kick drum,
  Wi-Fi power clicks, muting during playback, claps over speech). All pass. There is no C++ compiler on the host,
  hence the Python mirror; when editing `clap.h`, update `clap_sim.py` as well (the numbers are identical).
- Upon triggering, the speaker sends `{"type":"clap","count":2}` (from `status_task`, without blocking the microphone).
- Server (`server/jarvis/offline.py`, new module): config on connection (`clap`, `clap_sens`),
  receiving `clap`, subscription `hub.offline.on_clap.append(async def (dev, count))` — **the action (light) is handled by
  another session**, which hooks in here. Console: `clap on|off [1..10]`. Settings are saved to `brain` (prefs).
  `.env`: `JARVIS_CLAP=on|off` (default on), `JARVIS_CLAP_SENS=1..10` (5).
- `protocol.config(...)` gained the `clap` and `clap_sens` fields. **The firmware now applies only the received fields from `config`**:
  previously, any config reset sleep to 180 s.

## 2. Speaker Without Server

- Server: `protocol.plan(alarms, timers)`; `Brain.plan_rows()` collects upcoming reminders (into `alarms`)
  and timers from `set_timer` (into `timers`), up to 8 of each, labels ≤31 bytes. `Brain.plan_changed` is triggered on
  `add_reminder`/`cancel`/`finish`/`set_timer`; `Offline` broadcasts `plan` upon connection and on every
  change. Console: `plan` (send manually). Phone alarms (bridge) are not included in the plan yet.
- Firmware `firmware/main/net/plan.{h,cpp}`: the plan is stored in NVS (`plan` namespace), checked once per second.
  Due time reached and no connection (and not older than 5 min): `audio::alarm(40)` — "three beeps, pause" tone at 880/1175 Hz
  via the standard player (overall volume) + `Wake` card with the label and «Нет связи с сервером» ("No connection to server").
  Connection present — the speaker silently discards expired items (the server rings). Connection restored — the tone stops.
- Label: on disconnect, the bottom arc (`ui::set_agenda`) shows «Нет связи · HH:MM <label of nearest alert>» ("No connection · HH:MM <label of nearest alert>");
  when connection returns, the arc is cleared (the server should push its own agenda itself). The `face/` layer **was not modified**:
  the crossed-out Wi-Fi icon was already there.
- Limitation: the speaker clock tracks the server time. After a reboot **without** a server, the time is unknown
  (1970), and alerts will not trigger until a connection is established at least once.

## Build

`jarvis.bin` 0x128050 = 1,212,496 bytes out of 0x1c0000 (1,835,008) OTA slot, 0x97fb0 free (34%). No
warnings in my files. On this machine, the build failed with "out of memory" due to parallel sessions:
built with `ninja -C build -j2` in the idf.ps1 environment.

## Tests

Server: `server/tests/test_offline.py` (protocol, settings, plan from Brain, connection, push on change,
clap message, `clap`/`plan` console, settings persistence). Total: 205 passed, 2 skipped.

## Not Tested Live

- Clap thresholds are calibrated on synthetic data: a real INMP441 in a room may require `clap on <1..10>`
  (monitor whether it triggers on speech / misses claps). Log: «хлопки … включены» ("claps … enabled").
- Tone without connection, NVS plan, screen label, connection restoration — build only, not run.
- Firmware version was not bumped (coordinator).
