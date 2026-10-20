"""Ход сессий Claude Code на экране колонки (хук прогресса, docs/claude-hook.md).

Хуки Claude Code (tools/claude_hook.py) присылают события через служебный вход /admin командой
`claude_event <json>`. Здесь они превращаются в `task_status` (docs/protocol-next.md):
working (процент по todo, если известен), needs_permission, needs_input, done.

Выбор: на экране одна задача, поэтому показываем сессию, которой что-то нужно от человека
(needs_*), иначе самую свежую. Сессия без событий дольше STALE_S считается ушедшей.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import protocol

log = logging.getLogger(__name__)

STALE_S = 15 * 60
SPEAK_MIN_S = 30        # вслух говорим только про задачи, шедшие дольше 30 с
MAX_SESSIONS = 20
NEEDS = ("needs_input", "needs_permission")


@dataclass
class Session:
    id: str
    label: str
    state: str = "idle"
    pct: int = -1
    last: float = 0.0
    started: float = 0.0


def todo_pct(todos: Any) -> int:
    """Процент выполненных по списку todo из TodoWrite; −1, если списка нет."""
    if not isinstance(todos, list) or not todos:
        return -1
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    return round(100 * done / len(todos))


class ClaudeStatus:
    def __init__(self, send: Callable[[dict], Awaitable[None]],
                 announce: Callable[[str], Awaitable[bool]] | None = None,
                 get_pref: Callable[[str, str], str] | None = None,
                 set_pref: Callable[[str, str], None] | None = None,
                 clock: Callable[[], float] = time.time):
        self.send, self.announce, self._get, self._set, self.clock = send, announce, get_pref, set_pref, clock
        self.sessions: dict[str, Session] = {}
        self._shown: dict | None = protocol.task_status("idle")    # на экране пока пусто

    # ── настройка «говорить вслух» ──

    @property
    def speak(self) -> bool:
        return bool(self._get and self._get("claude_speak", "0") == "1")

    # ── события ──

    def _session(self, ev: dict[str, Any]) -> Session:
        sid = str(ev.get("session") or "default")
        now = self.clock()
        s = self.sessions.get(sid)
        if s is None:
            if len(self.sessions) >= MAX_SESSIONS:
                self.sessions.pop(min(self.sessions, key=lambda k: self.sessions[k].last))
            s = self.sessions[sid] = Session(sid, str(ev.get("project") or "Claude"))
        if ev.get("project"):
            s.label = str(ev["project"])
        s.last = now
        return s

    async def handle(self, ev: dict[str, Any]) -> str:
        """Одно событие хука → обновить сессию → показать на экране. Возвращает короткий итог."""
        name = str(ev.get("event", ""))
        s = self._session(ev)
        now = self.clock()
        speak_done = False
        if name == "SessionStart":
            s.state, s.pct = "idle", -1
        elif name == "UserPromptSubmit":
            s.state, s.pct, s.started = "working", -1, now
        elif name == "PreToolUse":
            if s.state != "working":
                s.state = "working"
                s.started = s.started or now
            if ev.get("todos") is not None:
                s.pct = todo_pct(ev["todos"])
        elif name == "Notification":
            kind = str(ev.get("ntype", ""))
            text = str(ev.get("message", "")).lower()
            if kind == "permission_prompt" or (not kind and "permission" in text):
                s.state = "needs_permission"
            elif kind in ("elicitation_dialog", "agent_needs_input") or (kind == "idle_prompt" and s.state != "done"):
                s.state = "needs_input"
        elif name == "Stop":
            last = str(ev.get("last", "")).rstrip()
            s.state = "needs_input" if last.endswith("?") else "done"
            s.pct = 100 if s.state == "done" else s.pct
            speak_done = s.state == "done" and self.speak and s.started and now - s.started >= SPEAK_MIN_S
            s.started = 0.0
        else:
            return f"событие {name!r} не учитываю"
        await self._show()
        if speak_done and self.announce is not None:
            await self.announce(f"Сессия Claude Code «{s.label}» закончила работу. "
                                "Скажи об этом одной короткой фразой.")
        return f"{s.label}: {s.state}" + (f" {s.pct}%" if s.pct >= 0 else "")

    def _pick(self) -> Session | None:
        live = [s for s in self.sessions.values() if self.clock() - s.last < STALE_S and s.state != "idle"]
        if not live:
            return None
        needing = [s for s in live if s.state in NEEDS]
        return max(needing or live, key=lambda s: s.last)

    async def _show(self) -> None:
        s = self._pick()
        msg = protocol.task_status("idle") if s is None else protocol.task_status(s.state, s.label, s.pct)
        if msg == self._shown:
            return
        self._shown = msg
        await self.send(msg)

    # ── консоль ──

    async def command(self, line: str) -> str | None:
        """`claude_event <json>` (от хука), `claude` (кто в работе), `claude speak on|off`."""
        head, _, rest = line.strip().partition(" ")
        if head == "claude_event":
            try:
                ev = json.loads(rest)
                if not isinstance(ev, dict):
                    raise ValueError
            except ValueError:
                return "claude_event: нужен JSON-объект"
            return await self.handle(ev)
        if head != "claude":
            return None
        arg = rest.strip().lower()
        if arg.startswith("speak"):
            if self._set is None:
                return "настройки недоступны"
            on = arg.split()[-1] in ("on", "1", "да")
            self._set("claude_speak", "1" if on else "0")
            return "озвучивание конца сессий: " + ("вкл" if on else "выкл")
        rows = [f"{s.label}: {s.state}" + (f" {s.pct}%" if s.pct >= 0 else "") for s in self.sessions.values()]
        return "\n".join(rows) + f"\nозвучивать конец: {'да' if self.speak else 'нет'}" if rows else \
            f"сессий нет; озвучивать конец: {'да' if self.speak else 'нет'}"
