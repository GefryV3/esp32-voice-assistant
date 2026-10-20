# Speaker Firmware

C++17 on ESP-IDF 5.5.5. Modules are described in [docs/architecture.md](../docs/architecture.md).

## Before First Build

1. `main/secrets.h`: if it does not exist, copy it from `main/secrets.h.example`. Fill in Wi-Fi,
   server address, and token (the same as `JARVIS_DEVICE_TOKEN` in `server/.env`).
2. Build **only from PowerShell or cmd** (the toolchain breaks in Git Bash) and only from a
   path without Cyrillic characters.

## Commands

```powershell
cd C:\dev\jarvis\firmware
.\idf.ps1 build                    # build
.\idf.ps1 -p COM5 flash monitor    # flash via USB and monitor logs (use your COM port)
.\idf.ps1 size                     # how much flash/RAM is used
.\idf.ps1 menuconfig               # ESP-IDF settings
```

`idf.ps1` sets up the ESP-IDF environment automatically (it is installed by ESPHome in
`C:\Users\kurli\.local\idf`; `C:\idf` is a short link to this folder).

Ready-made image for OTA updates: `build\jarvis.bin`.

## What You See on the Screen

- **1.5 s splash screen** on boot: red, green, and blue stripes from top to bottom, digits
  «123» ("123") from left to right. If colors or digits are swapped, fix `LCD_BGR` /
  `LCD_MIRROR_X` in `main/board.h`.
- Next is the face according to the mockup in [design/screen-mockup.html](../design/screen-mockup.html).
- **Red crossed-out Wi-Fi to the left of the weather** — no connection to the server.
- The clock and date appear once the server sends the time.
- Every 5 s, the frame rate and how long each part of the frame takes are logged.

## Fonts

Glyphs for the screen are generated from Inter (`assets/fonts/Inter.ttf`, OFL license) into
`main/face/fonts_data.cpp`. To change the font size or character set, edit the list in
`tools/make_fonts.py` and run:

```powershell
py -m venv tools\.venv
tools\.venv\Scripts\pip install pillow
tools\.venv\Scripts\python -I tools\make_fonts.py assets\fonts\Inter.ttf main\face\fonts_data.cpp
```

## Rollback

Firmware flashed over the air confirms itself automatically after connecting to Wi-Fi. If it
fails to reach Wi-Fi, the previous firmware will be restored after a reboot. You can always
revert to the old ESPHome: their partition tables are identical.
