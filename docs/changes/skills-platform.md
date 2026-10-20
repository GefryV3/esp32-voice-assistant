# skills-platform: навыки-плагины, предложение навыков, хук прогресса Claude Code

Ветка `claude/skills-platform-c373c3`. Подробности: [../skills.md](../skills.md), [../claude-hook.md](../claude-hook.md).

## Что сделано

1. **Плагины-навыки.** `server/jarvis/skills/` (папка), `skills_loader.py`: поиск, статическая проверка кода,
   включение/выключение (таблица `skills` в `brain.db`), горячая перезагрузка (`skills reload`, голосом через
   `skills_manage`), изоляция ошибок и зависаний (навык отключается, сообщение в Telegram), защита от подмены файла
   (sha256). Образцы: `clock` (`current_time` переехала из `brain.py`) и `dice` (`roll_dice`), оба доверенные.
   Правки `brain.py`: загрузчик создаётся в `__init__`, `tools()` и `_call` подхватывают навыки; из `TOOLS`/`_call`
   убрана `current_time` (теперь в навыке `clock`); новый необязательный параметр `skills_dir`.
2. **Предложение навыка.** `skill_requests.py`: функции Gemini `propose_skill(title, what, why)` и
   `skill_decision(approve|reject)`; таблица `skill_requests`; карточка в Telegram; команды Telegram `/skills`,
   `/approve N`, `/reject N`, `/enable имя`, `/disable`, `/reload`, `/diff N` (в `telegram.py` аддитивно: поле
   `skills` и ранний разбор команд до Gemini); те же команды в консоли (`hub.skills`).
3. **Исполнитель на ноутбуке.** `server/tools/skill_runner.py` + обработка `skill_job` в `pc/agent.py`; мост
   (`bridge.py`) пересылает `skill_progress`/`skill_result` через `on_pc_event` и сообщает о подключении ноутбука
   (`on_pc_connect`: ушедшие в ожидание задания отправляются). Модель безопасности: `docs/skills.md`.
4. **Хук прогресса.** `server/tools/claude_hook.py` (Claude Code) → служебный вход `/admin` → `claude_status.py` →
   `task_status`. Состояния `working` (процент по todo), `needs_permission`, `needs_input`, `done`; озвучивание конца
   по настройке `claude speak on|off`. В `protocol.py` добавлена `task_status(state, label, pct)` (формат по
   protocol-next.md). **При слиянии с display:** если там уже есть такая функция, оставить одну; моя обрезает
   метку строго до 31 байта (`_fit`), штатный `_clip` может выдать до 34.

## Новые переменные `.env`

Никаких. Хук читает те же `JARVIS_SERVER`, `JARVIS_PORT`, `JARVIS_DEVICE_TOKEN`.

## Новые команды консоли (и `tools/send.py`)

`skills [list]`, `skills enable|disable <имя>`, `skills reload [имя]`, `approve N`, `reject N`, `enable имя`,
`diff N`, `claude` (сессии), `claude speak on|off`, `claude_event <json>` (служебная, от хука).

## Правки общих файлов (все аддитивные)

`brain.py` (см. выше), `telegram.py` (+8 строк), `bridge.py` (+9: два колбэка и ветка `skill_*` в `on_pc`),
`console.py` (два модуля в обходе команд), `devices.py` (поля `skills`, `claude`), `__main__.py` (+14: связка),
`protocol.py` (`task_status`), `pc/agent.py` (+27: `skill_job`). Не трогал: версию, CHANGELOG, roadmap, ideas.

## Тесты

Всего `262 passed, 2 skipped` (было 195). Новые: `test_skills.py` (28: загрузчик, проверка кода, жизненный цикл,
сбои, таймаут, подмена файла, ctx), `test_skill_requests.py` (12: предложение, дубли, лимит, одобрение,
ноутбук оффлайн, голосовое «да», весь путь до `/enable`, Telegram), `test_skill_runner.py` (14: команда `claude`,
белый список, лишние файлы, проверка кода, тесты, таймаут, настоящий git во временном репозитории),
`test_claude_status.py` (13: состояния, приоритет сессий, озвучивание, хук по настоящему WebSocket).

## НЕ проверено вживую

- Настоящий запуск `claude -p` с этими флагами (`--permission-mode dontAsk`, `--permission-prompts none`,
  `--allowedTools "Edit(путь)"`, `--tools`): флаги сверены с актуальной документацией, но запуска не было
  (нужны вход в Claude Code и деньги). Первый боевой запуск стоит сделать самому: `skill_runner.py job.json`.
  Если правило `Bash(python -m pytest …)` окажется слишком строгим, Claude не сможет прогнать тесты, а раннер всё
  равно прогонит их сам и честно сообщит.
- Реальные хуки Claude Code и форма `tool_input` у `TodoWrite` (`todos[].status`); отрисовка `task_status` на колонке.
- Telegram-карточки и голосовое «да, делай» на живых сервисах; цепочка через настоящий `/pc` (проверен только
  обработчик моста по отдельности и `skill_runner` по отдельности).
- Выкладку в HA не делал, колонку не трогал.

## Что осталось / идеи

- Навыки выполняются в процессе сервера; для действительно недоверенных нужен отдельный процесс.
- Нет «публичных» подсказок Gemini о том, когда звать `propose_skill` (описание функции в `TOOLS` достаточно,
  но стоит добавить строку в системный подсказку `voice.system_prompt` после слияния).
- Строки справки консоли (`help`) не обновлял (файл общий).
- Нужно принести: ничего. Для боевого запуска исполнителя на ноутбуке должен быть установлен и авторизован `claude`.
