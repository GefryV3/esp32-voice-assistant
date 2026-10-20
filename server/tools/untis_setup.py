"""Ввести логин и пароль WebUntis, сразу проверить их и сохранить в server/.env только если подошли.

    python tools/untis_setup.py

Логин и пароль вводишь ты в этом окне (пароль не показывается). Скрипт проверяет их так же, как сервер
(JSON-RPC WebUntis), и пишет UNTIS_USER и UNTIS_PASSWORD в server/.env. Неверную пару не сохраняет.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from jarvis.config import read_env_file  # noqa: E402
from jarvis.untis import Untis, UntisConfig, UntisError  # noqa: E402

ENV = SERVER / ".env"


def save(user: str, password: str) -> None:
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.exists() else []
    lines = [ln for ln in lines if not ln.startswith(("UNTIS_USER=", "UNTIS_PASSWORD="))]
    lines += [f"UNTIS_USER={user}", f"UNTIS_PASSWORD={password}"]
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def check(cfg: UntisConfig) -> str | None:
    """None — вход удался, иначе текст ошибки."""
    try:
        await Untis(cfg).lessons(date.today() + timedelta(days=1))
        return None
    except UntisError as e:
        return str(e)
    except Exception as e:
        return f"не вышло связаться: {e!r}"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    env = read_env_file(ENV)
    cfg = UntisConfig.from_env({**env, "UNTIS_ICAL_URL": ""})
    if not (cfg.server and cfg.school):
        sys.exit("в server/.env нет UNTIS_SERVER и UNTIS_SCHOOL")
    print(f"Школа: {cfg.school} на {cfg.server}. Логин и пароль те же, что в WebUntis.")
    for attempt in range(1, 4):
        user = input("Логин WebUntis: ").strip()
        password = getpass.getpass("Пароль (не показывается): ")
        err = asyncio.run(check(replace(cfg, user=user, password=password)))
        if err is None:
            save(user, password)
            print("Подошло. UNTIS_USER и UNTIS_PASSWORD записаны в server/.env.")
            return
        print(f"Не подошло ({err}). Попытка {attempt} из 3.")
        if attempt == 3:
            print("Проверь вход вручную в приватном окне браузера на этой же странице WebUntis. "
                  "Если там пускает, а здесь нет, напиши мне.")


if __name__ == "__main__":
    main()
