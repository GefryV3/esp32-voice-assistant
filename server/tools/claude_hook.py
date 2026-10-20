"""Хук Claude Code → колонка Jarvis: ход сессии на экране (docs/claude-hook.md).

Claude Code запускает этот скрипт на событиях SessionStart, UserPromptSubmit, PreToolUse, Notification, Stop и
передаёт JSON в stdin. Скрипт превращает его в короткое событие и отправляет серверу через служебный вход /admin
командой `claude_event <json>`. Сервер сам решает, что показать (task_status).

Правила: всегда выходит с кодом 0 и быстро (не больше ~2 с), даже если сервер недоступен: хук не должен мешать
работе Claude Code. Промпт и ответ Claude на сервер не уходят, только метки (проект, состояние, число todo).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

TIMEOUT_S = 2.0


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def build_event(data: dict) -> dict | None:
    """Вход хука → событие для сервера (None — это событие не нужно)."""
    name = str(data.get("hook_event_name", ""))
    if name not in ("SessionStart", "UserPromptSubmit", "PreToolUse", "Notification", "Stop"):
        return None
    cwd = str(data.get("cwd", "")).replace("\\", "/").rstrip("/")
    ev: dict = {"event": name, "session": str(data.get("session_id", ""))[:40], "project": cwd.rsplit("/", 1)[-1][:31]}
    if name == "PreToolUse" and data.get("tool_name") == "TodoWrite":
        todos = (data.get("tool_input") or {}).get("todos")
        if isinstance(todos, list):
            ev["todos"] = [{"status": str(t.get("status", ""))} for t in todos[:100] if isinstance(t, dict)]
    elif name == "Notification":
        ev["ntype"] = str(data.get("notification_type", ""))
        ev["message"] = str(data.get("message", ""))[:120]
    elif name == "Stop":
        ev["last"] = str(data.get("last_assistant_message", ""))[-200:]      # хвост: нужен только знак вопроса
    return ev


async def send(url: str, token: str, ev: dict) -> None:
    from websockets.asyncio.client import connect      # noqa: PLC0415  (нет пакета — молча выходим выше)
    async with connect(url, open_timeout=TIMEOUT_S, close_timeout=0.5) as ws:
        await ws.send(token)
        await ws.send("claude_event " + json.dumps(ev, ensure_ascii=False))
        await asyncio.wait_for(ws.recv(), TIMEOUT_S)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
        ev = build_event(data) if isinstance(data, dict) else None
        if ev is None:
            return
        env = read_env(Path(__file__).resolve().parents[1] / ".env")
        url = f"ws://{env.get('JARVIS_SERVER', '127.0.0.1')}:{env.get('JARVIS_PORT', '8765')}/admin"
        asyncio.run(asyncio.wait_for(send(url, env.get("JARVIS_DEVICE_TOKEN", ""), ev), TIMEOUT_S + 1))
    except BaseException:       # хук никогда не мешает Claude Code
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
