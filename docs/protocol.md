=== File: protocol.md ===
# Column ⇄ Server Protocol

Version: **1**. Column is a WebSocket client, the server listens on `ws://<server>:8765/device`.

- **Text frames** — JSON object with a `type` field. Receivers silently ignore unknown `type`
  values and extra fields (so old firmware will survive a new server).
- **Binary frames** — audio (format in the "Audio" section).

Server code: [server/jarvis/protocol.py](../server/jarvis/protocol.py),
[server/jarvis/audio.py](../server/jarvis/audio.py).
Column code: [firmware/main/net/uplink.cpp](../firmware/main/net/uplink.cpp),
[firmware/main/audio/audio.cpp](../firmware/main/audio/audio.cpp).

## Handshake

The column sends `hello` as its first message, no later than 5 s after connecting:

```json
{"type": "hello", "proto": 1, "token": "…", "device": "jarvis-hub",
 "fw": "0.1.0", "caps": ["display"]}
```

`caps` — firmware capabilities: `display`, `mic`, `speaker`, `wake`.

The server responds:

```json
{"type": "welcome", "proto": 1, "server": "0.1.0",
 "time": 1760700000, "tz": "CET-1CEST,M3.5.0,M10.5.0/3"}
```

`time` — Unix time in seconds, `tz` — POSIX TZ string (the column sets its clock using it).

Handshake errors — the server closes the connection with code:

| Code | Reason |
|---|---|
| 4001 | invalid token |
| 4002 | first message was not `hello`, malformed JSON, or nothing received within 5 s |
| 4003 | unsupported `proto` version |

The column reconnects automatically every 3 s. Every 10 s it sends a WebSocket ping; if no
response is received for 30 s, it considers the connection lost and reconnects.

## Column → Server

| `type` | Fields | When |
|---|---|---|
| `status` | `uptime` (s), `heap` (bytes), `psram` (bytes), `rssi` (dBm), `fps` | every 30 s and immediately after `welcome` |
| `pong` | `id` | response to `ping` |
| `played` | — | finished playing audio, see "Audio" |
| `barge_in` | — | user interrupted the response: column has already stopped talking, followed by microphone audio with a 240 ms pre-history (firmware 0.5.0+) |

## Server → Column: Screen

Appearance follows the mockup [design/screen-mockup.html](../design/screen-mockup.html).

| `type` | Fields | What it does |
|---|---|---|
| `voice` | `state`: `idle` / `listening` / `thinking` / `speaking` | conversation state: edge glow, gaze |
| `emotion` | `name`: `happy` / `skeptic` / `roll` / `side` / `surprise` / `sorry`; `ms` (default 2600) | character expression on top of the state |
| `caption` | `kind`: `heard` / `said` / `reply` / `none`; `text` (up to 127 bytes); `icon`: `none` / `light` | caption at the bottom during conversation: what was heard, what was understood, what was done |
| `timer` | `sec`: 0…86400; `label` (up to 31 bytes) | timer counted by the column itself; `sec` 0 — dismiss |
| `agenda` | `accent` (up to 47 bytes), `text` (up to 79 bytes) | next event along the bottom arc, `accent` — in color; both empty — remove |
| `config` | `sleep_after` (s, 10…86400), `sleep_clock`: `top` / `bottom` | sleep timeout and clock position during sleep |
| `weather` | `cond`: −1…7; `temp`: °C | weather at the top when idle (`cond` −1 — hide) |
| `night` | `on`: bool | night → sleep until conversation starts |
| `brightness` | `level`: 0…255 | software brightness |
| `game` | `name` + game fields, see below | new screen does not draw this yet |
| `reminders` | `count` | new screen does not draw this yet |
| `ping` | `id` | connection check |

Weather `cond`: 0 clear, 1 partly cloudy, 2 cloudy, 3 rain, 4 snow, 5 thunderstorm,
6 fog, 7 clear night.

`emotion` with former names `alarm` and `playful` is understood by the column as `surprise`
and `happy`.

The date along the top arc and the clock are drawn by the column itself using time from
`welcome`. After inactivity (`sleep_after`, default 180 s), the eyes go to sleep and the clock
moves to the top or bottom. Any event from the server (`voice`, `emotion`, `timer`) wakes it up.

### Screen Priority

From top to bottom, the first matching wins: triggered timer (9 s or until next event) →
conversation (`voice` not `idle`) → active timer → night → idle/sleep by inactivity.
Expression (`emotion`) overlays any of them.

### Cards and Player (Phone Bridge)

| `type` | Fields | What it does |
|---|---|---|
| `notify` | `kind`: `msg` / `call` / `battery` / `info` / `remark` / `wake` / `check`; `app` (up to 31 bytes), `title` (up to 60), `text` (up to 190), `ms` | card over the face: phone notification, call, battery, jab, "bedtime check" |
| `notify_hide` | — | remove card (call ended) |
| `player` | `state`: `playing` / `paused` / `none`; `title`, `artist`, `pos`, `dur` (seconds) | mini-player: what is playing on the phone |

A column with firmware lacking cards ignores these messages.

## Over-The-Air Firmware Update

The server distributes the image via regular HTTP on the same port:
`GET /firmware.bin?token=<token>`.

| Direction | Message | What it means |
|---|---|---|
| server → column | `{"type": "ota", "url": "/firmware.bin?token=…", "version": "0.4.0", "size": 1204784}` | download image from the same server (host and port from connection address) and reboot |
| column → server | `{"type": "ota_state", "state": "progress", "pct": 40}` | progress, every 10% |
| column → server | `{"type": "ota_state", "state": "done", "pct": 100}` | image verified and written, rebooting now |
| column → server | `{"type": "ota_state", "state": "error", "error": "ESP_ERR_…"}` | failed, previous version remains working |

New firmware is loaded "on trial" and confirms itself only after receiving `welcome` from
the server. If it fails to connect within 120 s, the bootloader rolls back to the previous one.
The column sends `hello` with the new version in `fw` to the server — this is how the server
realizes the update succeeded.

## Phone and Computer

The phone app ([phone/](../phone)) connects to `ws://<server>:8765/phone`, the PC program
([pc/agent.py](../pc/agent.py)) to `/pc`. Handshake is the same as for the column (`hello` with
the same token, `device`: `phone` / `pc`), response is `welcome`.
Logic — [server/jarvis/bridge.py](../server/jarvis/bridge.py).

**Phone → Server**

| `type` | Fields | When |
|---|---|---|
| `state` | `screen`, `locked`, `charging`, `battery`, `unlocked_at` (ms), `next_alarm` (ms, 0 — none), `dnd` | on any change |
| `notif` | `key`, `app`, `app_name`, `title`, `text`, `category`, `can_reply`, `actions`, `when` | new notification |
| `notif_gone` | `key` | notification dismissed |
| `call` | `state`: `ringing` / `active` / `idle`; `number`, `name` | call |
| `media` | `state`, `title`, `artist`, `app`, `pos`, `dur` (ms), `at`, `palette` | track change or pause. `palette` — up to 3 main album art colors `["#RRGGBB", …]` (Palette API; no art — field omitted). The server uses them to tint the desk backlight (`music_light`) |
| `alarm` | `event`: `ringing` / `dismissed` | alarm ringing / dismissed |
| `alarm_set` | `hour`, `minute`, `direct` | response to `set_alarm`: `direct` — clock opened without screen, otherwise notification "tap" shown |
| `bt` | `devices`: [{`name`, `level`}] | battery level of connected headphones |
| `share` | `text` | "Share → Open on Computer" |
| `settings` | `values`: column settings | changed in the app |
| `test` | — | "Show notification on column" button |

**Server → Phone:** `settings` (`values`, `speaker`: {`online`, `fps`, `screen`}, `pc`), `ring`
(`on`), `reply` (`key`, `text`), `action` (`key`, `title`), `dismiss` (`key`), `media` (`cmd`:
`play` / `pause` / `toggle` / `next` / `prev`), `call` (`sms` — reject and text), `open_url`
(`url`), `set_alarm` (`hour`, `minute`, `label`: set alarm via AlarmClock).

**Computer → Server:** `pc` (`idle` — seconds without mouse and keyboard, `battery`, `charging`)
every 15 s; `to_phone` (`url`). **Server → Computer:** `open_url` (`url`).

`speaker.screen` — what is currently displayed on the column screen, derived by the server
from what it sent to it ([server/jarvis/screen.py](../server/jarvis/screen.py)): `mode` (`idle`,
`sleep`, `listening`, `thinking`, `speaking`, `timer`, `night`, `card`, `player`, `offline`),
`title` ("Слушает" ("Listening")), `detail`, `until` (ms, timer end). Sent again on every mode
change.

Notifications go to the column when the phone screen is off, it is not nighttime hours, and (if
"only at computer" is enabled) mouse or keyboard was touched in the last 2 minutes.

### Games (Old Screen)

| `name` | Fields | Duration |
|---|---|---|
| `coin` | `result`: 0 heads, 1 tails | 3.2 s |
| `dice` | `result`: 1…6 | 2.4 s |
| `wheel` | `count`: 2…8 sectors, `result`: 0…count−1 | 3.4 s |
| `rps` | `mine`: 0 rock, 1 paper, 2 scissors; `result`: 0 draw, 1 column won, 2 human won | 3.6 s |
| `magic8` | `answer`: 1…5 yes, 6…7 unclear, 8…10 no | 3.6 s |
| `ttt` | `board`: 9 characters from `X`, `O`, `.` (row by row) | 45 s or until next move |

## Audio

A column with a microphone and speaker writes `mic` and `speaker` into `caps`. Audio is PCM,
16-bit, mono, little-endian, in binary frames.

**Microphone.** The server sends `{"type": "listen_start", "gain": 12}` — the column starts
sending 16 kHz audio in 100 ms frames (3200 bytes); if the network was busy, accumulated audio
arrives in a single larger frame (up to 200 ms). Frame length is always even (whole
samples). Audio has already passed a 100 Hz filter. `{"type": "listen_stop"}` — stops
sending. `gain` — gain in dB, 0…30 (INMP441 has a quiet signal; default 12). The column
removes the DC offset itself.

**Speaker.**

| `type` | Fields | What it does |
|---|---|---|
| `audio_start` | `rate`: 8000…48000 (default 16000) | subsequent binary frames contain audio of any length |
| `audio_end` | — | audio finished; column plays out the buffer and replies `played` |
| `audio_stop` | — | silence immediately, discard buffer (interruption); response is also `played` |
| `barge` | `on` (0/1), `ratio_x10` (12…200, threshold ×10) | enable voice interruption: while the column is speaking, it listens to the microphone locally and, if the voice is noticeably louder than the echo of its own response (by `ratio_x10`/10 times) for longer than ~160 ms, it silences itself and sends `barge_in` |
| `volume` | `level`: 0…100 | volume level |

The column buffers 400 ms before starting and holds a 4 s buffer. The server sends audio no
more than 1 s ahead of real time — this ensures the buffer never overflows while allowing
the network to stutter for a second without audio glitches.

**Column → Server:** `{"type": "played"}` — played the last sample (or was stopped).

Later (stage 5): `{"type": "wake", "word": "hey_jarvis", "score": 0.97}` — column heard the
wake word.
