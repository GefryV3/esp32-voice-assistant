# Contributing

This is a personal project that runs in one bedroom, but issues, ideas and pull requests are welcome.

## Development setup

- **Server:** Python 3.12+. `cd server && python -m venv .venv && pip install -e ".[dev]"`, then `pytest`.
  `tools/fake_device.py` stands in for the speaker when there is no hardware.
- **Firmware:** ESP-IDF 5.5. Copy `firmware/main/secrets.h.example` to `secrets.h`, then `idf.py build`.
- **Android app:** JDK 17 and Android SDK 35, `gradle -p phone assembleDebug`.

## Guidelines

- All behaviour lives in code with tests: no logic in Home Assistant automations.
- The speaker ↔ server protocol is described in [docs/protocol.md](docs/protocol.md). Change the document together
  with the code, and keep old firmware working when possible.
- New settings go into `server/.env.example` with a short comment.
- Never commit secrets: `.env`, `secrets.h` and built APKs stay local.
- Docs and code comments are written in Russian; English is fine in issues and pull requests.
