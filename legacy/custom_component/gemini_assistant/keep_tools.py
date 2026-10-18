"""Неофициальная интеграция с Google Keep через gkeepapi.

ВАЖНО - прочитай перед использованием (см. также TUTORIAL.md, раздел про Keep):
- У Google Keep нет официального API для обычных (не Workspace) аккаунтов -
  только domain-wide delegation для корпоративных доменов. Для личного
  gmail.com-аккаунта это единственный практический вариант получить заметки
  именно в настоящем Keep, а не в Google Tasks.
- gkeepapi (https://github.com/kiwiz/gkeepapi) - НЕофициальная библиотека,
  имитирующая внутренний протокол приложения Keep. Она может сломаться без
  предупреждения при любом изменении на стороне Google - это риск, на который
  ты сознательно идёшь ради заметок именно в Keep.
- Нужен включённый двухфакторный вход (2-Step Verification) и отдельный
  App Password для этой библиотеки (обычный пароль аккаунта Google для входа
  не примет).
- Вход выполняется один раз вручную (см. get_keep_master_token.py рядом),
  дальше используется закешированный master_token, чтобы Home Assistant не
  логинился заново при каждом обращении (частые логины Google может
  расценить как подозрительную активность).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)


class KeepNotes:
    """Ленивая обёртка над gkeepapi.Keep - подключается только при первом вызове."""

    def __init__(self, token_path: str) -> None:
        self._token_path = Path(token_path)
        self._keep = None

    def _get_keep(self):
        if self._keep is not None:
            return self._keep

        import gkeepapi

        if not self._token_path.exists():
            raise RuntimeError(
                f"Файл {self._token_path} не найден - сначала прогони "
                "get_keep_master_token.py, см. TUTORIAL.md."
            )

        data = json.loads(self._token_path.read_text(encoding="utf-8"))
        keep = gkeepapi.Keep()
        keep.authenticate(data["email"], data["master_token"])
        self._keep = keep
        return self._keep

    # ---------- инструмент, вызываемый моделью ----------

    def add_note(self, text: str, title: str = "") -> str:
        keep = self._get_keep()
        note = keep.createNote(title, text)
        keep.sync()
        return note.id
