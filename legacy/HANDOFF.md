# HANDOFF — Project Handover to Another Assistant

Document for the assistant who will continue the project. Here is what it is, what state the code is in, what has already been done, what remains, and what specific bugs/shortcomings exist in the current files. Read together with `01_architecture.md` (full architecture) and `TUTORIAL.md` (step-by-step installation). Date of creation: 2026-07-22.

> **UPDATE 2026-07-22 (next agent):** items from §6 are closed in the code:
> A (`due_texts` attribute in sensor.py), B (`gemini_assistant.mark_reminder_done` service
> + calls in automations.yaml), C (DND conditions in blocks 1 and 4), D (service
> `gemini_assistant.compact_history` + night automation at 03:30), E (profile
> and current time with timezone in the prompt), F (proactive polling every 30 min,
> block 6 of automations.yaml), G (Europe/Vienna timezone: profile → get_now(),
> timeZone in create_event, aware-time in list_upcoming_events), H (diagnostics
> removed, logger INFO). Remaining: I (`manifest.json` placeholders) and deployment
> to the live system (copy code to /config, update automations, reflash
> ESP32 OTA) — see chat history with the user. User confirmed: hardware
> works (answered by voice, controlled lights), lives in Vienna, Keep is needed.

---

## 1. What This Project Is (in Two Paragraphs)

A home voice AI assistant. Hardware — a custom ESP32-S3 satellite
(round GC9A01 240x240 display, INMP441 microphone, speaker via MAX98357A).
Brain — Home Assistant (on the user's laptop) + a custom conversation agent
on Gemini. Voice is processed locally (Whisper STT + Piper TTS), the "mind" is
cloud Gemini. The assistant speaks Russian.

Three key capabilities of the design: (1) live dialogue with access to Google Calendar /
Gmail / Tasks / Keep and smart home devices via function calling; (2)
proactivity — it reminds about tasks on its own without waiting for a wake word; (3) presence
routing — at home it speaks out loud to the speaker, away from home it writes to Telegram. Long-term memory
is stored in a flat `memory.json`.

Main architectural decision: not to write an audio pipeline from scratch, but to ride on the native
Home Assistant Assist Pipeline (the satellite looks to HA almost like the official
Voice Preview Edition).

---

## 2. Repository Map

```
assistant_project/
├── 01_architecture.md          Full architecture and justification of decisions
├── README.md                   Quick installation guide
├── TUTORIAL.md                 Detailed step-by-step installation (20 sections)
├── HANDOFF.md                  ← this file
├── secrets.yaml.example        Secrets template (wifi, gemini, telegram)
│
├── esphome/
│   ├── voice-satellite.yaml    ESP32-S3 firmware (ready, many hard-earned nuances — see §5)
│   └── images/                 idle/listening/thinking/speaking.gif (placeholder animations, BINARY)
│
├── homeassistant/
│   ├── automations.yaml        5 automation blocks (proactivity, presence, Telegram, DND)
│   └── configuration_snippet.yaml   What to insert into configuration.yaml
│
├── custom_component/gemini_assistant/   AGENT CODE (copied to /config/custom_components/)
│   ├── __init__.py             Integration setup from YAML, creating config entry
│   ├── config_flow.py          Technical import-flow (without UI)
│   ├── conversation.py         ★ The agent itself: Gemini function-calling loop
│   ├── google_tools.py         Calendar / Gmail / Tasks wrappers (OAuth Desktop app)
│   ├── keep_tools.py           Unofficial Google Keep via gkeepapi (optional)
│   ├── memory.py               MemoryStore — reading/writing memory.json
│   ├── sensor.py               Active reminders count sensor (for the badge on the display)
│   ├── get_keep_master_token.py   One-time Keep master token retrieval script
│   └── manifest.json           Pip dependencies, integration metadata
│
└── memory/
    └── memory_template.json    Starting memory (profile already with the name Inokentiy)
```

Separation of code and data is done intentionally: code lives in
`/config/custom_components/gemini_assistant/`, while memory is in
`/config/gemini_assistant/memory.json`, so that code updates do not overwrite memory.

---

## 3. Data Flow (How It Works Live)

1. `micro_wake_word` runs locally on ESP32 (the phrase «Окей Набу» ("Okay Nabu")). Before triggering,
   audio does not leave the device.
2. Upon `on_wake_word_detected` → `voice_assistant.start` → microphone stream to HA
   via ESPHome API.
3. HA Assist Pipeline: Whisper (STT) → **`gemini_assistant` conversation agent** → Piper (TTS).
4. The agent (`conversation.py::async_process`) gathers history + memory context +
   tool declarations, runs a manual function-calling loop with `gemini-2.0-flash`
   (up to 4 rounds), calls tools (`_dispatch_tool`), returns text.
5. HA passes the response through Piper → audio goes back to the satellite's speaker.
6. The display draws the state (`listening`/`thinking`/`speaking`/`idle`) + clock +
   reminders badge. The reminder count arrives as a text_sensor from
   `sensor.assistant_pending_reminders`.

Proactivity and presence work separately, via `automations.yaml` (not through the agent).

---

## 4. Status Against Initial Project Goals

Initial goals are from `01_architecture.md`, section 1. Verification against actual implementation:

| Initial Goal | Status | Comment |
|---|---|---|
| Listens and speaks (mic/speaker/display) | ✅ Code ready | Firmware written and polished; **not tested on live hardware** by the user |
| Live conversationalist on Gemini | ✅ Ready | `conversation.py` — full function-calling loop |
| Access to Google (Calendar/Gmail/Tasks/Keep) | ✅ Code ready | Requires manual OAuth setup by the user (keys/tokens) |
| Smart home control by voice | ✅ Ready | `call_ha_service` tool |
| **Initiates contact on its own (proactivity)** | ⚠️ Partially | Morning briefing — present. Reminders — **broken** (see §6, bug A). Proactive "is there anything to say?" scheduler from arch. 4.4 p.1 — **not implemented** |
| Understands home/away, writes to Telegram | ✅ Ready | Presence routing in `automations.yaml` (blocks 1, 3, 4) |
| Long-term memory surviving reboots | ✅ Mostly | `memory.json` works; but profile is not passed to the model and daily compaction is not implemented (see §6, bugs D, E) |

Conclusion: the core (dialogue + Google + smart home + presence + Telegram) is ready at the code level.
**Specifically, the proactive part is incomplete** — the very thing the "assistant initiative"
was conceived for: real voice reminders will not work due to a specific bug, and
periodic Gemini self-polling is not written at all.

---

## 5. What Requires Manual User Actions (Cannot Be Automated)

These are not bugs — these are fundamentally manual steps (accounts, keys, soldering). Details are in
`TUTORIAL.md`, here is a checklist so the new assistant understands what is beyond its control:

1. Solder and flash ESP32-S3 via USB (first time), then OTA.
2. Gemini API key (Google AI Studio) → HA `secrets.yaml` as `gemini_api_key`.
3. OAuth client **Desktop app** (Google Cloud Console) → `credentials.json` +
   one-time local login → `token.json`, both in `/config/gemini_assistant/`.
   Scopes: calendar, gmail.readonly, tasks.
4. Separate OAuth client **Web application** for Google Calendar integration
   *inside HA itself* (needed only for the `calendar` trigger in automations).
5. (Optional) Google Keep via gkeepapi: 2FA + App Password →
   `get_keep_master_token.py` → `keep_token.json`.
6. Telegram bot via @BotFather + your chat_id (@userinfobot) → `secrets.yaml`.
7. In HA: install Whisper and Piper addons, build Assist Pipeline, link the satellite.
8. Create helper `input_boolean.assistant_dnd`.
9. In `automations.yaml`, replace placeholders with your actual system entities:
   - `person.you` → real `person.<name>`;
   - `calendar.your_google_calendar` → real calendar entity;
   - `assist_satellite.jarvis_hub_assist_satellite` — **already replaced with the user's real**
     satellite entity (found via Developer Tools → States).

---

## 6. Unfinished Tasks and Bugs in Current Code (Crucial for Successor)

Sorted by importance. These are things that really need to be finished.

### BUG A — Real Voice Reminders Will Not Work (Mismatch Between Sensor and Automation) 🔴

`automations.yaml`, block 1 reads the attribute:
```yaml
due: "{{ state_attr('sensor.assistant_pending_reminders', 'due_texts') | default([]) }}"
```
However, `sensor.py` returns **only a number** (`native_value = pending_reminder_count()`),
and the `due_texts` attribute is completely missing there. Thus `due` is always `[]`, the condition
`due | length > 0` is always false → **reminders are never spoken aloud and never sent
to Telegram**. Only the badge number on the display works.

What to do: in `PendingRemindersSensor`, add `extra_state_attributes` with
`due_texts` — a list of texts from `MemoryStore.get_due_reminders()`. In `memory.py`,
the `get_due_reminders()` method already exists; you just need to pass its texts into the sensor attribute.
(And decide what the badge shows: all pending or only "due" ones.)

### BUG B — Reminders Are Not Marked as Done → Will Loop Infinitely 🔴

`memory.py::mark_reminder_done()` exists, but **is not called anywhere**. As soon as bug A
is fixed, block 1 (`time_pattern: /5`) will repeat the exact same reminder
every 5 minutes forever. After a successful announce/Telegram, you need to call
`mark_reminder_done(text)` — either via an agent tool or via an additional HA
service that calls `hass.data[DOMAIN]["memory"].mark_reminder_done(...)`.
Currently, there is no such service/tool — this needs to be written.

### SHORTCOMING C — "Do Not Disturb" Mode Is Not Connected to Anything 🟡

`automations.yaml`, blocks 5 turn `input_boolean.assistant_dnd` on/off based
on time (23:00 / 08:00). However, **not a single automation checks this flag** before
speaking out loud. At the end of the file, there is an explicit TODO comment: "Don't
forget to add `input_boolean.assistant_dnd == off` condition to step 1." That is, DND is currently decorative.
You need to add a condition to block 1 (and possibly block 4 — calendar reminders) so that at night
voice announcements are muted, while Telegram messages still go through.

### SHORTCOMING D — Daily Dialogue Compaction Is Not Implemented 🟡

`conversation.py::_trim_history` refers in a comment to a "daily task
(see automations.yaml)" that folds old conversations into
`conversation_summary`. **There is no such automation in `automations.yaml`.**
`memory.py::update_conversation_summary()` exists, but **is not called anywhere**.
Within a session, history is simply trimmed to 20 turns and old ones are lost
forever. You need to write a daily automation (or a separate scheduled task)
that asks Gemini to summarize old turns and stores the result in summary.

### SHORTCOMING E — User Profile Is Not Passed to the Model 🟡

`memory.py::as_prompt_context()` renders `facts`, `reminders`,
`conversation_summary`, but **not `profile`** (name, timezone, notes).
`memory_template.json` already defines `"name": "Inokentiy"`, but the model doesn't
see it. It is worth adding the profile to the prompt context (at least the name and timezone —
the latter is also important for correct `due_iso`/event dates).

### SHORTCOMING F — Proactive "Is There Anything to Say Now?" Scheduler Is Not Written 🟡

Arch. 4.4 p.1 describes periodic (every 15–30 min) self-polling: "based on
memory and calendar, is there anything to say?". In `automations.yaml`, there is only a morning
briefing (fixed at 07:30) and reminder checking by time. A full-fledged proactive
Gemini poll does not exist. This is a noticeable part of the "it reaches out to me on its own" concept.

### BUG G — Timezones in Google Tools 🟠

`google_tools.py::create_event` builds `dateTime` from `datetime.fromisoformat(start_iso)`
without specifying a timezone. Google Calendar API usually requires an offset or a separate
`timeZone` field — the event might shift/be rejected. `list_upcoming_events` uses
`utcnow()`. And `memory.get_due_reminders` compares against naive `datetime.now()`
(local time). Mixing naive/UTC should be unified with an explicit timezone
(which can be taken from the profile, see shortcoming E).

### CLEANUP H — Diagnostics and Logs in Firmware 🟢

`voice-satellite.yaml`:
- `interval: 2s` block (lines ~326–330) — explicitly marked "TEMPORARY for diagnostics,
  remove". It spams the log, needs to be deleted.
- `logger: level: DEBUG` with DEBUG on microphone/micro_wake_word/voice_assistant —
  on a production device, this should be lowered to INFO once everything works.

### COSMETICS I — Metadata Placeholders 🟢

`manifest.json`: `codeowners: ["@you"]`, `documentation: github.com/you`. Does not affect
operation, but should be fixed if the project goes to git.

---

## 7. Subtle Details That Are NOT Bugs (Do Not Touch Unless You Understand Why)

The `voice-satellite.yaml` firmware contains a dozen non-obvious solutions, each of
which fixes a specific symptom — they are commented in detail directly in the file.
Briefly, so the successor doesn't "fix" what works:

- `cpu_frequency: 240MHz` — otherwise wake word cannot keep up with the microphone.
- `psram:` block — without it, there is not enough RAM for wake word tensors, "Okay Nabu" silently ignores triggers.
- `wifi: hidden: true` inside `networks:` — the user has a hidden SSID.
- `power_save_mode: none` — cures random Wi-Fi drops on S3.
- `type: BINARY` animations — RGB565 overflows the flash partition (~1.8MB limit).
- Wake word model by **direct link to V1** (`okay_nabu.json@main`), not the short
  name `okay_nabu` — the short name resolves to V2, and V2 crashes due to memory limits (ESPHome bug #15603).
- `speaker: buffer_duration: 8000ms` — HA sends TTS in a burst faster than real-time;
  a smaller buffer cuts off the response.
- `speaker: timeout: 500ms` (not `never`) — otherwise VA gets stuck in RESPONSE_FINISHED.
- Starting the assistant via `on_wake_word_detected` + `micro_wake_word.start` in
  `on_end`/`on_error`/`on_client_connected` — the only working combination; do not replace
  with `start_continuous` / `use_wake_word`.
- Adaptive display redrawing (`interval: 40ms` with state division) — quick
  fix. `update` breaks sound ("Cannot receive audio, buffer is full").

In `conversation.py`: the agent is specifically `conversation.ConversationEntity` (not the obsolete
`AbstractConversationAgent`), otherwise it will not appear in the pipeline's dialog agents list.
The config entry is created via import-flow from YAML — this is also intentional.

---

## 8. Where to Start for the Successor

Recommended order (from most valuable):

1. **Fix reminders** (bugs A + B) — this unlocks the main proactive
   feature. Edits in `sensor.py` (add `due_texts` attribute) and add the
   `mark_reminder_done` mechanism after sending.
2. **Connect DND** (shortcoming C) — one condition in automations, quick.
3. **Pass profile to the model** (shortcoming E) — one edit in `memory.py`, along with
   handling the timezone (bug G).
4. **Write daily compaction** (shortcoming D) and **proactive polling** (shortcoming F)
   — these are new automations/scheduled tasks.
5. **Clean up firmware** (H) — when hardware operation is confirmed.

Important: almost everything above consists of code and YAML edits that can be done without hardware.
However, **final verification requires a real device and user-configured
keys** (§5) — an assistant cannot do this for the user. You should explicitly
ask the user to run section 18 of the TUTORIAL (testing) after making the edits.

---

## 9. Open Questions for the User (Worth Clarifying at the Start)

- Has the hardware already been flashed and answered by voice at least once, or is the project still in code only?
  (This determines whether to clean up diagnostics in the firmware and whether testing is even possible.)
- Are Google OAuth and Telegram already set up (is `token.json` present, does the bot respond)?
- Is Google Keep (`gkeepapi`) actually needed, or is Google Tasks sufficient?
- What is the user's timezone (needed for reminders and events, the
  `profile.timezone` field in memory.json is currently empty).
