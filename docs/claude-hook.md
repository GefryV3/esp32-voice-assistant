# Ход сессий Claude Code на экране колонки

Колонка показывает, что делает Claude Code: «работает» (кольцо, процент по списку todo), «ждёт разрешения»,
«задал вопрос», «закончил». Формат сообщения экрану — `task_status` в [protocol-next.md](protocol-next.md).

## Как это устроено

```
Claude Code ──хук──▶ server/tools/claude_hook.py ──ws /admin──▶ сервер: claude_event <json>
                                                                  │  jarvis/claude_status.py
                                                                  ▼
                                                      task_status ─▶ колонка (экран)
                                                      «сессия закончила» ─▶ голос (по настройке)
```

- Хук запускается на пяти событиях и получает JSON в stdin (поля сверены с документацией Claude Code:
  `hook_event_name`, `session_id`, `cwd`, для `PreToolUse` — `tool_name`/`tool_input`, для `Notification` —
  `message`/`notification_type`, для `Stop` — `last_assistant_message`).
- Скрипт отправляет серверу только метки: имя проекта (последняя папка из `cwd`), состояние и статусы todo.
  **Текст промпта, тексты задач и ответ Claude на сервер не уходят** (от ответа берётся лишь последние 200 знаков,
  чтобы увидеть вопросительный знак).
- Транспорт тот же служебный вход `/admin` с токеном, что у `tools/send.py`: никаких новых портов. Адрес и токен
  скрипт читает из `server/.env` (`JARVIS_SERVER`, `JARVIS_PORT`, `JARVIS_DEVICE_TOKEN`).
- Скрипт всегда выходит с кодом 0 и укладывается в ~2 секунды, даже если сервер выключен: хук не мешает работе.

## Как события превращаются в `task_status`

| Событие хука | Что показываем |
|---|---|
| `SessionStart` | ничего (сессия открыта, но не работает) |
| `UserPromptSubmit` | `working`, процент неизвестен (бегущая дуга) |
| `PreToolUse` | `working`; для `TodoWrite` процент = доля `completed` в списке |
| `Notification` `permission_prompt` | `needs_permission` (янтарная пульсация) |
| `Notification` `elicitation_dialog` / `agent_needs_input` / `idle_prompt` | `needs_input` (после `done` `idle_prompt` игнорируется) |
| `Stop` | `done` (зелёная вспышка), а если ответ заканчивается на «?» — `needs_input` |

Несколько сессий: на экране одна. Если какой-то сессии что-то нужно от человека (`needs_*`), показываем её, иначе
самую свежую. Сессия без событий дольше 15 минут считается ушедшей. Одинаковые подряд сообщения не повторяются.

## Подключение (делает пользователь)

Глобальные настройки я не менял. Чтобы включить, добавь в `~/.claude/settings.json` (или в `.claude/settings.json`
нужного проекта) блок `hooks`. Если блок уже есть, добавь записи в существующие массивы:

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "C:\\dev\\jarvis\\server\\.venv\\Scripts\\python.exe C:\\dev\\jarvis\\server\\tools\\claude_hook.py", "timeout": 5 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "C:\\dev\\jarvis\\server\\.venv\\Scripts\\python.exe C:\\dev\\jarvis\\server\\tools\\claude_hook.py", "timeout": 5 }] }],
    "PreToolUse":       [{ "matcher": "*", "hooks": [{ "type": "command", "command": "C:\\dev\\jarvis\\server\\.venv\\Scripts\\python.exe C:\\dev\\jarvis\\server\\tools\\claude_hook.py", "timeout": 5 }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "C:\\dev\\jarvis\\server\\.venv\\Scripts\\python.exe C:\\dev\\jarvis\\server\\tools\\claude_hook.py", "timeout": 5 }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "C:\\dev\\jarvis\\server\\.venv\\Scripts\\python.exe C:\\dev\\jarvis\\server\\tools\\claude_hook.py", "timeout": 5 }] }]
  }
}
```

Пути указывают на основную папку проекта (`C:\dev\jarvis`), потому что `server/.env` есть только там. Python берём
из `server/.venv`, там уже стоит пакет `websockets`.

Проверка без Claude Code (сервер запущен):

```
echo {"hook_event_name":"UserPromptSubmit","session_id":"t","cwd":"C:\\dev\\jarvis"} | server\.venv\Scripts\python server\tools\claude_hook.py
python server\tools\send.py claude
```

Вторая команда покажет сессии и их состояния.

## Настройка «озвучить конец»

`claude speak on` / `claude speak off` (консоль или `tools/send.py`). Если включено, при `done` после задачи дольше
30 секунд колонка сама скажет одной фразой, что сессия закончила (через `Voice.announce`, как рутины). Вопросы
(`needs_input`) вслух не озвучиваются, для них есть янтарная пульсация. Настройка хранится в `brain.db` (`prefs`,
ключ `claude_speak`), по умолчанию выключена.

## Что не проверено вживую

Вся цепочка проверена тестами на подменах (в том числе настоящим локальным WebSocket для пути хук → /admin).
Не проверено: реальные хуки в настоящем Claude Code (форма `tool_input` у `TodoWrite` взята из его документации и
описания, при другой форме процент просто останется «неизвестен»), отрисовка `task_status` на колонке (её делает
сессия display), озвучивание на живой колонке.
