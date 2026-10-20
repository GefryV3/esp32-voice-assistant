# Installation Steps

A brief version is below. **The complete step-by-step guide (including obtaining all keys/tokens and troubleshooting common issues) is in `TUTORIAL.md`—start with that.**

1. `secrets.yaml.example` -> copy to `secrets.yaml` (both next to the ESPHome config and in HA `/config/`), fill it in.
2. Flash the ESP32-S3: `esphome run esphome/voice-satellite.yaml` (or add the config to the ESPHome Dashboard inside HA).
3. Copy `custom_component/gemini_assistant` to `/config/custom_components/gemini_assistant` on the laptop running HA.
4. Add the contents of `homeassistant/configuration_snippet.yaml` to the main `configuration.yaml`.
5. Place `memory/memory_template.json` into `/config/gemini_assistant/memory.json`.
6. Install the Whisper and Piper add-ons (Settings -> Add-ons -> Add-on Store), select them in Settings -> Voice Assistants -> pipeline along with the gemini_assistant agent.
7. Add automations from `homeassistant/automations.yaml` (via the UI editor or `!include automations.yaml`), adjust entity_id values (`person.you`, `calendar.your_google_calendar`, `esphome.home_voice_satellite_...`) to match your real system.
8. Create `input_boolean.assistant_dnd` (Settings -> Devices -> Helpers) for the "не беспокоить" ("do not disturb") mode.
9. Restart Home Assistant.

Screen state animations (`esphome/images/*.gif`) are ready-to-use working presets (ring/dots/spinner/equalizer). How to replace them with something prettier—Section 19 in `TUTORIAL.md`.

Notes: by default, the agent can write to Google Tasks. If you specifically want Google Keep, this is a separate optional step (unofficial gkeepapi library, Section 10 in `TUTORIAL.md`), which can be skipped without losing any other functionality.

A detailed description of the architecture and decisions is in `01_architecture.md`.
