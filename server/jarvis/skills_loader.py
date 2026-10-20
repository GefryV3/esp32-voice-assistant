"""Загрузчик навыков-плагинов (модель безопасности: docs/skills.md).

Навык — модуль в `jarvis/skills/` с META, TOOLS и `async def call(name, args, ctx)`.
Загрузчик находит файлы, включает и выключает навыки (состояние в brain.db, таблица `skills`),
перезагружает их на лету и изолирует ошибки: навык упал или завис — он отключается, пользователю
уходит сообщение, разговор продолжается.

Защита (честно: это не песочница, а несколько заслонов):
1. Статическая проверка исходника (`check_source`): запрещены опасные импорты и вызовы, а сеть,
   файлы, умный дом и Google открываются только заявленным в META["permissions"].
2. Возможности выдаются через `ctx`: без разрешения `ha` в ctx нет ha, и так далее.
3. Новый навык по умолчанию выключен (включены только доверенные, лежавшие в репозитории с начала).
   Включает человек: Telegram `/skills enable имя` или консоль.
4. Если файл изменился после включения, при запуске сервера он не загружается, пока человек
   не включит заново (хэш sha256 хранится в базе).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import re
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SKILLS_DIR = Path(__file__).parent / "skills"
TRUSTED = frozenset({"clock", "dice"})      # лежали в репозитории с самого начала: включены сразу
PERMISSIONS = ("net", "ha", "google", "files")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")
CALL_TIMEOUT_S = 20.0
NET_TIMEOUT_S = 10.0
NET_MAX_BYTES = 1_000_000
FILE_MAX_BYTES = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY, enabled INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '', sha TEXT NOT NULL DEFAULT '');
"""

MANAGE_TOOLS: list[dict[str, Any]] = [
    {"name": "skills_manage", "description": "Навыки Маги (плагины). action: list — какие есть и включены ли; "
     "disable — выключить навык; reload — перечитать файл навыка после правки (name пустое = все); "
     "enable — включить (только если пользователь прямо сказал «включи навык …»).",
     "parameters": {"type": "OBJECT", "properties": {
         "action": {"type": "STRING"}, "name": {"type": "STRING"}}, "required": ["action"]}},
]

# ── статическая проверка ──

ALWAYS_FORBIDDEN_IMPORTS = frozenset({
    "subprocess", "os", "sys", "socket", "ctypes", "shutil", "importlib", "pickle", "marshal", "multiprocessing",
    "signal", "builtins", "tempfile", "runpy", "code", "pty", "resource", "gc", "inspect", "types",
    "threading", "concurrent", "asyncio.subprocess", "webbrowser", "smtplib", "ftplib", "telnetlib", "winreg",
})
NET_IMPORTS = frozenset({"urllib", "http", "requests", "aiohttp", "httpx", "websockets", "ssl", "xmlrpc"})
FILE_IMPORTS = frozenset({"pathlib", "io", "glob", "fnmatch"})
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "globals", "locals", "vars",
                             "getattr", "setattr", "delattr", "breakpoint", "input"})
FORBIDDEN_ATTRS = frozenset({"__class__", "__subclasses__", "__globals__", "__builtins__", "__dict__",
                             "__bases__", "__mro__", "__code__", "__closure__", "__import__", "__loader__",
                             "__spec__", "system", "popen", "spawn", "fork", "execv", "execve"})


def check_source(source: str, permissions: tuple[str, ...] | list[str] = ()) -> list[str]:
    """Список нарушений (пустой — чисто). Не песочница, а заслон от очевидного."""
    perms = set(permissions)
    problems: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"синтаксическая ошибка: {e.msg} (строка {e.lineno})"]
    for node in ast.walk(tree):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append("относительный импорт запрещён")
            mods = [node.module or ""]
        for mod in mods:
            top = mod.split(".")[0]
            if mod in ALWAYS_FORBIDDEN_IMPORTS or top in ALWAYS_FORBIDDEN_IMPORTS:
                problems.append(f"импорт {mod} запрещён")
            elif top in NET_IMPORTS and "net" not in perms:
                problems.append(f"импорт {mod} нужен разрешению net (используй ctx.http_get_json)")
            elif top in FILE_IMPORTS and "files" not in perms:
                problems.append(f"импорт {mod} нужен разрешению files")
            elif top == "jarvis":
                problems.append("навык не импортирует jarvis: всё нужное в ctx")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in FORBIDDEN_CALLS:
                problems.append(f"вызов {node.func.id}() запрещён")
            if node.func.id == "open":
                problems.append("open() запрещён (используй ctx.read_text / ctx.write_text с разрешением files)")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            problems.append(f"атрибут .{node.attr} запрещён")
        if isinstance(node, ast.Name) and node.id in ("__builtins__", "__loader__", "__spec__"):
            problems.append(f"имя {node.id} запрещено")
    return sorted(set(problems))


def static_meta(source: str) -> dict[str, Any]:
    """META без исполнения кода (для списка выключенных навыков)."""
    try:
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "META" for t in node.targets):
                meta = ast.literal_eval(node.value)
                return meta if isinstance(meta, dict) else {}
    except (SyntaxError, ValueError):
        pass
    return {}


# ── контекст навыка ──

class SkillContext:
    """Всё, что навык получает от сервера. Чего нет в ctx, того навык сделать не может."""

    def __init__(self, name: str, permissions: tuple[str, ...], *, now: Callable[[], datetime],
                 db: sqlite3.Connection, data_dir: Path | None, ha: Any = None, google: Any = None,
                 fetch: Callable[[str], bytes] | None = None):
        self.name = name
        self.permissions = permissions
        self.now = now
        self.log = logging.getLogger(f"jarvis.skill.{name}")
        self._db = db
        self._dir = (data_dir / "skills" / name) if data_dir else None
        self._fetch = fetch or _fetch_https
        self.ha = ha if "ha" in permissions else None
        self.google = google if "google" in permissions else None

    # настройки навыка (в prefs с префиксом, чужие ключи недоступны)
    def pref(self, key: str, default: str = "") -> str:
        row = self._db.execute("SELECT value FROM prefs WHERE key = ?", (f"skill.{self.name}.{key}",)).fetchone()
        return row[0] if row else default

    def set_pref(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO prefs (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                         "value = excluded.value", (f"skill.{self.name}.{key}", str(value)[:2000]))
        self._db.commit()

    # сеть: только https, ответ до 1 МБ, таймаут 10 с
    async def http_get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        if "net" not in self.permissions:
            raise PermissionError("нет разрешения net")
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        if urllib.parse.urlparse(url).scheme != "https":
            raise PermissionError("только https")
        data = await asyncio.to_thread(self._fetch, url)
        return json.loads(data)

    # файлы: только папка data/skills/<имя>/
    def _path(self, rel: str) -> Path:
        if "files" not in self.permissions or self._dir is None:
            raise PermissionError("нет разрешения files")
        p = (self._dir / rel).resolve()
        if self._dir.resolve() not in p.parents:
            raise PermissionError("файл вне папки навыка")
        return p

    def read_text(self, rel: str) -> str:
        p = self._path(rel)
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def write_text(self, rel: str, text: str) -> None:
        if len(text.encode()) > FILE_MAX_BYTES:
            raise ValueError("файл слишком большой")
        p = self._path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _fetch_https(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "jarvis-skill"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT_S) as r:
        return r.read(NET_MAX_BYTES)


# ── загрузчик ──

@dataclass
class Skill:
    name: str
    path: Path
    description: str = ""
    version: str = ""
    permissions: tuple[str, ...] = ()
    enabled: bool = False
    loaded: bool = False
    error: str = ""
    sha: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    module: Any = None
    ctx: SkillContext | None = None

    def line(self) -> str:
        state = "включён" if self.loaded else ("ошибка: " + self.error if self.error else
                                                ("выключен" if not self.enabled else "не загружен"))
        perms = f" [{','.join(self.permissions)}]" if self.permissions else ""
        return f"{self.name} {self.version}{perms} — {state}. {self.description}".strip()


Notify = Callable[[str], Awaitable[None] | None]


class SkillLoader:
    def __init__(self, db: sqlite3.Connection, *, dir: Path = SKILLS_DIR, now: Callable[[], datetime] = datetime.now,
                 data_dir: Path | None = None, services: dict[str, Any] | None = None,
                 reserved: set[str] | frozenset[str] = frozenset(), notify: Notify | None = None,
                 trusted: frozenset[str] = TRUSTED, call_timeout: float = CALL_TIMEOUT_S,
                 fetch: Callable[[str], bytes] | None = None):
        self.db, self.dir, self.now, self.data_dir = db, Path(dir), now, data_dir
        self.services = services if services is not None else {}
        self.reserved = reserved
        self.notify = notify
        self.trusted, self.call_timeout, self.fetch = trusted, call_timeout, fetch
        self.skills: dict[str, Skill] = {}
        self._tool_owner: dict[str, str] = {}
        db.executescript(SCHEMA)
        db.commit()

    # ── состояние в базе ──

    def _row(self, name: str) -> tuple[bool, str, str] | None:
        r = self.db.execute("SELECT enabled, error, sha FROM skills WHERE name = ?", (name,)).fetchone()
        return (bool(r[0]), r[1], r[2]) if r else None

    def _save(self, name: str, enabled: bool, error: str = "", version: str = "", sha: str | None = None) -> None:
        old = self._row(name)
        sha = sha if sha is not None else (old[2] if old else "")
        self.db.execute("INSERT INTO skills (name, enabled, error, version, sha) VALUES (?, ?, ?, ?, ?) "
                        "ON CONFLICT(name) DO UPDATE SET enabled = excluded.enabled, error = excluded.error, "
                        "version = excluded.version, sha = excluded.sha",
                        (name, int(enabled), error[:300], version, sha))
        self.db.commit()

    def _tell(self, text: str) -> None:
        log.warning("навыки: %s", text)
        if self.notify is None:
            return
        try:
            res = self.notify(text)
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)
        except Exception:
            log.exception("навыки: уведомление не ушло")

    # ── поиск и загрузка ──

    def discover(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("*.py") if not p.name.startswith("_") and NAME_RE.match(p.stem))

    def load_all(self) -> None:
        """При старте: включённые (и доверенные) грузим, остальные только описываем без запуска."""
        self.skills.clear()
        self._tool_owner.clear()
        for name in self.discover():
            row = self._row(name)
            enabled = row[0] if row else name in self.trusted
            path = self.dir / f"{name}.py"
            source = path.read_text(encoding="utf-8")
            sha = hashlib.sha256(source.encode()).hexdigest()
            meta = static_meta(source)
            sk = Skill(name, path, str(meta.get("description", "")), str(meta.get("version", "")),
                       tuple(p for p in meta.get("permissions", ()) if p in PERMISSIONS), enabled=enabled, sha=sha)
            self.skills[name] = sk
            if enabled and row and row[2] and row[2] != sha and name not in self.trusted:
                sk.enabled, sk.error = False, "файл изменён после включения: проверь и включи заново"
                self._save(name, False, sk.error, sk.version)
                self._tell(f"навык {name} изменился с прошлого включения, оставил его выключенным")
            elif enabled:
                self._load(sk, source)

    def _load(self, sk: Skill, source: str | None = None) -> bool:
        """Прочитать, проверить и импортировать навык. Ошибка: навык выключается и сообщается."""
        try:
            source = source if source is not None else sk.path.read_text(encoding="utf-8")
            sk.sha = hashlib.sha256(source.encode()).hexdigest()
            meta = static_meta(source)
            if meta.get("name") != sk.name:
                raise ValueError(f"META['name'] должно совпадать с именем файла ({sk.name})")
            perms = tuple(meta.get("permissions", ()))
            bad_perm = [p for p in perms if p not in PERMISSIONS]
            if bad_perm:
                raise ValueError(f"неизвестные разрешения: {bad_perm}")
            problems = check_source(source, perms)
            if problems:
                raise ValueError("проверка кода: " + "; ".join(problems[:5]))
            spec = importlib.util.spec_from_file_location(f"jarvis_skill_{sk.name}", sk.path)
            module = importlib.util.module_from_spec(spec)
            exec(compile(source, str(sk.path), "exec"), module.__dict__)    # исходник уже прошёл check_source
            tools = list(getattr(module, "TOOLS", []))
            if not callable(getattr(module, "call", None)) or not inspect.iscoroutinefunction(module.call):
                raise ValueError("нет async def call(name, args, ctx)")
            names = [t.get("name", "") for t in tools]
            if not tools or not all(isinstance(n, str) and n for n in names):
                raise ValueError("TOOLS пуст или без имён")
            for n in names:
                if n in self.reserved or n in self._tool_owner and self._tool_owner[n] != sk.name:
                    raise ValueError(f"имя функции {n} уже занято")
            sk.module, sk.tools, sk.permissions = module, tools, perms
            sk.description, sk.version = str(meta.get("description", "")), str(meta.get("version", ""))
            sk.ctx = SkillContext(sk.name, perms, now=self.now, db=self.db, data_dir=self.data_dir,
                                  ha=self.services.get("ha"), google=self.services.get("google"), fetch=self.fetch)
            for n in names:
                self._tool_owner[n] = sk.name
            sk.loaded, sk.enabled, sk.error = True, True, ""
            return True
        except Exception as e:
            self._fail(sk, f"не загрузился: {e}")
            return False

    def _fail(self, sk: Skill, reason: str) -> None:
        self._unload(sk)
        sk.enabled, sk.error = False, reason[:300]
        self._save(sk.name, False, sk.error, sk.version)
        self._tell(f"навык {sk.name} отключён: {sk.error}")

    def _unload(self, sk: Skill) -> None:
        for n in [n for n, o in self._tool_owner.items() if o == sk.name]:
            del self._tool_owner[n]
        sk.loaded, sk.module, sk.ctx, sk.tools = False, None, None, []

    # ── управление ──

    def _find(self, name: str) -> Skill:
        name = name.strip().lower()
        if name not in self.skills and name in self.discover():
            self.skills[name] = Skill(name, self.dir / f"{name}.py")
        if name not in self.skills:
            raise KeyError(f"навыка «{name}» нет в папке навыков (файл не слит или не выложен?)")
        return self.skills[name]

    def enable(self, name: str) -> str:
        sk = self._find(name)
        self._unload(sk)
        if not self._load(sk):
            return f"не включился: {sk.error}"
        self._save(sk.name, True, "", sk.version, sk.sha)
        return f"навык {sk.name} включён"

    def disable(self, name: str) -> str:
        sk = self._find(name)
        self._unload(sk)
        sk.enabled, sk.error = False, ""
        self._save(sk.name, False, "", sk.version)
        return f"навык {sk.name} выключен"

    def reload(self, name: str = "") -> str:
        """Перечитать файлы. Явная перезагрузка человеком принимает новый хэш файла."""
        names = [self._find(name).name] if name.strip() else self.discover()
        out = []
        for n in names:
            sk = self._find(n)
            row = self._row(n)
            was_on = (row[0] if row else n in self.trusted) or bool(row and row[1])   # упавшие пробуем снова
            self._unload(sk)
            if was_on:
                out.append(f"{n}: " + ("перезагружен" if self._load(sk) else "не загрузился"))
                if sk.loaded:
                    self._save(n, True, "", sk.version, sk.sha)
            else:
                out.append(f"{n}: выключен")
        return "; ".join(out) or "навыков нет"

    def listing(self) -> list[Skill]:
        for n in self.discover():
            self._find(n)
        return [self.skills[n] for n in sorted(self.skills) if (self.dir / f"{n}.py").exists()]

    def summary(self) -> str:
        return "\n".join(s.line() for s in self.listing()) or "навыков нет"

    # ── вызовы ──

    def tools(self) -> list[dict[str, Any]]:
        return [t for s in self.skills.values() if s.loaded for t in s.tools]

    def owns(self, tool: str) -> bool:
        return tool in self._tool_owner

    async def call(self, tool: str, args: dict[str, Any]) -> dict:
        sk = self.skills.get(self._tool_owner.get(tool, ""))
        if sk is None or not sk.loaded:
            return {"ok": False, "error": f"нет такого инструмента: {tool}"}
        try:
            out = await asyncio.wait_for(sk.module.call(tool, args, sk.ctx), self.call_timeout)
            if not isinstance(out, dict):
                raise TypeError(f"навык вернул {type(out).__name__}, нужен dict")
            json.dumps(out, ensure_ascii=False)
            return out
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = "завис" if isinstance(e, TimeoutError) else f"упал: {e!r}"
            self._fail(sk, reason)
            return {"ok": False, "error": f"навык {sk.name} {reason}, я его отключил"}

    async def manage(self, args: dict[str, Any]) -> dict:
        """Инструмент skills_manage для голоса и Telegram."""
        action = str(args.get("action", "")).strip().lower()
        name = str(args.get("name", "")).strip()
        try:
            if action == "list":
                return {"ok": True, "skills": [
                    {"name": s.name, "enabled": s.loaded, "description": s.description, "error": s.error}
                    for s in self.listing()]}
            if action == "disable" and name:
                return {"ok": True, "result": self.disable(name)}
            if action == "enable" and name:
                return {"ok": True, "result": self.enable(name)}
            if action == "reload":
                return {"ok": True, "result": self.reload(name)}
        except KeyError as e:
            return {"ok": False, "error": e.args[0]}
        return {"ok": False, "error": "action: list, enable, disable, reload (для enable и disable нужно name)"}

    def command(self, line: str) -> str | None:
        """Консоль и Telegram: `skills`, `skills enable|disable|reload [имя]`."""
        parts = line.split()
        if not parts or parts[0].lower().lstrip("/") != "skills":
            return None
        if len(parts) == 1 or parts[1] == "list":
            return self.summary()
        action, name = parts[1].lower(), (parts[2] if len(parts) > 2 else "")
        try:
            if action == "enable" and name:
                return self.enable(name)
            if action == "disable" and name:
                return self.disable(name)
            if action == "reload":
                return self.reload(name)
        except KeyError as e:
            return e.args[0]
        return "skills [list] | skills enable|disable <имя> | skills reload [имя]"
