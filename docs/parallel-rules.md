# Rules for Parallel Work on Jarvis

Read this first if you have been launched as a parallel session.

## About the Project

Jarvis is a personal voice assistant named **Maga** (wake word "Maga", backup "Hey Jarvis"):
- `firmware/` — speaker firmware (ESP-IDF 5.5, ESP32-S3, round GC9A01 screen, INMP441 microphone,
  MAX98357A amplifier). Build from PowerShell: `cd firmware; .\idf.ps1 build` (see firmware/README.md and
  the `jarvis-build-windows` memory). `main/secrets.h` is not in git: copy it from
  `C:\dev\jarvis\firmware\main\secrets.h` to your worktree and do not commit it.
- `server/jarvis/` — Python 3.12+ server: voice via Gemini Live, brain (`brain.py`: memory, reminders,
  Gemini tools), smart home via HA (`ha.py`), Google (`google.py`), Telegram (`telegram.py`),
  background agents (`agents.py`), phone/PC bridge (`bridge.py`), presence (`presence.py`),
  routines (`routines.py`).
- `phone/` — Android app (Compose), `pc/` — PC application, `design/` — screen mockups.
- Home Assistant — used only as a bridge for the smart home (its address is 192.168.178.86, at the user's home).

User: a school student living near Vienna, Russian speaker, explanations should be simple,
responses in Russian. The speaker is located in his bedroom near the bed and computer.

Context: `docs/roadmap.md`, `docs/ideas.md` (section "Decisions 2026-10-20"), `docs/protocol.md`,
`docs/protocol-next.md` (**contract of new messages speaker ↔ server, stick to it**),
`docs/architecture.md`, project memory `C:\Users\kurli\.claude\projects\C--dev-jarvis\memory`
(`ideas-decisions`, `home-ha-setup`, `screen-design`, `jarvis-build-windows`, `speaker-wifi-barge`).

## What is Allowed and Forbidden

- Everything within the project scope is allowed without questions, but **do not enter passwords, keys, and tokens into forms, do not ask
  for them in chat, and do not put them into commits** (secrets belong only in `server/.env`, which is excluded from git).
- You work **in your own worktree and your own branch**. Do not switch branches and do not touch the main folder.
- **Do not deploy** to HA (`tools/deploy_addon.py`, `tools/ha_admin.py`), **do not flash the speaker** (neither via
  USB nor over the air) and do not touch the live device: the coordinator does this after merging when the user is at home.
  Two simultaneous deployments break the addon image.
- **Do not modify** `server/jarvis/__init__.py` (version), `server/addon/CHANGELOG.md`, `docs/roadmap.md`,
  `docs/ideas.md`. Instead, describe the result in `docs/changes/<session-name>.md`: what was done, new
  `.env` variables, console commands, what to check live, what remains.
- Put new code into **new modules**. In shared files (`brain.py`, `voice.py`, `config.py`, `__main__.py`,
  `console.py`, `protocol.py`, `devices.py`), make minimal additive edits: this makes merging easy.
- Do not invent user data (addresses, logins, schedules). If something is missing, write in the report
  what exactly to ask the user for.

## Verification

- Tests: from the `server` folder of your worktree, run
  `C:\dev\jarvis\server\.venv\Scripts\python -m pytest -q`. The interpreter is shared, the `jarvis` package must
  be imported from your folder (verify that tests pick up your code). Write tests using mocks (examples:
  `tests/test_ha.py`, `tests/test_google.py`, `tests/test_agents.py`).
- The user is currently at school and cannot verify anything live. Everything must be verified with tests and mocks,
  and the report must honestly state what **was not verified live**.
- Do not run `python -m jarvis` locally: the same Telegram token is used by the running addon, which will cause a 409 conflict.

## Commit and Report

- Write documentation in English: `docs/`, `docs/changes/<session-name>.md`, README files. Strings the assistant
  speaks or recognises stay in Russian. The repository is public, so no home address, school, names or Wi-Fi
  names in docs or code.
- `git -c user.name=kurli -c user.email=78043625+GefryV3@users.noreply.github.com commit`, end the message with the line
  `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Commit in your branch.
- Final report: what was done, commit hash and branch, tests (how many passed), what was not verified live,
  questions for the user (if data or decisions are needed).
