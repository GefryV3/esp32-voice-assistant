# Home AI Assistant on ESP32-S3 + Home Assistant + Gemini

## 1. Project Goal

A voice assistant on a round display that:

- listens and speaks via the built-in microphone/speaker;
- acts as a "live" conversational partner based on Gemini (by your choice), with access to Google services (Calendar, Gmail, etc.);
- initiates conversations with you on its own — reminds you about homework, doctor appointments, and other things without requiring you to wake it up every time;
- understands whether you are at home or not (via the standard Home Assistant device tracker), and if not, writes to Telegram instead of speaking into an empty room;
- stores long-term memory about you in a separate file that survives reboots and updates.

Key architectural decision: **not to write everything from scratch**, but to rely on the Home Assistant Assist Pipeline — HA's native voice pipeline, for which your ESP32-S3 microphone/speaker/display bundle is an almost exact replica of the official "Home Assistant Voice Preview Edition" device. This saves months of development on audio streaming, VAD, wake word, and synchronization.

## 2. Hardware (for reference, from your specification)

| Node | Model | Interface | Pins |
|---|---|---|---|
| MCU | ESP32-S3 Lolin Mini | — | Dual-core, Wi-Fi/BLE 5.0 |
| Display | GC9A01, round IPS 240x240 | SPI | CLK=12, MOSI=13, CS=16, DC=15, RST=14, MISO not used |
| Microphone | INMP441 | I2S (input) | SCK=9, WS=10, SD=11, LR→GND (left channel) |
| Amp+Speaker | MAX98357A | I2S (output) | BCLK=4, LRC=44, DIN=5, GAIN and SD not connected |

The microphone and speaker operate on **separate** I2S lines — the ESP32-S3 has two hardware I2S peripherals, so this is exactly what is needed: parallel recording and playback without bus conflicts.

## 3. General Architecture

```
[ESP32-S3 satellite]                         [Laptop: Home Assistant, 500GB]
 microphone INMP441 ──┐                        ┌─ Assist Pipeline (STT → Agent → TTS)
                      │  Wi-Fi, ESPHome API    │
 micro_wake_word ─────┼─── audio stream ──────►│
 (local, "Ok Nabu")     │                        │
                      │                        ├─ Custom Conversation Agent (Gemini)
 speaker MAX98357A ◄──┼─── audio stream ◄──────┤    ├─ Google Calendar / Gmail (tools)
                      │                        │    ├─ HA entities (tools: lights, sensors…)
 display GC9A01 ◄─────┼─── status/notices ─────┤    └─ memory.json (facts, reminders)
                      │                        │
                      │                        ├─ person.<you> (standard device_tracker)
                      │                        │     ├─ home  → announce on ESP32
                      │                        │     └─ away  → Telegram bot
                      │                        │
                      └────────────────────────┴─ Proactive scheduler
                                                      (checks calendar/memory on a timer,
                                                       decides "whether to say something now")
```

## 4. Components and Why It Is Done This Way

### 4.1 ESP32-S3 Firmware — ESPHome, Not a Scratch Arduino Sketch

ESPHome already knows how to:
- set up the I2S microphone and I2S speaker on different pins simultaneously (`i2s_audio` component, two independent buses — exactly your case);
- run a local wake word (`micro_wake_word`) right on the ESP32-S3 — audio does not leave the device until the word is triggered, which is a plus for privacy and latency;
- stream audio to Home Assistant via the internal API (`voice_assistant` component) — no separate audio intake server is needed;
- draw on the GC9A01 via `display` + `ili9xxx` (`GC9A01A` model) — assistant status (listening/thinking/speaking), clock, unread reminder icon.

File: `esphome/voice-satellite.yaml` (ready, see below).

### 4.2 Voice Pipeline — Home Assistant Assist Pipeline

Your HA is already set up, meaning: Settings → Voice Assistants → create a pipeline where:
- **Wake word**: on-device (`micro_wake_word` on the ESP32 itself);
- **STT**: Whisper (`Whisper` addon locally on the laptop — private and free, 500GB disk is more than enough for the model) or a cloud option if higher accuracy is needed;
- **Conversation agent**: our custom `gemini_assistant` component (see 4.3) instead of standard HA Assist;
- **TTS**: Piper (locally, free) — choice of a simpler/faster voice, since the "brain" belongs to Gemini anyway, not the voice.

### 4.3 "Live" Conversational Partner — Custom Conversation Agent on Gemini

The standard HA agent is a template engine for commands ("turn on the light"). To get a full-fledged dialogue, we install our own `custom_component` (`custom_components/gemini_assistant`) which:

1. Registers in HA as a `conversation` entity — then it can be selected in the pipeline as a regular agent;
2. Collects context on each request: recent utterances + relevant facts from `memory.json` + list of available tools;
3. Gives the model **function calling** for:
   - Google Calendar (create/read events — via official Google Calendar API, separate OAuth client of the project, not to be confused with my connectors in the chat);
   - Gmail (read/summarize emails, using the same method);
   - Google Tasks (write a note — "write down that I need to buy bread" (запиши, что мне нужно купить хлеб), visible later in the Google Tasks app on your phone; Google Keep has no public API for regular accounts, so we use Tasks as the closest working alternative);
   - HA entities (turn on light, query sensor — via `hass.services.async_call`);
   - "remember fact" / "set reminder" — writes to `memory.json`;
4. Returns text → HA passes through TTS → sound goes to the ESP32.

You will need to get a Gemini API key in Google AI Studio and put it in HA's `secrets.yaml` — this is the only step you will have to do manually (I cannot create accounts/keys for you).

### 4.4 Proactivity — "It Initiates Conversation with Me"

Three sources of initiative, all implemented via HA automations + service `assist_satellite.announce` (the current service for proactive speech on an assist_satellite entity starting from HA 2024.10; alternative is `tts.speak` on the satellite's media_player):

1. **On a schedule** (`time_pattern`, for example every 15–30 minutes): the script asks the Gemini agent "based on memory and calendar, is there anything to say now?" — if yes, it is spoken aloud / written to Telegram.
2. **By calendar event** (`calendar` trigger in HA, linked to Google Calendar): N minutes before the event — a reminder.
3. **By memory entry** ("remind me tomorrow to submit my homework" (напомни мне завтра сдать домашку)) — a separate reminder type with its own time, stored directly in `memory.json`, checked by the same script.

### 4.5 "At Home or Not" — Standard HA Tracker

You already confirmed that the HA device tracker is configured — so we use `person.<your_name>.state`:
- `home` → voice announcement on ESP32 (`announce`);
- `not_home` → `notify.telegram` + upon reply in Telegram, the `telegram_text` event is forwarded to the same Gemini agent so the conversation remains the same rather than being a separate stub bot.

### 4.6 Telegram as an Out-of-Home Channel

We use the built-in HA `telegram_bot` integration (no separate process/server needed):
- sending: `notify.telegram_<name>`;
- receiving: automation on the `telegram_text` event, text is forwarded to the `gemini_assistant` conversation agent using the same code as the voice — single memory, single logic.

You will need to create a bot via @BotFather in Telegram itself and insert the token into HA — this is also a step you do yourself (I do not input tokens/credentials for you).

### 4.7 Memory/Context

`memory.json` on the laptop's disk (500GB is more than enough for years of text notes):
```json
{
  "profile": { "name": "...", "timezone": "...", "notes": [] },
  "facts": [ { "text": "...", "added": "2026-07-07" } ],
  "reminders": [ { "text": "submit math homework", "due": "2026-07-08T18:00", "done": false } ],
  "conversation_summary": "compressed summary of old conversations to avoid bloating the prompt"
}
```
Once a day — a compactor script condenses old dialogues into `conversation_summary` so that the file does not grow infinitely and the prompt does not bloat.

## 5. What Is Required from You Manually (Cannot Be Automated)

1. Gemini API Key (Google AI Studio) → `secrets.yaml`.
2. OAuth credentials for Google Calendar/Gmail/Tasks API (Google Cloud Console, separate project, "Desktop app" or "Service account") → `secrets.yaml`.
3. Telegram bot token from @BotFather → `secrets.yaml`.
4. Flash ESP32-S3 (ESPHome CLI or ESPHome Dashboard in HA — just specify the config, then via USB/OTA).
5. In HA: Settings → Devices → add voice satellite, create Assist pipeline, specify our conversation agent.

## 6. Future Ideas (Beyond What Was Requested)

### Proactivity and Routines
- **Morning briefing**: at a set time — weather, upcoming calendar events, important unread emails, briefly "what's on the schedule today" — without a request, right like an alarm clock.
- **Evening digest**: what wasn't done during the day (overdue reminders), what's on the calendar for tomorrow.
- **"Do Not Disturb" mode**: night hours — do not speak aloud, but accumulate unread reminders and show a badge on the display by morning; emergency exceptions (see below) still go through.
- **Context-based reminders, not just time-based**: for example "remind me the next time I turn on the kitchen light" — not just `time_pattern`, but triggers on HA events.

### Security and Privacy
- **Emergency words**: if something resembling a health threat/alert is uttered in conversation — push to Telegram immediately, even if you are at home and "Do Not Disturb" is on.
- **Voice command "forget about this"**: deleting a specific fact/reminder from `memory.json` on request, without manually editing the file.
- **Local offline fallback**: if the internet is lost (Gemini unavailable) — a small local model via Ollama answers basic commands ("what time is it", "turn on the light"), full conversation returns when network is restored.

### Smart Home
- **Real actions, not just talk**: since HA is already there — the agent can pull `light.turn_on`, thermostat, sockets, etc., by voice, instead of just chatting.
- **Weather-dependent advice**: "take an umbrella" if rain is in the daily forecast, tied to the morning briefing.

### Personalization
- **Different voices/intonations**: Piper supports different TTS voices — quieter and softer for night reminders, regular during the day.
- **Multi-user (for the future)**: if there is more than one person in the house — speaker recognition by voice, so that reminders and memory are personal rather than shared by everyone.

### Infrastructure
- **Screen as a mini-dashboard**: clock/weather by default, active conversation animation (listening/thinking/speaking), badge for unprocessed reminder.
- **Multi-room**: HA Assist supports multiple voice satellites out of the box — if you add a similar device in another room later, the entire backend (agent, memory, Telegram) is reused without changes.
- **Privacy/price hybrid**: local Whisper+Piper for voice + Gemini only for the "brain" — cheaper and more private than sending raw audio to the cloud.
- **Memory backup**: `memory.json` + HA configs should be backed up (git repository on the same disk or Google Drive sync — fortunately Google access will already be available).
- **Network**: do not expose HA to the outside internet; Telegram works via outgoing polling/webhook, no need to open external ports.
- **Conversation continuity**: if Wi-Fi blinks and the voice session drops — the same dialogue (with the same memory) can be continued in Telegram with a single "continue in chat" button.

## 7. Files in This Project

- `esphome/voice-satellite.yaml` — firmware for your exact pinout.
- `homeassistant/automations.yaml` — presence routing, Telegram fallback, reminder checking.
- `homeassistant/configuration_snippet.yaml` — what to add to the main `configuration.yaml` (telegram_bot, intent_script, etc.).
- `custom_component/gemini_assistant/` — the conversation agent itself.
- `memory/memory_template.json` — starting memory file.
