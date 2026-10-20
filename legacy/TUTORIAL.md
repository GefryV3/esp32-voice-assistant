=== File: TUTORIAL.md ===
# Step-by-Step Installation — From Zero to a Working Assistant

Follow the numbers and do not skip anything on your first time. Wherever your personal input is needed (keys, tokens, IDs), it is explicitly stated where to get them.

---

## 0. What Should Be Ready in Advance

- ESP32-S3 (Lolin Mini) soldered according to the pinout from the specification, connected via USB-C to the computer (USB is mandatory for the first flashing, further updates will be over Wi-Fi).
- A laptop with Home Assistant already running (you confirmed that you have it).
- The same Wi-Fi network (2.4 GHz — ESP32-S3 does not work on 5 GHz networks; if you have a router with a single SSID for both bands, it's usually fine, but if they are separated, connect specifically to the 2.4 GHz network).
- Google Account (for Gemini API and Calendar/Gmail).
- Telegram on your phone.
- Files from `assistant_project/` (architecture is in `01_architecture.md`, everything else is in folders).

---

## 1. Hardware Check Before First Power-On

Check with a multimeter (or at least visually verify) against the table in `01_architecture.md`, section 2 — a single swapped I2S pin usually manifests as silence or noise instead of voice. Pay special attention to:

- The microphone LR is actually soldered to GND (not left floating) — otherwise the channel is unpredictable.
- The display MISO is connected to nothing — this is normal, ok9a01 operates in SPI mode without bus reading.

---

## 2. Installing ESPHome

Two options depending on how you installed Home Assistant:

**If HA Operating System or Supervised** (usually the case if you installed via the official image on a separate device or VM):
1. In HA: Settings → Add-ons → Add-on Store.
2. Find "ESPHome Device Builder" (or "ESPHome"), click Install.
3. Open its Web UI directly from the HA sidebar — this is the Dashboard, from now on you work in the browser.

**If HA Container / HA Core** (without Supervisor — on bare Docker/venv):
1. On the same laptop: `pip install esphome` (or via the separate Docker container `ghcr.io/esphome/esphome`).
2. Start the dashboard: `esphome dashboard /path/to/config-folder` — it will open at `http://localhost:6052`.

Further steps are identical regardless of the option — just through a different UI.

---

## 3. Prepare Values for secrets.yaml

This is still on the same laptop where Home Assistant is installed — a separate computer is not needed. You do not need to create the file manually yet (you will do that in the next step through the Dashboard interface) — just prepare 4 values here, you will need them immediately:

```yaml
wifi_ssid: "your_network"
wifi_password: "your_password"
api_encryption_key: "..."   # generate using the command below
ota_password: "..."         # come up with any long random password
```

Generate the encryption key in the laptop terminal (if you have the `esphome` CLI installed — Container/Core option) or it will be offered automatically via the "Generate" button right in the device creation wizard in the Dashboard (option with the add-on, see step 4):
```
esphome generate-encryption-key
```

---

## 4. First Flashing (via USB)

The ESPHome Dashboard interface does not have a file manager to "drop a file next to it" — all files (both `secrets.yaml` and the device config itself) are created and edited directly through the Dashboard web interface using buttons. Here are the step-by-step instructions:

1. Open the ESPHome Dashboard (via the HA sidebar if you installed it as an add-on, or `http://localhost:6052` if standalone).
2. Click **"+ NEW DEVICE"** → go through the wizard: name `home-voice-satellite`, board type ESP32, then select "ESP32-S3" (if asked) → you can enter Wi-Fi right away (SSID/password) — the Dashboard will automatically create `secrets.yaml` and write `wifi_ssid`/`wifi_password` there for you. Complete the wizard to the end — a device card will appear.
3. On the device card, click the **pencil icon (Edit)** — a built-in text editor will open with the file contents right in the browser. Select all (Ctrl+A) and delete, then paste the **entire** contents of our `esphome/voice-satellite.yaml` there. Save (diskette icon / Ctrl+S).
4. Add the secrets `api_encryption_key` and `ota_password` (which the wizard did not create) via the **"SECRETS"** menu item in the upper Dashboard panel (key icon / three dots → Secrets) — the `secrets.yaml` editor will open, add both lines there and save.
   - If such an item is not in your version of the Dashboard — you can do the same via the **File Editor** or **Samba** add-ons (Settings → Add-ons → Add-on Store, if not installed yet): they open the `/config/` filesystem, including `/config/esphome/secrets.yaml`, with a regular text/file editor.
5. Connect the ESP32-S3 to your computer with a USB cable.
6. On the device card, click **"INSTALL"** → **"Plug into this computer"** → select the port (usually `/dev/ttyUSB0` on Linux or `COMx` on Windows).
7. Wait for compilation and flashing (the first time takes 3-7 minutes, the compiler downloads the toolchain).
8. After flashing, the device will reboot and connect to Wi-Fi on its own.

If the port is not found — reinstall/check the USB-UART bridge driver (CP210x or CH340 depending on the board), this is a standard issue with the first flashing, unrelated to our config.

---

## 5. Further Updates — Over-The-Air (OTA)

After the first flashing, you can change the config (for example, when you replace placeholder animations with your own) without USB — the Dashboard will see the device on the network and offer "Install" → "Wirelessly".

---

## 6. Adding the Device to Home Assistant

1. Settings → Devices & Services → a notification "Discovered ESPHome device home-voice-satellite" should appear automatically (mDNS discovery).
2. Click "Configure", confirm — HA will ask for that exact `api_encryption_key` (if you entered it in the config, it should connect successfully, HA will pass it itself).
3. Once connected, the device will have entities: `media_player.home_voice_satellite_media_player`, `sensor...`, `binary_sensor...`, etc. — check against the real names in Settings → Devices → this device, they will be useful for automations.yaml.

---

## 7. Installing a Custom Conversation Agent

This is **not the same folder** where `images/` and ESPHome `secrets.yaml` from steps 3-4 are located — that one (`/config/esphome/`) is nested INSIDE the configuration folder of Home Assistant itself. Now we need the root level, one level higher:

Important: below there are **two different** `gemini_assistant/` in different places — this is not a typo, code and data are intentionally separated into different folders so that a code update never overwrites your memory:

```
/config/                             <- HA configuration root (this is what you need for this step)
  esphome/                           <- this is where you put voice-satellite.yaml, secrets.yaml (ESPHome), images/
    secrets.yaml
    voice-satellite.yaml
    images/
  custom_components/
    gemini_assistant/                <- CODE (step 3 below): conversation.py, memory.py, google_tools.py,
                                         __init__.py, sensor.py, manifest.json - copy the folder from the
                                         project here entirely, do not touch it manually later
  gemini_assistant/                  <- DATA (step 4 below): only one file, memory.json - this is what
                                         grows and changes as you use the assistant
    memory.json
  secrets.yaml                       <- ANOTHER secrets.yaml, for HA itself (step 12)
  configuration.yaml
```

1. On the laptop with HA, find the root HA configuration folder (`/config` — if HA OS/Supervised, open it via the File Editor or Samba add-on; if HA Core — it's just the folder you specified during installation). You cannot open it via the ESPHome Dashboard — it only has access to its `esphome/` subfolder; for this step, file access to the entire `/config` is needed.
2. Inside `/config` (next to the `esphome/` folder, not inside it), create `custom_components/` (if it does not exist yet).
3. Copy the entire `custom_component/gemini_assistant` folder from our project there — you will get `/config/custom_components/gemini_assistant/...` **with the code** (`memory.py` inside is a class that knows how to read/write the memory file, not the memory itself).
4. Separately, at the `/config` level (NOT inside `custom_components/`), create another `/config/gemini_assistant/` folder — this time **for data only** — and put `memory_template.json` from the project there, renaming it to `memory.json`. The path to this file is specified in `gemini_assistant.memory_path` in `configuration_snippet.yaml` (step 12) — if you want to store it in a different place, change it there.

---

## 8. Gemini API Key

1. Go to **aistudio.google.com** with your Google account.
2. Left/top — "Get API key" → "Create API key" (select an existing Google Cloud project or let it create a new one automatically).
3. Copy the key — you will need it in the Home Assistant `secrets.yaml` (not to be confused with the ESPHome secrets.yaml, these are different files!) as `gemini_api_key`.

There is a free tier, but the Gemini API is billed by tokens — if there are many conversations, it's worth checking billing in the Google Cloud Console periodically.

---

## 9. Google Cloud: Access to Calendar, Gmail, and Notes

This is a separate, longer process because Google requires going through an OAuth verification to access personal data:

1. **console.cloud.google.com** → select the same project as for Gemini (or create a new one) → APIs & Services → Library.
2. Enable three APIs: **Google Calendar API**, **Gmail API**, and **Google Tasks API** (the latter is for notes: "запиши, что я тебе сказал" ("note down what I told you") will go to Google Tasks, this can be seen later in the Google Tasks app / Google Keep-like list on your phone; Google Keep as such does not have a public API for regular accounts, so we use Tasks — the closest working option).
3. APIs & Services → OAuth consent screen: type "External" (unless it's a Google Workspace account), fill in the minimums (app name, your email), add yourself to "Test users" (until the app is verified by Google, it will only work for accounts from this list — i.e., for yourself, this is normal for a personal project).
4. APIs & Services → Credentials → Create Credentials → OAuth client ID → type **Desktop app**. Click Create.
5. Google will show a dialog with a "Download JSON" button — download the file (it will have a long name like `client_secret_....json`), it will end up in the regular "Downloads" folder on the computer you are currently using. **Do not move it anywhere yet** — you will need it literally in the next step, on this same computer.

### One-Time Authorization (Getting token.json)

The purpose of this step: our code (`google_tools.py` inside `custom_component/gemini_assistant/`) needs to "introduce" itself to Google once and get permission to act on your behalf. Usually, this opens a browser with a "Sign in with Google" screen — but Home Assistant often runs without a monitor and browser (headless), so we perform this very first sign-in not on it, but on a regular computer where a browser is available. **You do not need to open or edit `google_tools.py` for this** — it is only mentioned to explain why we even need the `token.json` file that we are about to obtain. Afterward, HA will read the already prepared `token.json` and everything will happen automatically, without a browser.

What to do specifically, step by step:

1. Take any computer with internet and a browser (the same laptop where HA is installed, or your regular computer — it doesn't matter). Python must be installed on it — if you are not sure if it is, open the terminal/command prompt and type `python3 --version` (on Windows sometimes `python --version`); if it outputs a version number — Python is present, if a "command not found" error — install Python from python.org first.
2. In the terminal, enter the command and press Enter — it will install two helper packages: `pip install google-auth-oauthlib google-api-python-client`.
3. Create a **new empty folder** anywhere (for example, on the Desktop), name it anything, for example `google-auth-setup`.
4. Move the file downloaded in step 5 above (`client_secret_....json`) into this folder, and rename it strictly to `credentials.json` (the exact name is important — you can rename it like a regular file via "Rename" in the file manager).
5. In the same folder, create a new text file using any text editor (standard "Notepad"/Блокнот, TextEdit, VS Code — anything works), and paste exactly this code into it:

```python
from google_auth_oauthlib.flow import InstalledAppFlow
SCOPES = ["https://www.googleapis.com/auth/calendar", "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/tasks"]
flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)
open("token.json", "w").write(creds.to_json())
```

**Important**: none of the lines should have leading spaces/indents — all 5 lines must start strictly at the very beginning of the line (Python, unlike many languages, is sensitive to indents, an extra space at the beginning of a line is an `IndentationError`). If you copy the code directly from this file — check after pasting into Notepad that the left side of each line is empty.

Save the file in the same folder under the name `get_google_token.py` (important: the extension must be `.py`, not `.txt` — when saving in Notepad, select "Save as type: All files" and type the name with `.py` manually).
6. In the terminal, navigate to this folder (command `cd path_to_folder`, for example `cd Desktop/google-auth-setup`) and run the script with the command: `python3 get_google_token.py` (or `python get_google_token.py` on Windows).
7. Usually, a browser window with the Google login screen opens automatically right here. **If the browser did not open automatically** — no problem: look at the same terminal window where you ran the script — there should be a line like `Please visit this URL to authorize this application: https://accounts.google.com/o/oauth2/auth?...`. Select it entirely (in cmd/PowerShell — select with the mouse and press Enter or right-click to copy) and paste it into the address bar of any browser manually — further steps are identical. Auto-opening fails if the default browser is not defined in the system, or if the terminal is running in a headless environment — this does not affect the result, just an extra manual click.
8. Sign in with the same account you used in the Google Cloud Console. Google will show a warning "Google hasn't verified this app" / "непроверенное приложение" — this is expected for a personal non-commercial project, not an error. Click "Advanced"/"Дополнительно" → "Go to (unsafe)"/"Перейти на ... (небезопасно)" → confirm access (Allow/Разрешить) to all requested permissions (calendar, mail, tasks).
9. After this, the browser will show something like "The authentication flow has completed" — you can close the tab. In the same folder next to the script, a new file `token.json` will appear — this is the result.
10. Now you have two ready files in the `google-auth-setup` folder: `credentials.json` and `token.json`. Copy **both** to the Home Assistant laptop, into the `/config/gemini_assistant/` folder (you already created it in step 4 of section 7). From then on, `token.json` updates itself automatically; you do not need to repeat this step (until the refresh token expires — which happens very rarely).

---

## 10. Google Keep (Unofficial, via gkeepapi)

This is a separate mechanism from step 9 — Google Keep does not have an official API for regular (non-Workspace) accounts, only domain-wide delegation for corporate domains. If you really want notes in actual Keep (rather than Google Tasks), we use the unofficial `gkeepapi` library, which mimics the internal app protocol.

**Deliberate risks you are taking with this approach:**
- The library is not supported by Google and may break without warning upon any change to the internal protocol;
- Frequent logins may look like suspicious activity to Google — which is why we log in once and then reuse the token, rather than logging in on every request.

**Steps:**

1. Enable two-factor authentication if not already enabled: **myaccount.google.com/security** → "2-Step Verification" → Turn on.
2. Create an App Password specifically for this: **myaccount.google.com/apppasswords** → "Select app: Other (custom name)" → name it, for example, `gkeepapi` → Generate → copy the 16-character password (without spaces). Your regular account password will not work here.
3. On any computer with Python: `pip install gkeepapi`.
4. Copy `custom_component/gemini_assistant/get_keep_master_token.py` from the project there and run: `python get_keep_master_token.py`. Enter your email and the App Password from step 2.
5. A `keep_token.json` file will be generated — copy it to the HA laptop, into `/config/gemini_assistant/keep_token.json`.
6. In `configuration.yaml` (see step 12), specify the path to it in `gemini_assistant.keep_keep_token_path` — it is already listed in `configuration_snippet.yaml`.

If you skip this step (do not create `keep_token.json`), the agent simply will not offer the Keep note-taking tool, but Google Tasks from step 9 will continue to work as usual.

---

## 11. Telegram Bot

1. In Telegram, write to **@BotFather** → `/newbot` → come up with a name and username for the bot → you will get a token like `123456:ABC-DEF...`.
2. Write to **@userinfobot** (any such bot) — it will send you your `chat_id` (a number).
3. Both values go into the Home Assistant `secrets.yaml`: `telegram_bot_token`, `telegram_chat_id`.
4. Don't forget to **write to your bot first** in Telegram by sending at least one message — otherwise the bot won't be able to send you its first notification (Telegram requires the conversation to be initiated by the user).

---

## 12. Home Assistant Configuration: Secrets and Config

1. Open `secrets.yaml` in `/config/` (the shared HA secrets file, create it if it doesn't exist), and add:
   ```yaml
   gemini_api_key: "..."
   telegram_bot_token: "..."
   telegram_chat_id: "..."
   ```
   (`google_client_id`/`google_client_secret` are NOT needed here — see the subsection below, they are not entered through secrets.yaml.)
2. Open the main `configuration.yaml`, paste the contents of our `homeassistant/configuration_snippet.yaml` (or include it via `!include` if your setup is structured that way).
3. Check the config before restarting: Developer Tools → YAML → "Check Configuration" (or Settings → System → "Check Configuration" button).

### Google Calendar Inside Home Assistant Itself (for Reminder Triggers)

Separately from what we did in step 9 (which provided access for our Python agent), Home Assistant itself also needs access to the calendar so that the `calendar` trigger works in automations.yaml (reminder 15 minutes before a meeting). Previously, this was configured via YAML (`calendar: platform: google`), but in modern HA versions, this has been moved in favor of a standard UI integration:

1. Google Cloud Console → the same project → APIs & Services → Credentials → Create Credentials → OAuth client ID → this time select type **Web application** (not Desktop app — this is a separate, second OAuth client, specifically for HA).
2. In the "Authorized redirect URIs" field, add: `https://my.home-assistant.io/redirect/oauth`.
3. Create → it will show **Client ID** and **Client secret** — copy them somewhere temporarily (a regular text file on your computer); you do not need to put them in `secrets.yaml`.
4. In Home Assistant: Settings → Devices & Services → "+ Add Integration" → find **Google Calendar** → in the wizard that opens, paste the Client ID and Client secret from step 3 when requested (HA will save them as "Application Credentials" — a separate secure storage, not the `secrets.yaml` file).
5. Then a standard Google login screen will open (like in step 9) — sign in with the same account, grant access to the calendar.
6. After that, a `calendar.<name_of_your_calendar>` entity will appear — use it in automations.yaml instead of `calendar.your_google_calendar`.

**Why is there no similar step for Tasks and Keep?** Because they are not needed here — Tasks (step 9) and Keep (step 10) are already fully set up and working; the agent accesses them directly from its Python code (`google_tools.py`/`keep_tools.py`) via `credentials.json`/`token.json`/`keep_token.json`, without any separate entity inside HA. This entire subsection with UI integration was needed **only** because of the calendar — because `automations.yaml` contains a "remind 15 minutes before an event" trigger automation (step 16, item 4), and triggers in HA can only fire on its own entities (`calendar.*`), which our Python code simply doesn't have. For Tasks/Keep, such an automation trigger is not provided (there is no scenario "remind me when a new task/note appears"), so there is no reason to build an extra HA integration for them.

---

## 13. Local STT/TTS: Whisper and Piper

(Only if HA OS/Supervised — on Container/Core, instead of add-ons, you use any third-party STT/TTS integrator or cloud option, there are no add-ons there.)

1. Settings → Add-ons → Add-on Store → find "Whisper" → Install → Start.
2. Similarly, "Piper" → Install → Start.
3. Both will appear as available STT/TTS options in the voice pipeline settings (next step).

---

## 14. Voice Pipeline (Assist Pipeline)

1. Settings → Voice Assistants → "Add Assistant".
2. Language: Russian.
3. Speech-to-text: Whisper.
4. **Conversation agent: Gemini Assistant** (will appear in the list thanks to our custom_component — if it doesn't appear, restart HA and check logs for import errors).
5. Text-to-speech: Piper (select a Russian voice if multiple are offered).
6. Wake word: you can leave it blank/"none" here — because our wake word is **on-device**, in the firmware itself (`micro_wake_word`), and not in HA.
7. Save, name the pipeline, for example "Домашний ассистент" ("Home Assistant").

---

## 15. Linking the Satellite to the Pipeline

Settings → Devices & Services → Devices → find `home-voice-satellite` → there should be an "Assistant" / "Voice Assistant Pipeline" setting — select the pipeline created in step 13 (if the ESPHome voice_assistant is configured correctly, it usually picks up automatically).

---

## 16. Automations

1. Settings → Automations & Scenes → "..." icon → Edit in YAML (for a new empty automation), or simply open `/config/automations.yaml` via the file editor/Studio Code Server add-on and paste the contents of our `homeassistant/automations.yaml`.
2. **Be sure to adjust for yourself**:
   - `person.you` → to your real `person.<your_name>` (Settings → People, check the exact entity_id).
   - `calendar.your_google_calendar` → to the real calendar entity (appears after step 12, Google Calendar platform integration).
   - `assist_satellite.home_voice_satellite` — this is the entity_id of the satellite itself, to which speech output is sent via the **`assist_satellite.announce`** service (action). Note: this is NOT an `esphome.*` service — in current versions of Home Assistant (2024.10+), the ESPHome voice satellite is represented as a universal entity of the `assist_satellite` domain, and announcements are made through it, not through a separate service for each device. That is why if you try searching for "esp" in the Actions tab, nothing will be found — this is normal, there shouldn't be an esphome service for this. To find the exact entity_id of your specific satellite:
     1. In the left menu of Home Assistant, open **Developer Tools**.
     2. Open the **States** tab.
     3. In the filter field, start typing `assist_satellite` — exactly one line should appear (or more, if there are multiple satellites) — this is the required entity_id, copy it as is (e.g., `assist_satellite.home_voice_satellite` or similar, depending on how HA named it when adding the device).
     4. If the entity_id differs from what is currently in `homeassistant/automations.yaml` (everywhere `assist_satellite.home_voice_satellite` is written) — replace it with the real one.
   - The `assist_satellite.announce` service accepts a `message` field (text for TTS speech output) — our automations already use it this way, no need to change it.
3. Settings → Devices & Services → Helpers → "+ Create Helper" → Toggle → name it `assistant_dnd` (this will create `input_boolean.assistant_dnd`, used by the "do not disturb" automation).

---

## 17. Pre-Flight Check

- Developer Tools → YAML → "Check Configuration" — no errors.
- Settings → System → Restart Home Assistant.
- Check logs: Settings → System → Logs — there should be no errors about `gemini_assistant` (a frequent cause is that automatic package installation from `manifest.json` failed, in which case HA will try to reinstall on the next startup; if it crashes consistently, check that the `requirements` in manifest.json are available from pip in this environment).

---

## 18. Testing

1. Say "Окей Набу" ("Okay Nabu") (the wake word from the `okay_nabu` config) near the device — the "listening" animation (`listening.gif`) should appear on the screen.
2. Ask a question — the screen will switch to "thinking" (`thinking.gif`), then "speaking" (`speaking.gif`) along with a voice response from the speaker.
3. Ask "запомни, что я хожу в спортзал по вторникам" ("remember that I go to the gym on Tuesdays") — in a couple of seconds, the agent should confirm, and `memory.json` should update (you can check the file manually).
4. Set a reminder "напомни через 2 минуты проверить это" ("remind me in 2 minutes to check this") and wait — automation #1 from `automations.yaml` will trigger, an announcement will come to the speaker (if you are at home) or to Telegram (if not — temporarily set yourself to `not_home` manually via Developer Tools → States to test the fallback).
5. Write something to your Telegram bot — a response from the same agent should arrive.

---

## 19. Where to Get Better Animations (When You Decide to Replace the Placeholders)

I generated 4 basic GIFs (`esphome/images/idle.gif`, `listening.gif`, `thinking.gif`, `speaking.gif`) — simple, but functional, so you have something to show on the screen right away: a breathing ring at rest, running dots while listening, a spinner when "thinking", an equalizer when answering. These are black-and-white silhouettes (white on black), not colored — see the flash info box below for why.

### ⚠️ Main Limitation — Flash Space

The ESP32-S3 from the specification (esp32-s3-devkitc-1) has 8MB of total flash memory, but the standard partition scheme with OTA update support allocates only about **1.8MB** for the firmware itself. Animations are stored not as a separate file, but are embedded directly into the firmware body as a byte array — and this eats up the limit very quickly:

- `type: RGB565` (2 bytes per pixel) — at 240x240 and dozens of frames, it easily bloats the firmware to 10+ MB, compilation then fails with the error `The program size (...) is greater than maximum allowed (...)`.
- `type: BINARY` (1 bit per pixel, only 2 colors) — the same frame weighs **16 times less**. This is what is currently set in `voice-satellite.yaml`.

Guideline when picking your own animations: stick to **1-2 colors (BINARY)** and **8-12 frames** per animation at 240x240 resolution — this fits within the budget with room to spare. If you really want color (`GRAYSCALE` or `RGB565`) — compensate by proportionally reducing the resolution (e.g., 100x100) and the number of frames, and keep an eye on the `Flash: [...] X% (used ... from ...)` line in the compilation log — it must be less than 100%.

### How to Replace

1. **lottiefiles.com/free-animations/voice-assistant** (or search for "siri wave", "ai voice assistant", "loading spinner") — there are many free animations there, exported directly as GIFs.
2. Bring them to the required size, frame count, and color depth via **ezgif.com** (online, no installation): "Resize" → 240x240 (or smaller), "Optimize"/"Crop frames" → trim to 8-12 frames. If you want to stay on `BINARY`, you can pre-convert to black and white ("Effects" → "Black & White" with high contrast) so that the result after ESPHome binarization looks predictable.
3. Replace the file in `esphome/images/` **with the same name** (`idle.gif`, `listening.gif`, `thinking.gif`, `speaking.gif`) — no need to change the config, `type: BINARY` is already specified in `voice-satellite.yaml` under each animation (this is basically a required parameter in newer ESPHome versions — without any `type`, there will be an error `'type' is a required option for [animation]`). Simply recompile/reflash via the ESPHome Dashboard (step 5, wirelessly) and check the `Flash:` line in the log.
4. If you later want more "alive" graphics (smooth vector transitions rather than just GIF frames), look into the ESPHome LVGL component (`esphome.io/components/lvgl/`, `animimg` widget) — it is more powerful, but the config is also more complex; for a home project, regular frame-by-frame animation via `animation:` (as it is now) is quite sufficient.

---

## 20. Common Issues

- **`The program size (...) is greater than maximum allowed (...)` during compilation**: the firmware doesn't fit in the flash partition — almost always the animations are to blame (see the note in section 19). Check that all 4 animations use `type: BINARY` instead of `RGB565`/`GRAYSCALE`/`RGB`, and that there are no more than 8-12 frames per animation at 240x240.
- **Microphone silence**: double-check that LR is actually connected to GND and not just lying nearby; check that `channel: left` in the config matches the actual soldering.
- **ESP32 not visible on Wi-Fi**: almost always a 5GHz network or an incorrect password in `secrets.yaml`.
- **Gemini Assistant does not appear in the conversation agent list**: check HA logs for `google-genai`/`google-api-python-client` import errors — HA installs them itself via `manifest.json`, but if offline or behind a proxy, it won't work, and you'll have to deal with networking on the laptop.
- **Calendar/mail not responding**: most likely `token.json` is not created or was created in the wrong folder — the agent will crash with "error: ..." right in the response, the error text usually hints at what is wrong.
- **`LoginException: BadAuthentication` in get_keep_master_token.py**: almost always means you entered your regular account password instead of an App Password, or two-factor authentication is not enabled (steps 1-2 of section 10 are mandatory, without them an App Password cannot be created).
- **Announce doesn't play**: check that `automations.yaml` uses the correct `entity_id` in `target:` for the `assist_satellite.announce` service (see step 16) — verify it in Developer Tools → States by filtering for `assist_satellite`.
- **"Wake word" in the satellite settings is grayed out/unavailable**: this is expected and not a bug. This dropdown allows switching between MULTIPLE wake word models embedded in the firmware. We have only one model (`okay_nabu`) specified in `micro_wake_word:` in `voice-satellite.yaml` — there is nothing to choose between, so HA blocks the field. If you want switchable phrases, add more models to the firmware under `micro_wake_word: models:` (see `esphome.io/components/micro_wake_word/` for the list of available ones), reflash — then the field will become active.
