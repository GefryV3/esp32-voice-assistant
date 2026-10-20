# Stages

Each stage ends with something that can be seen or heard.
Marks: ✅ done · 🔨 in progress · ⬜ not started.

## 1. Skeleton 🔨
- ✅ Repository, documentation, v1 protocol.
- ✅ Server: WebSocket hub for speakers, handshake, screen commands, console, tests (33),
  fake speaker.
- ✅ Firmware: GC9A01 via `esp_lcd`, facial engine port, Wi-Fi, WebSocket client,
  clock synced from server. Built: 973 KB out of 1.75 MB slot.
- ✅ Flashing to the speaker via USB: Wi-Fi and server connection work.
- 🔨 New screen according to the mockup [design/screen-mockup.html](../design/screen-mockup.html):
  eyes with a clock, sleep mode, glow during speech, timer, captions, personality expressions,
  Inter font. Flashed; awaiting visual inspection (colors, mirroring, readability).
- ⬜ Games and reminder counter in the new style.
- ✅ Server permanently on a laptop with HA — local addon `server/addon`, deployment via
  `python tools/deploy_addon.py`. Speaker, computer, and phone connect to 192.168.178.86.

**Result:** eyes on the screen driven by its own firmware; the server can display expressions,
timers, weather, and conversation captions.

## 2. Audio ✅
- ✅ I2S microphone (INMP441) → server, WAV recording (`rec`, file also reaches the laptop
  via `tools/send.py`). Clean, speech −18 dBFS at 12 dB gain.
- ✅ Server → I2S speaker (MAX98357A), 4 s buffer in PSRAM, server sends no more than 1 s
  ahead, speaker replies with `played`. Volume in settings (`vol`).
- ✅ Full pipeline check: `echo 5` — record and play back on the speaker.
- ✅ Backup Wi-Fi network (range extender): speaker switches automatically.
- ✅ Distortion in the speaker: audio arrived slower than real-time — default TCP window
  (5.7 KB) during latency spikes through the extender (up to 300 ms). Window 32 KB, accumulation
  400 ms: 3 s of audio finish playing in 3.2 s instead of 6.4, recording during audio playback without losses.
- ✅ Interference "like on an old radio": every Wi-Fi packet is a current surge, microphone
  power sags, causing a click in the audio (almost all below 150 Hz). Microphone sends data in 100 ms
  batches (5 times fewer clicks) and cuts everything below 100 Hz: background in silence −45 dBFS instead of −33.
- ⬜ Optional hardware: 10 µF + 100 nF capacitor directly on VDD–GND at the INMP441.

**Result:** the server hears the speaker and can play audio on it.

## 3. First Conversation 🔨
- ✅ Wake word on the server: openWakeWord models "Hey Jarvis" on onnxruntime
  (`jarvis/wake.py`, without the openwakeword package), 1.4 ms per 80 ms of audio.
- ✅ Conversation via Gemini Live (`gemini-3.8-live`, Charon voice): audio out, voice
  back, end of phrase determined by Gemini itself, transcription of the phrase on screen. First response
  audio in 0.3–1 s. Continuation without wake word for 6 s. Commands `ask`, `wake`, `last`, `log`.
- ✅ While the speaker is talking, the microphone does not send data to the network (otherwise the amplifier crackles from Wi-Fi packets);
  basses below 160 Hz and peaks are cut, volume scale with a ceiling.
- ⬜ Test live with the wake word "Hey Jarvis" (currently tested with `ask` and recordings).

**Result:** "Hey Jarvis, привет" ("Hey Jarvis, hello") → voice response.

## 4. Brain Migration 🔨
- ✅ Gemini calls functions (`jarvis/brain.py`): remember/forget fact, timed reminder
  (speaker speaks on its own), list and cancel, timer on screen. `brain.db` database, facts
  injected at the beginning of the conversation. Server 0.5.0 deployed to addon; test live.
- ✅ Smart home via HA WebSocket (`jarvis/ha.py`): lights/outlets/curtains by name or
  room, brightness and color temperature, reading sensors. Server 0.6.0; test live.
- 🔨 (0.11.0 ready, awaiting Google keys: docs/google.md) Google services (calendar, mail, tasks) directly via Google API, separate from
  HA: custom OAuth on the server (legacy code: legacy/custom_component/gemini_assistant/google_tools.py
  and keep_tools.py). Calendar from HA (branch claude/calendar-weather-bfc6ce) is then not needed,
  weather from it remains.
- ✅ (0.12.0, test live) Speaker ignores requests while the user is not present: neither wake
  word nor ask triggers a conversation (`jarvis/presence.py`). Indicators: computer, phone,
  person.* in HA; does not turn off at night near the bed. Override via `presence auto|here|away`
  (console, Telegram, by voice «я ушёл» ("I'm leaving")). Open question: what does "place" mean — computer, home, or room.
- ✅ (0.10.0, test live) Full synchronization of Telegram chat with the assistant: one shared dialogue for voice and
  text. What is said to the speaker is visible in the chat (request and response), what is written in Telegram the assistant
  remembers in voice conversation and vice versa; shared history in `brain.db`, not separate
  sessions for the bot and the speaker. Replying to a message from Telegram can be voiced on the speaker if desired.
- 🔨 (0.12.0, test live) Migration of old memory: `server/tools/import_memory.py memory.json`
  (requires real memory.json from HA). Routines on the new server: morning briefing (upon alarm dismissal
  or at `JARVIS_BRIEFING_AT`) and home greeting (`jarvis/routines.py`). Other old
  routines: calendar event reminder 15 minutes prior and proactive polling — not migrated, pending decision.
- Smart home via HA WebSocket API (lights, who is home, weather, calendar).
- Telegram bot, morning briefing, proactivity, news, games, expressions.
- Delete old `gemini_assistant` and YAML automations from HA.

**Result:** everything the old assistant could do works without our code in HA.

## 5. Wake Word on the Speaker ⬜
- microWakeWord on TFLite Micro: speaker sends audio only after the wake word.
- Custom Russian wake word: data collection and model training.

## 6. Fine-Tuning ⬜
- 🔨 Over-the-air firmware update from the server: firmware 0.4.0 and server 0.4.0 are ready
  (`send.py ota`, rollback if the new one fails to connect to the server). First time 0.4.0 — via wire.
- 🔨 Remote access to home — Tailscale, instructions in [remote-access.md](remote-access.md).
- Server as an HA addon (Docker), auto-start — done earlier, in stage 1.
- Interrupting the response with a wake word, night mode, quiet hours.

## 7. Wave 2026-10-20 (0.13.0, all gathered, awaiting deployment and live testing)
Details on each task: [changes/](changes/), decisions on ideas: [ideas.md](ideas.md).
- ✅ Lights: «спокойной ночи» ("good night"), restoring colors after dawn, lights to music, claps → all lights.
- ✅ Screen: task progress and "waiting for you", games, eyes, "Silence". Sound: claps on the speaker, plan without server.
- ✅ Night and voice: quieter at night, emotions always, character by time of day, «не слушай» ("don't listen"), rollback on 503.
- ✅ German and voice games. Morning: WebUntis, gentle wake-up, bus, umbrella.
- ✅ Habits, ideas, letter, weekly summary and reserve, «как ты?» ("how are you?"), check word.
- ✅ Skills-plugins and executor on Claude Code, progress hook. "Alive" Maga: reflection, feedback.
- 🔨 In room: BLE behind a flag (separate build), radar as a fallback: keys and decision needed.
- ⬜ Later: #30 (temperature/CO₂ sensors), #32 (capacitor on microphone).
