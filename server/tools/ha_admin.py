"""Управление аддоном Jarvis в Home Assistant без интерфейса: через supervisor/api по WebSocket.

    python tools/ha_admin.py info              версия, состояние, есть ли обновление
    python tools/ha_admin.py reload            заставить HA перечитать локальные аддоны
    python tools/ha_admin.py update            reload + обновить до версии из папки аддона
    python tools/ha_admin.py rebuild           пересобрать ту же версию
    python tools/ha_admin.py restart           перезапустить
    python tools/ha_admin.py api /jobs/info    любой GET к Supervisor (диагностика)
    python tools/ha_admin.py logs [строк]      последние строки журнала (по умолчанию 40)

Адрес и токен — JARVIS_HA_URL и JARVIS_HA_TOKEN из server/.env (токен: профиль HA →
Безопасность → Долгосрочные токены; пользователь должен быть администратором).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from websockets.asyncio.client import connect

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from jarvis.config import read_env_file  # noqa: E402

ADDON = "local_jarvis"
TIMEOUT_S = 600.0      # сборка контейнера бывает долгой


class Admin:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0

    async def call(self, endpoint: str, method: str = "get", timeout: float = 60.0, **data):
        self._id += 1
        msg = {"id": self._id, "type": "supervisor/api", "endpoint": endpoint, "method": method}
        if data:
            msg["data"] = data
        await self.ws.send(json.dumps(msg))
        async with asyncio.timeout(timeout):
            while True:
                reply = json.loads(await self.ws.recv())
                if reply.get("id") == self._id:
                    break
        if not reply.get("success"):
            raise SystemExit(f"{endpoint}: {reply.get('error') or reply}")
        return reply.get("result")


async def open_admin(url: str, token: str) -> Admin:
    ws = await connect(url, max_size=None, open_timeout=15)
    if json.loads(await ws.recv()).get("type") != "auth_required":
        raise SystemExit("HA ответил неожиданно")
    await ws.send(json.dumps({"type": "auth", "access_token": token}))
    if json.loads(await ws.recv()).get("type") != "auth_ok":
        raise SystemExit("HA не принял токен (JARVIS_HA_TOKEN в server/.env)")
    return Admin(ws)


async def reload_store(a: Admin) -> None:
    """Перечитать локальные аддоны: в разных версиях Supervisor ручка называется по-разному."""
    for endpoint in ("/store/reload", "/addons/reload"):
        try:
            await a.call(endpoint, "post")
            return
        except SystemExit as e:
            print(f"{e} — пробую другую ручку")


async def info(a: Admin) -> dict:
    return await a.call(f"/addons/{ADDON}/info")


def show(i: dict) -> str:
    return (f"{i.get('name')}: установлена {i.get('version')}, последняя {i.get('version_latest')}, "
            f"состояние {i.get('state')}, обновление {'есть' if i.get('update_available') else 'нет'}")


async def main(argv: list[str]) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = argv[0] if argv else "info"
    env = read_env_file(SERVER / ".env")
    url, token = env.get("JARVIS_HA_URL", ""), env.get("JARVIS_HA_TOKEN", "")
    if not url or not token:
        sys.exit("в server/.env нет JARVIS_HA_URL и JARVIS_HA_TOKEN")
    a = await open_admin(url, token)
    try:
        if cmd == "info":
            print(show(await info(a)))
        elif cmd == "reload":
            await reload_store(a)
            print(show(await info(a)))
        elif cmd == "update":
            await reload_store(a)
            i = await info(a)
            print(show(i))
            if not i.get("update_available"):
                print("обновлять нечего (проверь, что deploy_addon.py выложил новую версию)")
                return
            await a.call(f"/addons/{ADDON}/update", "post", TIMEOUT_S)
            print(show(await info(a)))
        elif cmd == "rebuild":
            await a.call(f"/addons/{ADDON}/rebuild", "post", TIMEOUT_S)
            print(show(await info(a)))
        elif cmd == "restart":
            await a.call(f"/addons/{ADDON}/restart", "post", TIMEOUT_S)
            print(show(await info(a)))
        elif cmd == "logs":
            n = int(argv[1]) if len(argv) > 1 else 40
            text = await a.call(f"/addons/{ADDON}/logs")
            print("\n".join(str(text).splitlines()[-n:]))
        elif cmd == "api":       # api /jobs/info — любой GET к Supervisor, для диагностики
            print(json.dumps(await a.call(argv[1]), ensure_ascii=False, indent=1)[:3000])
        else:
            sys.exit(__doc__)
    finally:
        await a.ws.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
