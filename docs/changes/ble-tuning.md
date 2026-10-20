# ble-tuning: BLE beacon without memory or Wi-Fi degradation

Branch `claude/ble-tuning-bf7fab`. User guide: `docs/room-presence.md`. Message contract:
`docs/protocol-next.md` (`ble_scan`, `room`, `config`).

## Bottom line in one sentence

The BLE stack has been trimmed down to "observer only" and **enabled in the standard firmware** (`sdkconfig.defaults`), disabled by default. When disabled, it costs ~4.7 KB of internal RAM (down from 24 KB); when enabled, estimated at 25–35 KB for the controller (**not measured on hardware**). The scan is short, infrequent, and automatically pauses on weak Wi-Fi, audio playback, connection loss, and OTA.

## Figures (build `.\idf.ps1 build`, `esp_idf_size build\jarvis.map`)

| | Standard 0.6.0 (no BLE) | BLE build before (previous `sdkconfig.ble`) | **BLE after (now standard)** |
|---|---|---|---|
| `jarvis.bin` image | 0x12f230 (1.24 MB, 32% free) | 0x150860 (1.38 MB, 25%) | **0x15a0f0 (1.42 MB, 23%)** |
| IRAM `.text` (shares DIRAM with heap), bytes | 104,735 | 126,911 | **107,443** |
| `.data` + `.bss`, bytes | 24,880 + 67,568 = 92,448 | 25,876 + 68,448 = 94,324 | 25,952 + 68,496 = **94,448** |
| Total D/IRAM static, bytes | 198,211 | 222,263 (**+24,052**) | **202,919 (+4,708)** |
| Controller code `libbtdm_app`, IRAM | — | 20,107 | **1,053** (`libbtdm_app_flash`: code moved to flash, +39 KB flash) |
| Flash (`.text`+`.rodata`), bytes | 1,110,868 | 1,224,460 | 1,282,900 |

The image grows by 175 KB, but still fits into the 0x1c0000 OTA slot (23% remains). Size breakdown files: `size-components` shows remaining in IRAM are `libbtdm_app_flash` 1 KB, `libbt` 0.8 KB, `libcoexist` 0.2 KB, `libbtbb` 0.3 KB.

Heap estimate (no hardware measurements yet; baseline "~90 KB free" taken from memory in the `speaker-wifi-barge`/`screen-design` project):

| State | Internal RAM free (estimate) |
|---|---|
| Firmware without BLE (0.6.0) | ~90 KB |
| BLE in image, **disabled** | ~85 KB (−4.7 KB static) |
| BLE **enabled** | ~47–57 KB (−4.7 KB, −25…35 KB controller and its queues, −3 KB host stack) |

The NimBLE host is entirely in PSRAM (`MEM_ALLOC_MODE_EXTERNAL`); only its task stack (3 KB) and what the controller allocates with `MALLOC_CAP_INTERNAL|DMA` remain internal (hardcoded in `bt.c` sources, controller cannot be moved to PSRAM).

## What was done in configuration (from ESP-IDF 5.5.5 sources, not from memory)

| Option | Effect |
|---|---|
| `BT_CTRL_RUN_IN_FLASH_ONLY=y` (`libbtdm_app_flash.a` exists for S3) | **Main change**: IRAM −19 KB, flash +39 KB. Controller interrupt handlers do not use `ESP_INTR_FLAG_IRAM` (verified in `bt.c`), meaning they are masked during flash writes and will not crash; BLE may drop frames during OTA, which is why scanning is paused during OTA |
| `BT_NIMBLE_ROLE_*` only `OBSERVER`, `GATT_CLIENT/SERVER=n`, `SECURITY_ENABLE=n`, `HS_PVCY=n`, `50_FEATURE_SUPPORT=n` | Less host code in flash and fewer static tables; no connections, `MAX_CONNECTIONS` hidden |
| `BT_NIMBLE_MSYS_1_BLOCK_COUNT=8`, `MSYS_2=0`, `TRANSPORT_ACL_FROM_LL_COUNT=4`, `EVT_COUNT=8`, `EVT_DISCARD_COUNT=6`, `WHITELIST_SIZE=1`, `MAX_CCCDS=1` | Minimal host pools (located in PSRAM, so this is mainly hygiene for internal RAM) |
| `BT_NIMBLE_LOG_LEVEL_NONE=y`, `BT_NIMBLE_GAP_SERVICE=n` | Host logging and GAP service not needed |
| `BT_CTRL_BLE_MAX_ACT=1` | ~828 bytes per controller "activity" (per Kconfig); one remains — scan |
| `BT_CTRL_BLE_MASTER=n`, `BT_CTRL_BLE_ADV=n`, `BT_CTRL_BLE_SECURITY_ENABLE=n`, `BT_CTRL_DTM_ENABLE=n` | Controller feature flags; small effect on prebuilt library size, **not verified on hardware whether scanning works with ADV/MASTER disabled** (see "What to verify") |
| `BT_CTRL_BLE_SCAN_DUPL=y`, type DEVICE, cache 20 | Controller filters duplicates itself: beacon reported once per window |
| `BT_CTRL_BLE_ADV_REPORT_FLOW_CTRL_NUM=50` | Host stores at most 50 reports (35 B each), down from 100 |
| `ESP_COEX_SW_COEXIST_ENABLE=y` (defaults to on with Wi-Fi + BT) | Software RF coexistence; `ESP_COEX_POWER_MANAGEMENT` left at `n`: speaker Wi-Fi has no power saving (`WIFI_PS_NONE`) |

What was **not** done: hardware support for moving the controller to PSRAM does not exist in IDF for S3; full stack unloading after `ble off` is not implemented (`nimble_port_deinit` is not called, cannot be tested) — memory remains allocated until reboot.

## Wi-Fi coexistence (firmware, `net/room.cpp`)

- Passive scan in short windows via `ble_gap_disc(duration_ms)` instead of continuous scanning: 80 ms window every 3 s (2.7% duty cycle), when settled at "here" (≥60 s and the last cycle heard the beacon) every 6 s (1.3%), at night (`night`) every 15 s with a 200 ms window (to catch the 100–250 ms advertising interval on Android), falls back to 3 s after a missed cycle.
- Scan pause and in-flight cancellation: speaker Wi-Fi < −80 dBm (resumes > −77), speaker playing audio (`audio::playing()`), no connection to server and for 20 s after reconnect, OTA in progress. Pause time offsets "last heard" time, so it is not counted as beacon silence; pause longer than 5 minutes → `room` with `src: none`.
- Everything is parameterized via `ble_scan` (`win_ms`, `every_s`, `stable_every_s`, `night_every_s`, `silence_s`, `cal_s`), server console `ble tune …` (persisted, applied on connect).
- The speaker microphone is streaming almost continuously based on `keep_listening` (wake word runs on server), so "pause during speech" is tied to audio playback (`playing()`), not `listening`.

## Memory diagnostics

On `ble_scan on`: logs once "NimBLE running, internal RAM X → Y (min Z)", once on first scan, then every minute "free / all-time min" (`heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL)`, rather than `esp_get_minimum_free_heap_size()`, because the latter includes PSRAM). Same figures in `room` (optional `heap_free`, `heap_min`), console `room` shows "internal RAM free N KB, min M KB".

## Detection reliability

- Median of three measurements (a single outlier packet does not shift state), EMA 0.3; "near" requires two consecutive readings above threshold (first reading immediately), "away" requires three consecutive below; state is held between thresholds (hysteresis −78/−85 as before).
- "Silence" timeout = 90 s, but no fewer than 15 scan periods (at night ≈ 225 s): a phone on the nightstand with screen off and 250 ms advertising interval is still picked up, while false "away" states are suppressed by the server's 5-minute wait (`JARVIS_ROOM_AWAY_MIN`) and nighttime logic in `presence.py`.
- Calibration: console `room calibrate` sends `ble_scan` with `cal_s=30` (scans once per second, 200 ms window, `room` emitted every cycle with `raw`/`heard`), after 30 s calculates: "near" threshold = 10th percentile RSSI − 3 dBm, "away" = near − 7 dBm; `room calibrate apply` saves (`brain` pref `ble_thresh`) and sends `config {ble_near, ble_far}`, re-sent on every reconnect. If < 5 packets received — logs suggestions on what to check and suggests nothing.

## Server

- `jarvis/ble_calibrate.py` (new), changes to `presence.py`, `protocol.py` (additive): `ble_scan(…, win_ms=…, cal_s=…)`, `config(ble_near=…, ble_far=…)`, console `room` shows RAM, `room calibrate [apply]`, `ble tune [k=v …|reset]`.
- Tests: `tests/test_presence.py`, `tests/test_protocol.py` (mocks, no hardware): total 426 passed, 1 skipped.
- No new `.env` variables.

## User decision: BLE in standard firmware

**Yes, enabled in `sdkconfig.defaults`, disabled by default** (`ble on` from the server enables it). Rationale: static cost of the disabled stack is 4.7 KB (estimated free ~85 KB ≥ 60 KB threshold), image size 1.42 MB against 1.75 MB slot. Enabled — estimated ~47–57 KB, close to the threshold, so running `ble on` remains a deliberate user choice. A separate build is no longer needed; `sdkconfig.ble` is kept as an empty stub so legacy commands do not break.

Important for the coordinator: `firmware/sdkconfig` is generated and not tracked in git; it **will not pick up the new defaults** on its own: remove `firmware\sdkconfig` (and `sdkconfig.old`) or run `.\idf.ps1 fullclean` before building. I did not bump the firmware version.

Rollback if things go poorly on hardware (RAM < ~40 KB on `ble on` or connection drops): `ble off`; to remove completely — delete the "BLE beacon" block from `sdkconfig.defaults` (`room.cpp` code will build without BLE automatically).

## What the user must measure at home (not verified on hardware)

1. Flash, `ble off`: `devices` → heap (internal RAM) — expect ~85 KB; then `ble on`: observe in `room` "internal RAM free / min" and log lines "NimBLE running… X → Y". If free < 40 KB — `ble off`.
2. Whether scanning works with `BT_CTRL_BLE_ADV=n`/`MASTER=n` (expect `room`: `src: ble`, `rssi` present). If not — revert these two options back to `y` in `sdkconfig.defaults`, leave the rest.
3. 30 minutes with `ble on`: check logs for Wi-Fi/WebSocket disconnects and `rssi` in `devices`; check logs for "weak Wi-Fi" pauses — how frequent (threshold −80 dBm, kitchen signal fluctuates −65…−82: if scanning stays paused almost constantly, raise the threshold or use radar/Fritz!Box).
4. `room calibrate` in bed and in another room, `room calibrate apply`.
5. Phone screen off overnight: verify whether `room` stays stuck on "none" for longer than 5 minutes (OnePlus: "unrestricted" battery setting for HA app).
6. OTA with BLE enabled (flash writes while controller resides in flash): the speaker must survive and reconnect.

## Not done

- Full stack unloading on `ble off`; moving part of controller to PSRAM (not supported in IDF for S3).
- No host C++ tests (no host compiler available); `Filter`/scheduler logic verified only by code review and build.
- LD2410C radar untouched. Fritz!Box TR-064 documented in `docs/room-presence.md`, not implemented.
