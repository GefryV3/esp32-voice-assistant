# "I'm in the room": signal from the speaker

Maga (Maga) responds to the wake word only when you are nearby. The server used to guess this based on the computer, phone, and Home Assistant (`presence.py`). Now the speaker can say "a person is nearby" on its own: the `room` message (`docs/protocol-next.md`). This is the strongest indicator; manual `presence here|away` still takes precedence.

## How the server uses `room`

- `here=true` (fresh within 2 minutes) → "present", even if HA thinks you are not home.
- `here=false` longer than `JARVIS_ROOM_AWAY_MIN` minutes (default 5) → "not in the room", the wake word is ignored, just like when "away". Repeating `here=false` every 30 s does not reset the countdown.
- **At night** (sleep window from the bridge) "not seen" does not disable Maga if, according to HA, you are at home (HA does not say "not home") and the phone is charging: you might have left the phone in another room.
- `src` = `none`, or `room` messages are absent for longer than 2 minutes (speaker disconnected, BLE disabled) → the signal is not used, old rules apply.

## Option 1: Phone BLE beacon (free)

The phone (OnePlus 13s) transmits an iBeacon, and the speaker listens and determines whether it is nearby based on the signal strength (RSSI).

### Enable on the phone

1. Home Assistant App → Settings → Companion app → **Manage sensors** → **BLE Transmitter**.
2. Enable the sensor, grant Bluetooth permissions ("Nearby").
3. In the sensor settings: **Transmitting** enabled, **Transmit power** — High (if battery allows), **Advertise mode** — Low latency or Balanced.
4. Copy the beacon **UUID** (Identifier / UUID) right there. Major/Minor are not needed.
5. In `server/.env` of the addon, add `JARVIS_BLE_UUID=<this UUID>` (with or without hyphens) and restart the server. Without the UUID, the speaker doesn't know whose phone this is and will send `src: none`.

The phone must be in the room with Bluetooth enabled. The beacon consumes a little battery, but noticeably.

### Enable on the speaker

BLE is included in the standard firmware (the stack is trimmed down to "observer only", summary in `docs/changes/ble-tuning.md`), but by default it is **disabled**: until enabled, it consumes ~5 KB of internal RAM and doesn't touch the airwaves. The server console enables it:

```
ble on
ble off
```

The choice is saved and applied every time the speaker connects. The speaker logs internal RAM before/after startup and at least once a minute for the entire time; the same numbers are visible in `room` on the server. After `ble off`, the stack remains in memory until the speaker reboots (there is no full unload).

Scan and threshold settings (all optional, sensible defaults are set):

```
ble tune win_ms=80 every_s=3 stable_every_s=6 night_every_s=15 silence_s=90   # scan parameters, ble tune reset — reset
room calibrate          # 30 seconds of RSSI measurements, suggests "nearby"/"away" thresholds
room calibrate apply    # apply and save
```

Calibration: place the phone where it usually lies when you are in the room (nightstand, bed) and do not move it for 30 seconds.

### Check

Server console (or Telegram), command:

```
room
```

Response like `на месте (колонка видит человека (ble)); … колонка: здесь (ble, -58 дБм, 12 с назад)` ("present (speaker sees person (ble)); ... speaker: here (ble, -58 dBm, 12 s ago)").
What to look for: take the phone to another room — after ~5 minutes `room` will show "no", and Maga will stop responding to the wake word. Bring it back — it will return in 10–30 seconds. If `src: none` — no UUID or scanning is disabled; if "ago" was a long time ago — the speaker is not sending.

### How the speaker decides

Scans passively with short windows rather than continuously (so as not to hog the Wi-Fi airwaves):

- window 80 ms every 3 s (≈2.7% of airtime) while "away" or state is unstable; every 6 s when "here" holds for more than a minute; at night every 15 s with a 200 ms window;
- scan pause: weak speaker Wi-Fi (< −80 dBm, resumes above −77), while playing sound, no connection to the server (and 20 s after its return), firmware update in progress; pause time is not considered beacon silence; pause longer than 5 minutes → speaker sends `src: none` (server reverts to old rules);
- RSSI: median of three measurements (dampens single outliers) + smoothing. "Nearby" — two consecutive measurements above −78 dBm, "away" — three consecutive below −85 dBm or beacon not heard for 90 s (with rare scanning — at least 15 periods: at night ≈ 4 minutes). Mild beacon pauses with the screen off are not considered leaving; the server additionally waits for `JARVIS_ROOM_AWAY_MIN` minutes (5);
- `room` departs upon change and every 30 s, with optional `heap_free`/`heap_min`.

## Option 2 (fallback, not purchased): LD2410C presence radar

If the phone doesn't fit (you walk without it, battery), a millimeter-wave radar sees a person even when they are lying motionless (sleeping) — BLE is not afraid of this, but PIR sensors are.

**Connection to ESP32-S3** (3.3V logic, 5V power):

| LD2410C | Speaker |
|---|---|
| 5V | 5V (VBUS) |
| GND | GND |
| TX | GPIO7 (speaker RX) |
| RX | GPIO8 (speaker TX) |
| OUT | not needed (GPIO6 can be used as a reserve) |

GPIO7 and GPIO8 are not occupied in `board.h` (screen 12–16, microphone 9–11, speaker 4/5/44); check that they are brought out on your board before buying. UART: 256000 baud, 8N1.

**How to read.** The radar sends frames on its own: header `F4 F3 F2 F1`, length (2 bytes, LE), data, footer `F8 F7 F6 F5`. Main data frame (`length 0x0D`): `02 AA`, target state (0 none, 1 moving, 2 stationary, 3 both), moving distance (2 bytes, cm), energy (1), stationary distance (2), energy (1), total distance (2), `55 00`. "Person is present" = state ≠ 0; to prevent blinking, hold for 20 s after the last target.
Reading code is located in `firmware/main/net/room.cpp` (`parse_ld2410`), enabled only by the `JARVIS_ROOM_RADAR` flag (in `board.h`/`sdkconfig`, default 0) and sends `room` with `src: "radar"`.
The radar is pointed at the bed, the range in the HLK LD2410 app on the phone is adjustable (default up to 6 m — better to reduce it to the bed so as not to see the hallway).

## Fallback via Fritz!Box router (not implemented, description only)

Idea: do not touch the firmware at all. The router already knows which Wi-Fi access point the phone is connected to, and the server (addon in the same house) can ask the router about this. If the phone has its own Mesh node in the bedroom, then "phone connected to the bedroom node" ≈ "I am in the room".

**What Fritz!Box can do.** Via the TR-064 protocol (SOAP over HTTP, port 49000, or HTTPS on 49443, address `fritz.box` or router IP), there is the `Hosts:1` service: a list of home devices (`GetGenericHostEntry`, `GetSpecificHostEntry` by MAC), each has `Active` (currently on the network), `InterfaceType` (802.11 or Ethernet), IP, name. AVM extensions: `X_AVM-DE_GetHostListPath` returns the path to `hostlist.xml`, and `X_AVM-DE_GetMeshListPath` to **`meshlist.json`**, which describes the Mesh topology: nodes (router and repeaters), their connections, for each device — which node it is connected to, frequency band (2.4/5 GHz), speed, and signal level. Ready-made Python library: `fritzconnection` (`FritzHosts.get_mesh_topology()`); I haven't checked the field names of `meshlist.json` live and am writing from memory — check the actual response of your router before use.

**What is needed.**
1. Enable TR-064 in Fritz!Box: *Heimnetz → Netzwerk → Netzwerkeinstellungen → Weitere Einstellungen → Zugriff für Anwendungen zulassen* (not "Heimnetzfreigabe", but specifically "Zugriff für Anwendungen").
2. A separate router user (*System → FRITZ!Box-Benutzer*) with "FRITZ!Box Einstellungen" permission, read-only would be enough for us, but AVM does not make separate read-only permissions for TR-064: the password grants a lot. Therefore — a separate user, long password, only in `server/.env` (`JARVIS_FRITZ_USER`, `JARVIS_FRITZ_PASSWORD`), not in git.
3. Phone MAC (in phone Wi-Fi settings: "Device MAC address"; on Android 10+ it is random per network by default — persistent for this network, which is fine, but if you reset the network, it will change) and the bedroom Mesh node.
4. Polling every 30–60 s: request `meshlist.json`, find the device by MAC, check the node and `Active`. Load on the router and airwaves is negligible, does not affect the speaker at all.

**Pros.**
- Nothing is flashed and it doesn't take up speaker RAM/airtime; speaker Wi-Fi under a weak signal does not suffer.
- The phone doesn't need a BLE beacon, nothing needs to be enabled in the HA app.
- Responds only to the target phone (by MAC), other phones in the house do not interfere.

**Cons.**
- Works **only if the repeater is Fritz!** (Fritz!Repeater/Fritz!Box in Mesh mode). A third-party repeater is not seen by the router as a Mesh node: the phone will be "connected to the router" or to an "unknown client". Which repeater `HomeWiFi_EXT` uses, I do not know.
- Accuracy — "which node it is attached to", not "which room". The phone holds onto the repeater until the signal drops below the switching threshold, so from the next room it may remain on the same node. If there is no separate node in the bedroom, the method is useless.
- Latency: the router does not update data instantly, node switching is done by the phone itself with a delay of tens of seconds. Screen off — phone Wi-Fi may fall asleep and "disappear" from the list (this is a false "away" for the night; Android's "Keep Wi-Fi on during sleep" mode is needed for this network).
- Router password required on the server (stored only in `.env`) and working TR-064; router firmware may change the `meshlist.json` format.

**Conclusion.** A good fallback path if BLE on the speaker turns out to be too heavy for its memory and airwaves and if the repeater in the bedroom is a Fritz!. Before writing code, check two things: 1) in the web interface *Heimnetz → Mesh*, the phone is shown on the bedroom node and switches when moving from room to room; 2) TR-064 responds (`http://fritz.box:49000/tr64desc.xml`). If both checks are "yes", implementation is a single server module of 100 lines passing the same signal to `presence.py` as `room` (source `src: "router"`).

## BLE risks (see reports `docs/changes/room-presence.md`, `docs/changes/ble-tuning.md`)

BLE and Wi-Fi share a single ESP32-S3 radio, and the repeater signal for the speaker in the kitchen is already weak (−65…−82 dBm), connection dropped. Therefore, BLE is disabled by default, scans are short and rare (1–3% of the time, pauses during weak Wi-Fi and audio). Enable `ble on` and check `devices` (rssi, heap) and connection drops in the log; if it drops — `ble off` and consider the radar.
