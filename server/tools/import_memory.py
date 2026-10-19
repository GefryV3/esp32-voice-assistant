"""Перенос памяти старого ассистента (memory.json) в brain.db нового сервера.

    python tools/import_memory.py путь/к/memory.json [--db data/brain.db]

Факты и профиль («имя: …», «часовой пояс: …», заметки) становятся фактами, невыполненные
напоминания в будущем — напоминаниями. Дубли и просроченное пропускаются; повторный запуск
ничего не задваивает. Останови сервер на время переноса или запускай, пока он не пишет в базу.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
from jarvis.brain import Brain  # noqa: E402


def profile_facts(profile: dict) -> list[str]:
    out = []
    for key, value in profile.items():
        if key == "заметки":
            out += [str(n).strip() for n in (value or []) if str(n).strip()]
        elif str(value).strip():
            out.append(f"{key.replace('_', ' ')}: {str(value).strip()}")
    return out


def import_memory(brain: Brain, data: dict) -> dict[str, int]:
    """Добавить память из словаря старого формата; вернуть счётчики."""
    stat = dict.fromkeys(("facts", "dup_facts", "reminders", "dup_reminders", "expired", "done"), 0)
    texts = profile_facts(data.get("profile") or {})
    texts += [str(f.get("text", "") if isinstance(f, dict) else f).strip() for f in data.get("facts") or []]
    for text in filter(None, texts):
        stat["facts" if brain.remember(text) else "dup_facts"] += 1
    now = brain.now().replace(tzinfo=None)
    for r in data.get("reminders") or []:
        text = str(r.get("text", "")).strip()
        if not text:
            continue
        if r.get("done"):
            stat["done"] += 1
            continue
        try:
            due = datetime.fromisoformat(str(r.get("due"))).replace(tzinfo=None)
        except ValueError:
            stat["expired"] += 1
            continue
        if due <= now:
            stat["expired"] += 1
        elif any(t.casefold() == text.casefold() and d == due.replace(second=0, microsecond=0)
                 for _, t, d in brain.pending()):
            stat["dup_reminders"] += 1
        else:
            brain.add_reminder(text, due)
            stat["reminders"] += 1
    return stat


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("memory", type=Path, help="старый memory.json")
    ap.add_argument("--db", type=Path, default=SERVER / "data" / "brain.db")
    args = ap.parse_args()
    if not args.memory.is_file():
        sys.exit(f"нет файла {args.memory}")
    try:
        data = json.loads(args.memory.read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"{args.memory}: не JSON ({e})")
    s = import_memory(Brain(args.db), data)
    print(f"фактов добавлено {s['facts']} (дублей {s['dup_facts']}); "
          f"напоминаний добавлено {s['reminders']} (дублей {s['dup_reminders']}, "
          f"просрочено {s['expired']}, уже выполнено {s['done']}). База: {args.db}")


if __name__ == "__main__":
    main()
