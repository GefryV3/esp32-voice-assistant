import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from jarvis.brain import Brain
from jarvis.skills_loader import SkillLoader, check_source

NOW = datetime(2026, 10, 20, 9, 5)

GOOD = '''
META = {"name": "%(name)s", "description": "тест", "version": "1.0", "permissions": %(perms)s}
TOOLS = [{"name": "%(tool)s", "description": "d", "parameters": {"type": "OBJECT", "properties": {}}}]

async def call(name, args, ctx):
%(body)s
'''


def write(dir: Path, name: str, body: str = "    return {'ok': True, 'v': 1}", perms: str = "[]", tool: str | None = None):
    (dir / f"{name}.py").write_text(GOOD % {"name": name, "perms": perms, "tool": tool or f"{name}_do", "body": body},
                                    encoding="utf-8")


@pytest.fixture
def env(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE prefs (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    notes: list[str] = []
    return d, db, notes, tmp_path


def make(env, **kw):
    d, db, notes, tmp = env
    kw.setdefault("notify", notes.append)
    return SkillLoader(db, dir=d, now=lambda: NOW, data_dir=tmp / "data", **kw)


# ── проверка кода ──

@pytest.mark.parametrize("src,perms,frag", [
    ("import os", (), "os"),
    ("import subprocess", (), "subprocess"),
    ("from os import path", (), "os"),
    ("import requests", (), "net"),
    ("import urllib.request", (), "net"),
    ("open('x')", (), "open"),
    ("eval('1')", (), "eval"),
    ("x = ().__class__", (), "__class__"),
    ("import pathlib", (), "files"),
    ("import jarvis.brain", (), "jarvis"),
    ("def (", (), "синтакс"),
])
def test_check_source_rejects(src, perms, frag):
    assert any(frag in p for p in check_source(src, perms))


def test_check_source_allows_declared_and_plain():
    assert check_source("import random, math, json\nx = 1", ()) == []
    assert check_source("import urllib.request", ("net",)) == []      # уже явно заявлено
    assert check_source("import pathlib", ("files",)) == []
    assert check_source("import ctypes", ("net", "files")) != []      # разрешения опасное не открывают


# ── жизненный цикл ──

async def test_trusted_sample_skills_work_out_of_the_box(env):
    ld = SkillLoader(env[1], now=lambda: NOW)          # настоящая папка jarvis/skills
    ld.load_all()
    assert ld.owns("current_time") and ld.owns("roll_dice")
    out = await ld.call("current_time", {})
    assert out["time"] == "09:05" and out["date"] == "20.10.2026"
    dice = await ld.call("roll_dice", {"sides": 6, "count": 3})
    assert len(dice["rolls"]) == 3 and all(1 <= r <= 6 for r in dice["rolls"])


async def test_new_skill_is_off_until_enabled_then_works(env):
    d = env[0]
    write(d, "hello", "    return {'ok': True, 'hi': ctx.now().hour}")
    ld = make(env)
    ld.load_all()
    assert not ld.owns("hello_do")                      # новый навык по умолчанию выключен
    assert "выключен" in ld.summary()
    assert "включён" in ld.enable("hello")
    assert await ld.call("hello_do", {}) == {"ok": True, "hi": 9}
    assert any(t["name"] == "hello_do" for t in ld.tools())
    ld.disable("hello")
    assert not ld.owns("hello_do") and ld.tools() == []
    # состояние живёт в базе: новый загрузчик на той же базе помнит выбор
    ld2 = make(env)
    ld2.load_all()
    assert not ld2.owns("hello_do")
    ld2.enable("hello")
    ld3 = make(env)
    ld3.load_all()
    assert ld3.owns("hello_do")


async def test_crash_disables_skill_and_conversation_survives(env):
    d, _, notes, _ = env
    write(d, "boom", "    raise RuntimeError('ой')")
    ld = make(env)
    ld.load_all()
    ld.enable("boom")
    out = await ld.call("boom_do", {})
    assert out["ok"] is False and "отключил" in out["error"]
    assert not ld.owns("boom_do")
    assert notes and "boom" in notes[0] and "ой" in notes[0]
    assert "ошибка" in ld.summary()
    assert (await ld.call("boom_do", {}))["ok"] is False        # повторный вызов не падает


async def test_hanging_skill_times_out(env):
    d, _, notes, _ = env
    write(d, "slow", "    import asyncio\n    await asyncio.sleep(5)\n    return {}")
    ld = make(env, call_timeout=0.05)
    ld.load_all()
    ld.enable("slow")
    out = await ld.call("slow_do", {})
    assert "завис" in out["error"] and not ld.owns("slow_do") and notes


async def test_non_dict_result_counts_as_crash(env):
    write(env[0], "bad", "    return 5")
    ld = make(env)
    ld.load_all()
    ld.enable("bad")
    assert (await ld.call("bad_do", {}))["ok"] is False and not ld.owns("bad_do")


def test_unsafe_or_broken_skills_do_not_load(env):
    d, _, notes, _ = env
    write(d, "evil", "    import os\n    return {}")
    (d / "broken.py").write_text("def (:", encoding="utf-8")
    (d / "nameless.py").write_text("META = {'name': 'other'}\nTOOLS = []\n", encoding="utf-8")
    ld = make(env)
    ld.load_all()
    for n in ("evil", "broken", "nameless"):
        assert "не включился" in ld.enable(n)
    assert ld.tools() == [] and len(notes) == 3


def test_tool_name_conflicts_are_refused(env):
    d = env[0]
    write(d, "one", tool="shared")
    write(d, "two", tool="shared")
    write(d, "three", tool="remember")
    ld = make(env, reserved={"remember"})
    ld.load_all()
    assert "включён" in ld.enable("one")
    assert "занято" in ld.enable("two")
    assert "занято" in ld.enable("three")


async def test_reload_picks_up_new_code(env):
    d = env[0]
    write(d, "ver", "    return {'v': 1}")
    ld = make(env)
    ld.load_all()
    ld.enable("ver")
    assert (await ld.call("ver_do", {}))["v"] == 1
    write(d, "ver", "    return {'v': 2}")
    assert "перезагружен" in ld.reload("ver")
    assert (await ld.call("ver_do", {}))["v"] == 2
    assert "ver: перезагружен" in ld.reload()            # без имени — все


def test_file_changed_after_enable_is_not_loaded_on_restart(env):
    d, _, notes, _ = env
    write(d, "ver", "    return {'v': 1}")
    ld = make(env)
    ld.load_all()
    ld.enable("ver")
    write(d, "ver", "    return {'v': 666}")           # кто-то подменил файл
    ld2 = make(env)
    ld2.load_all()
    assert not ld2.owns("ver_do") and "изменился" in notes[-1]
    assert "перезагружен" in ld2.reload("ver")           # явная команда человека принимает новую версию
    assert ld2.owns("ver_do")


def test_reload_retries_a_crashed_skill_after_fix(env):
    d = env[0]
    write(d, "fix", "    return {}")
    ld = make(env)
    ld.load_all()
    ld.enable("fix")
    ld._fail(ld.skills["fix"], "упал")
    assert not ld.owns("fix_do")
    ld.reload("fix")
    assert ld.owns("fix_do")


# ── возможности через ctx ──

async def test_ctx_capabilities_follow_permissions(env):
    d = env[0]
    body = ("    return {'ha': ctx.ha is None, 'google': ctx.google is None,\n"
            "            'data': await ctx.http_get_json('https://x.test/a', {'q': '1'})}")
    write(d, "plain", body)
    write(d, "netty", body.replace("'ha': ctx.ha is None", "'ha': ctx.ha"), perms='["net", "ha"]', tool="netty_do")
    calls: list[str] = []

    def fetch(url):
        calls.append(url)
        return b'{"a": 1}'

    ld = make(env, services={"ha": "HA", "google": "G"}, fetch=fetch)
    ld.load_all()
    ld.enable("plain")
    ld.enable("netty")
    out = await ld.call("plain_do", {})                  # без net вызов падает, навык отключается
    assert out["ok"] is False and "net" in out["error"]
    out = await ld.call("netty_do", {})
    assert out == {"ha": "HA", "google": True, "data": {"a": 1}}
    assert calls == ["https://x.test/a?q=1"]


async def test_ctx_net_is_https_only_and_files_are_confined(env):
    d, _, _, tmp = env
    write(d, "io", "    ctx.write_text('a/b.txt', 'hi')\n    r = ctx.read_text('a/b.txt')\n"
                   "    try:\n        ctx.read_text('../../secret')\n    except PermissionError:\n        r += ' blocked'\n"
                   "    try:\n        await ctx.http_get_json('http://x.test')\n    except PermissionError:\n        r += ' https'\n"
                   "    return {'r': r}", perms='["files", "net"]')
    ld = make(env, fetch=lambda url: b"{}")
    ld.load_all()
    ld.enable("io")
    assert await ld.call("io_do", {}) == {"r": "hi blocked https"}
    assert (tmp / "data" / "skills" / "io" / "a" / "b.txt").read_text() == "hi"


async def test_ctx_prefs_are_namespaced(env):
    d, db, _, _ = env
    write(d, "pf", "    ctx.set_pref('k', 'v')\n    return {'v': ctx.pref('k')}")
    ld = make(env)
    ld.load_all()
    ld.enable("pf")
    assert (await ld.call("pf_do", {})) == {"v": "v"}
    assert db.execute("SELECT key FROM prefs").fetchall() == [("skill.pf.k",)]


# ── управление голосом/консолью и подхват в Brain ──

async def test_manage_tool_and_commands(env):
    write(env[0], "hello")
    ld = make(env)
    ld.load_all()
    assert (await ld.manage({"action": "enable", "name": "hello"}))["ok"]
    listing = (await ld.manage({"action": "list"}))["skills"]
    assert {"name": "hello", "enabled": True, "description": "тест", "error": ""} in listing
    assert ld.command("skills disable hello") == "навык hello выключен"
    assert "нет в папке" in ld.command("skills enable nosuch")
    assert (await ld.manage({"action": "enable", "name": "nosuch"}))["ok"] is False
    assert (await ld.manage({"action": "wat"}))["ok"] is False
    assert ld.command("что-то другое") is None


async def test_brain_exposes_skills_and_isolates_failures(tmp_path):
    d = tmp_path / "skills"
    d.mkdir()
    write(d, "boom", "    raise RuntimeError('x')")
    brain = Brain(tmp_path / "b.db", now=lambda: NOW, skills_dir=d)
    names = {t["name"] for t in brain.tools()}
    assert {"skills_manage", "propose_skill", "skill_decision", "remember"} <= names
    assert "current_time" not in names                  # в этой папке часов нет
    sent = []

    async def send(m):
        sent.append(m)

    brain.skills.enable("boom")
    assert "boom_do" in {t["name"] for t in brain.tools()}
    out = await brain.call("boom_do", {}, send)
    assert out["ok"] is False
    assert "boom_do" not in {t["name"] for t in brain.tools()}      # разговор жив, навык выключен
    assert (await brain.call("skills_manage", {"action": "list"}, send))["ok"]


async def test_brain_default_skills_replace_builtin_time_and_dice(tmp_path):
    brain = Brain(tmp_path / "b.db", now=lambda: NOW)
    names = {t["name"] for t in brain.tools()}
    assert {"current_time", "roll_dice"} <= names
    assert len([t for t in brain.tools() if t["name"] == "current_time"]) == 1
    out = await brain.call("current_time", {}, lambda m: asyncio.sleep(0))
    assert out["time"] == "09:05"
    json.dumps(brain.tools())                           # декларации сериализуются для Gemini
