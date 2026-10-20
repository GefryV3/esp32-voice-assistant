"""Выложить проект на GitHub с вычищенной историей.

    python tools/publish_github.py              — собрать чистую копию и запушить в main
    python tools/publish_github.py --dry-run    — только собрать и проверить, без пуша
    python tools/publish_github.py --force      — перезаписать main (если новое правило задело
                                                  уже выложенные коммиты и хэши поменялись)

Локальная история остаётся как есть, на GitHub уходит её копия, пропущенная через
git-filter-repo с правилами из .git/scrub-expressions.txt (формат --replace-text; файл
лежит только в .git, потому что в нём сами личные данные). Чистка детерминированная:
те же коммиты дают те же хэши, поэтому каждый новый запуск — обычный fast-forward поверх
уже выложенного main. Авторы коммитов переписываются по .git/publish-mailmap (формат
git mailmap): на GitHub коммиты привязаны к аккаунту, а не к локальной почте.

Перед пушем проверяется, что в чистой истории не осталось ни строк из левой части правил,
ни значений из server/.env и firmware/main/secrets.h.

Нужен пакет git-filter-repo (pip install git-filter-repo).
Прямой `git push` из рабочей папки запрещён хуком pre-push: там старая история.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXPR = ROOT / ".git" / "scrub-expressions.txt"
MAILMAP = ROOT / ".git" / "publish-mailmap"
REMOTE = "https://github.com/GefryV3/esp32-voice-assistant.git"
# не секреты: адреса в локальной сети, порт, пояс, уровень логов, выбор поиска — они и в коде есть
PUBLIC_KEYS = {"JARVIS_HOST", "JARVIS_PORT", "JARVIS_TZ", "JARVIS_LOG_LEVEL", "JARVIS_SERVER", "JARVIS_HA_URL",
               "JARVIS_AGENT_SEARCH", "SERVER_URI"}


def git(*args: str, cwd: Path, capture: bool = False) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, encoding="utf-8",
                         capture_output=capture)
    return res.stdout if capture else ""


def forbidden_strings() -> list[str]:
    """Что не должно попасть на GitHub: левые части правил и значения секретов."""
    out = []
    for line in EXPR.read_text(encoding="utf-8").splitlines():
        src = line.partition("==>")[0]
        if src and not src.startswith("regex:"):
            out.append(src)
    env = ROOT / "server" / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.partition("=")
            val = val.strip().strip("\"'")
            key = key.strip()
            if sep and not key.startswith("#") and key not in PUBLIC_KEYS and len(val) >= 6:
                out.append(val)
    secrets_h = ROOT / "firmware" / "main" / "secrets.h"
    if secrets_h.is_file():
        for key, val in re.findall(r'#define\s+(\w+)\s+"([^"]{6,})"', secrets_h.read_text(encoding="utf-8")):
            if key not in PUBLIC_KEYS:
                out.append(val)
    return out


def check_clean(repo: Path) -> None:
    history = git("log", "--all", "-p", "--format=%H%n%an %ae%n%B", "--no-color", cwd=repo, capture=True)
    leaks = [s for s in forbidden_strings() if s in history]
    regexes = [line.partition("==>")[0][6:] for line in EXPR.read_text(encoding="utf-8").splitlines()
               if line.startswith("regex:")]
    leaks += [rx for rx in regexes if re.search(rx, history)]
    if leaks:
        # сами значения не печатаем — это могут быть ключи
        sys.exit(f"СТОП: в чистой истории осталось {len(leaks)} запрещённых строк, пуша не будет")


def _rm_readonly(func, path, _exc) -> None:
    # git кладёт объекты только для чтения, на Windows rmtree без этого спотыкается
    Path(path).chmod(0o700)
    func(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--branch", default="stage1/skeleton", help="какую локальную ветку выкладывать")
    ap.add_argument("--dry-run", action="store_true", help="собрать и проверить, но не пушить")
    ap.add_argument("--force", action="store_true", help="перезаписать main на GitHub")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not EXPR.is_file():
        sys.exit(f"нет {EXPR} — без правил чистки выкладывать нельзя")

    tmp = Path(tempfile.mkdtemp(prefix="jarvis-publish-"))
    try:
        pub = tmp / "pub"
        git("clone", "--quiet", "--no-local", "--single-branch", "--branch", args.branch, str(ROOT), str(pub), cwd=tmp)
        mailmap = ["--mailmap", str(MAILMAP)] if MAILMAP.is_file() else []
        subprocess.run([sys.executable, "-m", "git_filter_repo", "--quiet", "--replace-text", str(EXPR),
                        "--replace-message", str(EXPR), *mailmap], cwd=pub, check=True)
        git("branch", "-m", args.branch, "main", cwd=pub)
        check_clean(pub)
        head = git("rev-parse", "--short", "HEAD", cwd=pub, capture=True).strip()
        count = git("rev-list", "--count", "HEAD", cwd=pub, capture=True).strip()
        print(f"чистая история: {count} коммитов, main = {head}")
        if args.dry_run:
            print("--dry-run: не пушу")
            return
        git("remote", "add", "origin", REMOTE, cwd=pub)
        git("push", *(["--force"] if args.force else []), "origin", "main", cwd=pub)
        print(f"выложено: {REMOTE.removesuffix('.git')}")
    finally:
        shutil.rmtree(tmp, onexc=_rm_readonly)


if __name__ == "__main__":
    main()
