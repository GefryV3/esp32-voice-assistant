# room-presence: «человек в комнате» ("person in room") signal

Branch `claude/room-presence-f80b97`. User manual: `docs/room-presence.md`.

## What was done

**Server** (all verified with mock tests, `tests/test_presence.py`):
- `presence.py`: `Presence.on_room` (`hub.on_message` subscription) stores the latest `room`. Rule applied immediately after manual override: `here=true` (fresher than 2 min) → present; `here=false` continuously for ≥ `JARVIS_ROOM_AWAY_MIN` (5) → «в комнате нет» ("not in room"), takes precedence over an active PC. At night (sleep window from the bridge), it does not turn off if HA does not report «нет дома» ("away") and the phone is charging. `src: none` / message older than 2 min → signal is not used.
- Console/Telegram: `room` (what the speaker sees), `ble on|off` (persisted in brain.db, applied upon connection).
- `protocol.ble_scan(on, uuid)` — new server→speaker message `{"type":"ble_scan","on":1,"uuid":"<32 hex>"}` (I did not add it to `docs/protocol-next.md`; add when merging).
- New `.env` variables: `JARVIS_ROOM_AWAY_MIN` (5), `JARVIS_BLE_UUID` (iBeacon UUID from the HA app).
- Tests: 202 passed, 2 skipped.

**Firmware** (`net/room.cpp`, compiles, **has not been flashed**):
- Passive NimBLE iBeacon scan (100 ms window once every 1 s ≈ 10% airtime), UUID filtering, EMA RSSI, hysteresis: nearby > −78 dBm, away < −85 dBm or not heard for 30 s. `room` is sent on state change and once every 30 s.
- LD2410C radar behind the `ROOM_RADAR` flag (`board.h`, 0): `parse_ld2410` frame parsing, UART1 256000, GPIO7/8, `src:"radar"`. Was not built with flag 1 and was not tested (no hardware).

## Real BLE numbers (NimBLE build, observer only, no GATT)

| | without BLE | with BLE |
|---|---|---|
| image (jarvis.bin) | 0x126e60 before my changes; standard build with room.cpp without BLE 0x1270b0 (+0.6 KB, 34% free) | 0x148820 (1.32 MB, 27% free) |
| IRAM `.text` (shares DIRAM with heap) | 104 735 | 126 911 (**+22 KB**) |
| `.bss` + `.data` | 89 720 | 91 604 (+1.9 KB) |

The main point: even with BLE turned off, the stack in the image consumes ~22 KB of internal RAM out of the ~90 KB available (~68 KB will remain), and on `ble on` the controller allocates even more dynamically (noticeably more, estimated at 30–50 KB, **not measured on real hardware**, the firmware log contains the line «NimBLE поднят, внутренняя RAM X → Y» ("NimBLE initialized, internal RAM X → Y")). Plus a shared radio with Wi-Fi on a weak repeater signal (memory `speaker-wifi-barge`).

## Decision: BLE is not included in the standard firmware

- `sdkconfig.defaults` — as before, without BT (no RAM wasted, behavior unchanged). In such a build, `ble_scan` only logs «собрана без BLE» ("built without BLE").
- BLE build is separate: `sdkconfig.ble` (command in its header: `build-ble` directory). The user enables it deliberately, then runs `ble on` on the server (off by default).
- What to verify on real hardware before enabling: how much internal RAM is free (`devices` → heap) before and after `ble on`; Wi-Fi/WebSocket drops and rssi during `ble on` (30 minutes, check the log); range: `room` with the phone in bed, in the corridor, in another room (thresholds −78/−85 were chosen arbitrarily, they need to be calibrated); how the BLE Transmitter behaves on OnePlus with the screen off (Android may throttle it, requires «без ограничений» ("unrestricted") battery mode). If RAM drops below ~40 KB or the connection drops — keep BLE disabled and use the radar.

## What is needed from the user

- UUID from the HA app (BLE Transmitter) → `JARVIS_BLE_UUID`.
- Decision on whether to try the BLE build (flashed by the coordinator), or go straight for the radar (purchasing LD2410C).

## Not done / not verified

- Nothing has been tested on real hardware; firmware has not been flashed; no C++ host tests (no compiler on the host).
- Radar code path was not compiled with `ROOM_RADAR=1`.
- No separate flag in the speaker console (`ble_scan` is only sent from the server).

Both builds (standard and `sdkconfig.ble`) compile without errors; the BLE image 0x148820 fits into the 0x1c0000 OTA slot (27% free). Compiling the BLE image on this machine sometimes fails intermittently ("Unable to load DLL"/as.exe) — rerun the command, ninja will continue.
