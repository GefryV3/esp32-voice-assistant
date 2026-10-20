# Architecture

## Diagram

```
┌──────────── Speaker (ESP32-S3, custom firmware) ────────────┐
│ microphone ─► wake word ─► audio stream ──────────┐         │
│ speaker    ◄─ reply audio ◄───────────────────┐   │         │
│ display    ◄─ eyes / emotions / games / weather ◄─┐│   │    │
└───────────────────────────────────────────────┼┼───┼────────┘
                    WebSocket (single channel)  ││   │
┌──────────────── Server (Python) ──────────────┼┼───┼────────┐
│ device hub ── screen commands ────────────────┘│   │        │
│            ── reply voice (24 kHz) ────────────┘   │        │
│            ◄─ microphone → wake word ──────────────┘        │
│                  └─► Gemini Live: audio ⇄ voice, transcript │
│ brain: Gemini + tools (memory, reminders, games,            │
│        news, smart home)                                    │
│ scheduler: briefing, reminders, proactivity                 │
│ Telegram bot                                                │
└──────────────┬───────────────────────────┬──────────────────┘
               │ WebSocket API + token     │ HTTPS
        Home Assistant                Gemini, Edge TTS,
   (lights, sensors, who's home,      Telegram, Google
    weather, calendar)
```

## Principles

1. **All logic is in plain code.** No YAML automations or Jinja templates: schedules,
   event reactions, and dialogue are Python code with tests. HA contains no code of ours whatsoever.
2. **The speaker is "thin", but alive.** The firmware can: listen for the wake word, stream audio,
   draw the face and animations. Decisions (what to say, what to show) are made by the server.
   Animations are computed on the speaker itself — the server only sends "show the die, rolled a 5".
3. **A single communication channel.** The speaker is a WebSocket client. Text frames are JSON commands,
   binary frames are audio. The protocol is described in [protocol.md](protocol.md) and versioned.
4. **HA is a proxy, not the brain.** The server accesses HA via WebSocket API with a long-lived
   token: reads states, calls services, subscribes to events. If HA
   is unavailable, the assistant still talks — it just doesn't control the home.
5. **Every stage ends with a working result**, see [roadmap.md](roadmap.md).

## Firmware (`firmware/`)

C++17 on ESP-IDF 5.5, without Arduino or ESPHome. Modules:

| Module | What it does |
|---|---|
| `board.h` | hardware pins and constants |
| `display/lcd` | GC9A01 via `esp_lcd`, non-blocking frame slice DMA transfer |
| `face/anim` | face behavior according to the mockup [design/screen-mockup.html](../design/screen-mockup.html): springs, blinking, falling asleep, expressions |
| `face/render` | frame rendering in 24-line slices: live background, edge glow, eyes, clock, captions; 16-bit with dithering |
| `face/text` + `fonts_data` | Inter font converted into anti-aliased masks (`tools/make_fonts.py`) |
| `ui/ui` | screen state (voice, expression, timer, captions, night) and render task up to 60 fps |
| `net/wifi` | Wi-Fi connection and reconnects; fallback network (`WIFI_SSID_2`) after 3 failures |
| `net/uplink` | WebSocket client: hello, server commands, status, bidirectional audio |
| `net/ota` | over-the-air updates: downloads image from server via HTTP, rollback if the new firmware fails to reach the server within 120 s |
| `audio/audio` | microphone (I2S0, 32-bit → 16, 100 Hz HPF, gain, sent to network in 100 ms chunks) and speaker (I2S1, 4 s buffer in PSRAM, volume) |
| `wake/*` | (stage 5) wake word on the speaker (microWakeWord, TFLite Micro) |

FreeRTOS tasks: rendering — core 1 (`ui`), assisted by `face2` on core 0 with the lowest
priority: the frame is divided into 15 slices, each core takes the next available one. Slices
are sent to the display in order by `lcd_send` (core 1) so SPI waits don't stall the cores.
Wi-Fi and networking — core 0; audio (`mic`, `mic_send`, `player`) — core 0 with high priority,
it always preempts the assistant. The microphone is read continuously, frames are sent to the
network via a dedicated task and a 1 s buffer — network latency doesn't break DMA. Speaker frames
arrive in the WebSocket task (which holds the client lock), so they are pushed to the buffer without blocking. Screen state is protected by a mutex: network only changes "what to display".

Speed (2026-10-18): idle and timer — 62 fps (ceiling, one frame every 16 ms), talking
with edge glow — 41–47. Every 5 s, a breakdown of frame time by cores and passes is logged.

Time: server sends Unix time and time zone in `welcome` — the speaker doesn't need
internet access or SNTP.

## Server (`server/`)

Python 3.12+, asyncio. Package `jarvis`:

| Module | What it does |
|---|---|
| `config` | settings from environment variables / `.env` (in the add-on — from options.json) |
| `protocol` | message types and validation — the single source of truth for the format |
| `devices` | WebSocket server for speakers, device session, sending commands |
| `console` | (dev) keyboard commands: show expression, roll a die, etc. |
| `audio` | recording from speaker mic, playback with tempo, WAV, `rec`/`echo`/`beep`/`play` commands |
| `wake` | wake word: openWakeWord models on onnxruntime, downloaded to data/models |
| `gemini_live` | Gemini Live API client (WebSocket): 16 kHz audio upstream, 24 kHz and transcripts downstream |
| `ota` | serves firmware image via HTTP on the same port, `ota` command, and verification that the new version is running |
| `screen` | current state of the speaker display — for the phone app |
| `voice` | conversation: wake word detector on continuous mic stream → Live session → reply to speaker |
| `brain/*` | (stage 4) Gemini, tools, memory (SQLite) |
| `home/*` | (stage 4) HA WebSocket API client |
| `telegram/*` | (stage 4) bot |
| `scheduler` | (stage 4) reminders, briefing, proactivity |

Where it runs: development — on a Windows laptop; production — on the laptop with HA as
a local add-on (a separate Docker container, HA only launches it).

Add-on — `server/addon` (config.yaml + Dockerfile based on `python:3.13-alpine`). The
`tools/deploy_addon.py` script builds the add-on folder (the `jarvis` package, token from `.env`) and puts
it into `\\192.168.178.86\addons\jarvis` via the Samba share add-on. In HA: Settings →
Add-ons → Add-on Store → ⋮ → Check for updates → Jarvis → Install; after a new deployment —
"Rebuild". Port 8765 is mapped to the laptop with HA, speaker settings are in `/data`,
logs are in the add-on's "Log" tab, HA restarts it if the port stops responding.

## Wake Word

The goal is a custom Russian word. There is no off-the-shelf model for it, so it needs to be trained (synthetic recordings
from several Russian TTS voices + custom recordings from the speaker's microphone). This is a separate
task, so initially a pre-trained "Hey Jarvis" model is used: it is available both for the speaker
(microWakeWord) and for the server (openWakeWord).

## What Is Deliberately Omitted

- Cloud STT/TTS from Nabu Casa — the subscription is expired, and it is no longer needed.
- Our code inside HA — the old `custom_components/gemini_assistant` will be removed in stage 4.
