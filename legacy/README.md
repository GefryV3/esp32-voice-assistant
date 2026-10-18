# Порядок установки

Краткая версия ниже. **Полная пошаговая инструкция (с получением всех ключей/токенов и разбором частых проблем) — в `TUTORIAL.md`, начни с неё.**

1. `secrets.yaml.example` -> скопировать в `secrets.yaml` (и рядом с ESPHome-конфигом, и в HA `/config/`), заполнить.
2. Прошить ESP32-S3: `esphome run esphome/voice-satellite.yaml` (или добавить конфиг в ESPHome Dashboard внутри HA).
3. Скопировать `custom_component/gemini_assistant` в `/config/custom_components/gemini_assistant` на ноутбуке с HA.
4. Добавить содержимое `homeassistant/configuration_snippet.yaml` в основной `configuration.yaml`.
5. Положить `memory/memory_template.json` в `/config/gemini_assistant/memory.json`.
6. Установить аддоны Whisper и Piper (Settings -> Add-ons -> Add-on Store), выбрать их в Settings -> Voice Assistants -> pipeline вместе с агентом gemini_assistant.
7. Добавить автоматизации из `homeassistant/automations.yaml` (через UI-редактор или `!include automations.yaml`), поправить entity_id (`person.you`, `calendar.your_google_calendar`, `esphome.home_voice_satellite_...`) под реальные из твоей системы.
8. Создать `input_boolean.assistant_dnd` (Settings -> Devices -> Helpers) для режима "не беспокоить".
9. Перезапустить Home Assistant.

Анимации состояний экрана (`esphome/images/*.gif`) — уже готовые рабочие заготовки (кольцо/точки/спиннер/эквалайзер). Как заменить на что-то красивее — раздел 19 в `TUTORIAL.md`.

Заметки: по умолчанию агент умеет писать в Google Tasks. Если хочется именно в Google Keep — это отдельный опциональный шаг (неофициальная библиотека gkeepapi, раздел 10 в `TUTORIAL.md`), пропустить можно без потери остального функционала.

Подробное описание архитектуры и решений — в `01_architecture.md`.
