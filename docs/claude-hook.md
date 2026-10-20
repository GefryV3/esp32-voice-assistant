# Claude Code Sessions on the Column Screen

The column shows what Claude Code is doing: "working" (ring, percentage from the todo list), "waiting for permission",
"asked a question", "finished". The screen message format is `task_status` in [protocol-next.md](protocol-next.md).

## How It Works

```
Claude Code ──hook──▶ server/tools/claude_hook.py ──ws /admin──▶ server: claude_event <json>
                                                                  │  jarvis/claude_status.py
                                                                  ▼
                                                      task_status ─▶ column (screen)
                                                      "session finished" ─▶ voice (per settings)
```

- The hook runs on five events and receives JSON in stdin (fields verified against the Claude Code documentation:
  `hook_event_name`, `session_id`, `cwd`, for `PreToolUse` — `tool_name`/`tool_input`, for `Notification` —
  `message`/`notification_type`, for `Stop` — `last_assistant_message`).
- The script sends only labels to the server: project name (last folder from `cwd`), state, and todo statuses.
  **The prompt text, task texts, and Claude's response are not sent to the server** (only the last 200 characters of
  the response are taken to check for a question mark).
- The transport is the same service endpoint `/admin` with a token as `tools/send.py`: no new ports. The script reads
  the address and token from `server/.env` (`JARVIS_SERVER`, `JARVIS_PORT`, `JARVIS_DEVICE_TOKEN`).
- The script always exits with code 0 and completes in ~2 seconds, even if the server is down: the hook does not interfere with work.

## How Events Turn into `task_status`

| Hook Event | What We Show |
|---|---|
| `SessionStart` | nothing (session is open, but not working) |
| `UserPromptSubmit` | `working`, percentage unknown (running arc) |
| `PreToolUse` | `working`; for `TodoWrite`, percentage = fraction of `completed` in the list |
| `Notification` `permission_prompt` | `needs_permission` (amber pulsing) |
| `Notification` `elicitation_dialog` / `agent_needs_input` / `idle_prompt` | `needs_input` (after `done`, `idle_prompt` is ignored) |
| `Stop` | `done` (green flash), and if the response ends with "?", `needs_input` |

Multiple sessions: one on the screen. If some session needs something from a person (`needs_*`), we show it; otherwise,
the freshest one. A session with no events for longer than 15 minutes is considered gone. Consecutive identical messages are not repeated.

## Setup (Done by the User)

I did not change global settings. To enable, add the `hooks` block to `~/.claude/settings.json` (or `.claude/settings.json`
of the desired project). If the block already exists, add entries to the existing arrays:

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

The paths point to the main project folder (`C:\dev\jarvis`) because `server/.env` only exists there. We take Python
from `server/.venv`, where the `websockets` package is already installed.

Checking without Claude Code (server running):

```
echo {"hook_event_name":"UserPromptSubmit","session_id":"t","cwd":"C:\\dev\\jarvis"} | server\.venv\Scripts\python server\tools\claude_hook.py
python server\tools\send.py claude
```

The second command will show the sessions and their states.

## "Announce End" Setting

`claude speak on` / `claude speak off` (console or `tools/send.py`). If enabled, upon `done` after a task lasting longer
than 30 seconds, the column itself will say in a single phrase that the session has finished (via `Voice.announce`, like routines). Questions
(`needs_input`) are not spoken out loud; the amber pulse is used for them. The setting is stored in `brain.db` (`prefs`,
`claude_speak` key), disabled by default.

## What Has Not Been Tested Live

The entire chain has been tested with mock tests (including a real local WebSocket for the hook → /admin path).
Not tested: real hooks in actual Claude Code (the `tool_input` form for `TodoWrite` is taken from its documentation and
description; with a different form, the percentage will simply remain "unknown"), rendering of `task_status` on the column (done by the
display session), speech output on a live column.
