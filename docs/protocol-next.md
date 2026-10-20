# New Messages Protocol (2026-10-20)

Parallel sessions are developing the firmware and the server simultaneously. To ensure they align
upon merging, the formats of new messages are fixed here. The main protocol is: [protocol.md](protocol.md).
Unknown `type` values and fields are silently ignored by the receiver, so the rollout order does not matter.

## Server → Satellite

### `task_status`: progress of a long-running task (Claude Code and background agents)

```json
{"type": "task_status", "state": "working", "label": "Telegram-бот", "pct": 40}
```

- `state`: `idle` (clear), `working`, `needs_input` (Claude asked a question), `needs_permission`
  (waiting for permission), `done`, `failed`.
- `label`: up to 31 bytes of UTF-8, describing the task. `pct`: 0..100 or -1 (percentage unknown).
- Screen: `working`: progress ring along the edge (scrolling arc when -1), label at the bottom;
  `needs_input` / `needs_permission`: amber edge pulsing and the label «Ждёт тебя» ("Waiting for you");
  `done`: green flash, fades out after 4 s; `failed`: red. Lower priority than call and conversation,
  higher than clock and sleep (sleep is interrupted by `needs_*`).

### `quiet`: silent mode ("don't listen")

```json
{"type": "quiet", "on": true}
```

The satellite displays a dot and the label «Тишина» ("Silence"), eyes are calm (like in sleep, but do not fade). **The satellite microphone continues to send audio to the server**: the server itself listens locally only for the words of Maga and «вернись» ("come back"), audio is not sent to Gemini. This is required to wake the satellite back up by voice. The mode is disabled by the `quiet` message with `on: false`
(or `listen_start`). The night mode (`night`) is stronger: in it, the screen is dark, the dot is not drawn.

### `ble_scan` (server → satellite)

```json
{"type": "ble_scan", "on": 1, "uuid": "<32 hex>"}
```

Enable/disable scanning of the phone BLE beacon (iBeacon) with the specified UUID. Works only in the BLE firmware build
(`sdkconfig.ble`), in the regular build the message is silently ignored. Details: [room-presence.md](room-presence.md).

Optional fields (the satellite applies only the received ones, keeping the rest at defaults from `net/room.cpp`):

| Field | Default | Meaning |
|---|---|---|
| `win_ms` | 80 | scan window (10…500 ms); at night not less than 200 |
| `every_s` | 3 | scan period while "absent" or state is unstable (1…120 s) |
| `stable_every_s` | 6 | period when "here" holds for more than a minute and the last cycle heard the beacon |
| `night_every_s` | 15 | period at night (after the `night` message) |
| `silence_s` | 90 | beacon not heard for this many seconds (but not less than 15 scan periods) — "left" |
| `cal_s` | — | calibration: this many seconds of scanning once per second, window 200 ms, `room` on each cycle with fields `raw`, `heard` |

Scanning automatically pauses on weak Wi-Fi (< −80 dBm), while audio is playing, when there is no connection to the server, or during firmware updates; the pause time is not counted as beacon silence. Pause longer than 5 minutes → `room` with `src: "none"`.

### `light_flash` (optional, "mom is calling" bridge)

Not needed: lighting flashes are handled by the server via WLED/HA.

## Satellite → Server

### `clap`: claps

```json
{"type": "clap", "count": 2}
```

The satellite itself recognizes a series of claps (on device, without wake word and without network).
`count` 2 — toggle all lights in the room. The server acts upon receipt.

### `room`: "person near the satellite"

```json
{"type": "room", "here": true, "rssi": -58, "src": "ble"}
```

The satellite sends this upon state change and every 30 s. `src`: `ble` (phone beacon), `radar`
(presence sensor, if available), `none` (nothing to determine with). The server accounts for this in
`presence.py` as the strongest sign of "in the room".

Optional fields (old server ignores them): `heap_free`, `heap_min` — free internal satellite RAM and
minimum for the entire operation time, in bytes (BLE diagnostics, server console `room` command displays them); `raw` (RSSI of the last beacon packet in the cycle) and `heard` (how many packets) — only in calibration mode (`cal_s`).

## Satellite without server

The satellite keeps track of the clock, timer, and scheduled alarms itself if there is no connection. The server sends `plan` in advance
(may repeat on every connection):

```json
{"type": "plan", "alarms": [{"at": 1760940000, "label": "Подъём"}], "timers": []}
```

`at`: Unix time. Without connection, the alarm sounds with a built-in tone and is shown on the screen.
`alarms` — reminders and alarms, `timers` — timers; `label` up to 31 bytes, no more than 8 in the list.
The plan replaces the previous one entirely each time. The server sends it upon connection and upon any change.

### `config`: BLE thresholds

`{"type":"config","ble_near":-72,"ble_far":-80}` — beacon RSSI thresholds in dBm: "near" is above `ble_near`, "away" is below
`ble_far` (`ble_far` < `ble_near`). The server stores them (result of `room calibrate apply`) and sends them on every connection.

### `config`: claps

`{"type":"config","clap":1,"clap_sens":5}` — enable/disable the clap detector on the satellite and
sensitivity 1…10. The satellite applies only the received fields (sleep and claps arrive separately).
