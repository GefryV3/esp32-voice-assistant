"""Выложить сервер как локальный аддон Home Assistant.

    python tools/deploy_addon.py [--dest \\\\192.168.178.86\\addons\\jarvis]

Собирает папку аддона: addon/config.yaml (с версией из jarvis.__version__), addon/Dockerfile,
пакет jarvis, pyproject.toml и токен из .env — и кладёт её в общую папку addons аддона
Samba share. Дальше в HA: Настройки → Дополнения → Магазин → ⋮ → Проверить обновления →
Jarvis → Установить (в следующий раз — Пересобрать).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from jarvis import __version__  # noqa: E402
from jarvis.config import read_env_file  # noqa: E402

DEFAULT_DEST = r"\\192.168.178.86\addons\jarvis"
# в аддоне адрес и папка данных свои — с ноутбука разработки берём только эти
ENV_KEYS = ("JARVIS_DEVICE_TOKEN", "JARVIS_TZ", "JARVIS_LOG_LEVEL")


def build(dest: Path) -> list[str]:
    env = read_env_file(SERVER / ".env")
    if not env.get("JARVIS_DEVICE_TOKEN"):
        sys.exit("в server/.env нет JARVIS_DEVICE_TOKEN — аддону нечем пускать колонку")

    dest.mkdir(parents=True, exist_ok=True)
    config = (SERVER / "addon" / "config.yaml").read_text(encoding="utf-8")
    config = re.sub(r'(?m)^version: .*$', f'version: "{__version__}"', config)
    (dest / "config.yaml").write_text(config, encoding="utf-8", newline="\n")
    shutil.copyfile(SERVER / "addon" / "Dockerfile", dest / "Dockerfile")
    shutil.copyfile(SERVER / "addon" / "CHANGELOG.md", dest / "CHANGELOG.md")  # HA показывает его при обновлении
    shutil.copyfile(SERVER / "pyproject.toml", dest / "pyproject.toml")

    pkg = dest / "jarvis"
    if pkg.exists():
        shutil.rmtree(pkg)          # чтобы удалённые модули не остались в аддоне
    shutil.copytree(SERVER / "jarvis", pkg, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    lines = [f"{k}={env[k]}" for k in ENV_KEYS if env.get(k)]
    (dest / "jarvis.env").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sorted(p.name for p in dest.iterdir())


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dest", default=DEFAULT_DEST, help="папка аддона в общей папке addons")
    dest = Path(p.parse_args().dest)
    try:
        files = build(dest)
    except OSError as e:
        sys.exit(f"не получилось записать в {dest}: {e}\n"
                 "открой папку в проводнике и войди логином аддона Samba share")
    print(f"аддон Jarvis {__version__} → {dest}: {', '.join(files)}")


if __name__ == "__main__":
    main()
