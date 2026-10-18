# Jarvis — домашний голосовой ассистент

Своя прошивка для колонки на ESP32-S3 + свой сервер на Python.
Home Assistant — только посредник для умного дома (свет, датчики, «кто дома», погода, календарь).

```
firmware/   прошивка колонки (C++, ESP-IDF 5.5)
server/     сервер-«мозг» (Python 3.12+)
docs/       архитектура, протокол связи, этапы, железо
legacy/     старый проект на ESPHome + HA (только для справки, не собирается)
```

С чего читать:
- [docs/architecture.md](docs/architecture.md) — как всё устроено и почему;
- [docs/protocol.md](docs/protocol.md) — о чём колонка и сервер говорят по WebSocket;
- [docs/roadmap.md](docs/roadmap.md) — этапы и что уже сделано;
- [docs/hardware.md](docs/hardware.md) — железо и пины.

## Сервер (разработка на Windows)

```powershell
cd server
py -m venv .venv
.venv\Scripts\pip install -e .[dev]
copy .env.example .env      # заполнить JARVIS_DEVICE_TOKEN
.venv\Scripts\python -m jarvis
```

Тесты: `.venv\Scripts\pytest`.
Колонку без железа можно изобразить: `.venv\Scripts\python tools\fake_device.py`.

## Прошивка

Сборка только из PowerShell/cmd (не из Git Bash) и только из пути без кириллицы —
поэтому проект лежит в `C:\dev\jarvis`. Подробности — [firmware/README.md](firmware/README.md).
