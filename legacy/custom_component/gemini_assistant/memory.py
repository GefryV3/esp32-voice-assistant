"""Персистентная память ассистента.

Хранит факты о пользователе и напоминания в одном JSON-файле на диске.
Специально сделано максимально просто (без БД) — 500ГБ диска и текстовые
заметки на годы вперёд не пересекутся по объёму, а плоский файл легко
бэкапить и читать руками при отладке.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)

DEFAULT_STRUCTURE: dict[str, Any] = {
    "profile": {"имя": "", "часовой_пояс": "", "заметки": []},
    "facts": [],
    "reminders": [],
    "conversation_summary": "",
}


class MemoryStore:
    """Простое файловое хранилище памяти ассистента."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = Lock()
        self._data: dict[str, Any] = DEFAULT_STRUCTURE.copy()
        self._load()

    # ---------- базовые операции с файлом ----------

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as err:
                _LOGGER.error("Не удалось прочитать файл памяти %s: %s", self._path, err)
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._save()

    def _save(self) -> None:
        tmp_path = self._path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp_path.replace(self._path)  # атомарная замена, чтобы не словить битый файл

    # ---------- профиль и время ----------

    def get_profile(self) -> dict[str, Any]:
        return self._data.get("profile", {})

    def get_timezone_name(self) -> str | None:
        tz = self.get_profile().get("часовой_пояс", "")
        return tz or None

    def get_now(self) -> datetime:
        """Текущее время с таймзоной: из profile.часовой_пояс, иначе локальная системная."""
        tz_name = self.get_timezone_name()
        if tz_name:
            try:
                return datetime.now(ZoneInfo(tz_name))
            except (KeyError, ValueError) as err:
                _LOGGER.warning("Неизвестный часовой пояс в профиле: %s (%s)", tz_name, err)
        return datetime.now().astimezone()

    # ---------- факты ----------

    def add_fact(self, text: str) -> None:
        with self._lock:
            self._data["facts"].append(
                {"text": text, "added": datetime.now().isoformat(timespec="seconds")}
            )
            self._save()

    def forget_fact(self, needle: str) -> int:
        """Удаляет факты, содержащие needle. Возвращает число удалённых записей."""
        with self._lock:
            before = len(self._data["facts"])
            self._data["facts"] = [
                f for f in self._data["facts"] if needle.lower() not in f["text"].lower()
            ]
            removed = before - len(self._data["facts"])
            if removed:
                self._save()
            return removed

    # ---------- напоминания ----------

    def add_reminder(self, text: str, due_iso: str) -> None:
        with self._lock:
            self._data["reminders"].append({"text": text, "due": due_iso, "done": False})
            self._save()

    def get_due_reminders(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or self.get_now()
        due = []
        for r in self._data["reminders"]:
            if r["done"]:
                continue
            try:
                due_dt = datetime.fromisoformat(r["due"])
            except ValueError:
                _LOGGER.warning("Некорректная дата в напоминании: %s", r)
                continue
            # Модель пишет due_iso без смещения - считаем это местным временем
            # пользователя (той же таймзоной, что и now).
            if due_dt.tzinfo is None and now.tzinfo is not None:
                due_dt = due_dt.replace(tzinfo=now.tzinfo)
            if due_dt <= now:
                due.append(r)
        return due

    def mark_reminder_done(self, text: str) -> None:
        with self._lock:
            for r in self._data["reminders"]:
                if r["text"] == text:
                    r["done"] = True
            self._save()

    def pending_reminder_count(self) -> int:
        return sum(1 for r in self._data["reminders"] if not r["done"])

    # ---------- сводка старых разговоров ----------

    def update_conversation_summary(self, summary: str) -> None:
        with self._lock:
            self._data["conversation_summary"] = summary
            self._save()

    def get_conversation_summary(self) -> str:
        return self._data.get("conversation_summary", "")

    # ---------- рендер контекста для промпта модели ----------

    def as_prompt_context(self) -> str:
        profile = self.get_profile()
        profile_bits = []
        if profile.get("имя"):
            profile_bits.append(f"- имя: {profile['имя']}")
        if profile.get("часовой_пояс"):
            profile_bits.append(f"- часовой пояс: {profile['часовой_пояс']}")
        for note in profile.get("заметки", []):
            profile_bits.append(f"- {note}")
        profile_text = "\n".join(profile_bits)
        facts = "\n".join(f"- {f['text']}" for f in self._data["facts"][-30:])
        reminders = "\n".join(
            f"- {r['text']} (к {r['due']})" for r in self._data["reminders"] if not r["done"]
        )
        return (
            f"Профиль пользователя:\n{profile_text or '(пока нет)'}\n\n"
            f"Факты о пользователе:\n{facts or '(пока нет)'}\n\n"
            f"Активные напоминания:\n{reminders or '(пока нет)'}\n\n"
            f"Сводка прошлых разговоров:\n{self._data['conversation_summary'] or '(пока нет)'}"
        )
