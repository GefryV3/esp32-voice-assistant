"""Обёртки над Google Calendar, Gmail и Tasks для function-calling из Gemini.

Требует отдельного OAuth-клиента в Google Cloud Console (тип "Desktop app"),
файл credentials.json и один раз пройденный интерактивный вход - после
этого token.json обновляется автоматически, руками входить больше не нужно.

Это НЕ то же самое, что коннекторы Gmail/Calendar в самом Claude/Cowork -
тут отдельный, независимый доступ, которым управляет сам ассистент на ноутбуке.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/tasks",
]


class GoogleServices:
    """Ленивая обёртка: настоящие google-api-python-client объекты
    создаются только при первом вызове, чтобы не тормозить старт HA."""

    def __init__(
        self, credentials_path: str, token_path: str, timezone_name: str | None = None
    ) -> None:
        self._credentials_path = Path(credentials_path)
        self._token_path = Path(token_path)
        # IANA-имя таймзоны пользователя (например "Europe/Vienna") - берётся
        # из profile.часовой_пояс memory.json, см. __init__.py.
        self._timezone_name = timezone_name
        self._calendar_service = None
        self._gmail_service = None
        self._tasks_service = None

    def _get_credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if self._token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self._token_path), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # Интерактивный вход нужен только один раз, локально на ноутбуке,
                # не изнутри Home Assistant - прогони отдельным скриптом.
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self._credentials_path), SCOPES
                )
                creds = flow.run_local_server(port=0)
            self._token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def _calendar(self):
        if self._calendar_service is None:
            from googleapiclient.discovery import build

            self._calendar_service = build("calendar", "v3", credentials=self._get_credentials())
        return self._calendar_service

    def _gmail(self):
        if self._gmail_service is None:
            from googleapiclient.discovery import build

            self._gmail_service = build("gmail", "v1", credentials=self._get_credentials())
        return self._gmail_service

    def _tasks(self):
        if self._tasks_service is None:
            from googleapiclient.discovery import build

            self._tasks_service = build("tasks", "v1", credentials=self._get_credentials())
        return self._tasks_service

    # ---------- инструменты, вызываемые моделью ----------

    def list_upcoming_events(self, max_results: int = 10) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat()
        result = (
            self._calendar()
            .events()
            .list(
                calendarId="primary",
                timeMin=now,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return [
            {
                "summary": e.get("summary", ""),
                "start": e["start"].get("dateTime", e["start"].get("date")),
            }
            for e in result.get("items", [])
        ]

    def create_event(self, summary: str, start_iso: str, duration_minutes: int = 60) -> str:
        start = datetime.fromisoformat(start_iso)
        end = start + timedelta(minutes=duration_minutes)
        start_body: dict[str, str] = {"dateTime": start.isoformat()}
        end_body: dict[str, str] = {"dateTime": end.isoformat()}
        # Модель отдаёт start_iso без смещения - Google в таком случае требует
        # явное поле timeZone, иначе событие может "уехать" на другой час.
        if start.tzinfo is None and self._timezone_name:
            start_body["timeZone"] = self._timezone_name
            end_body["timeZone"] = self._timezone_name
        event = {
            "summary": summary,
            "start": start_body,
            "end": end_body,
        }
        created = self._calendar().events().insert(calendarId="primary", body=event).execute()
        return created.get("htmlLink", "")

    def list_unread_subjects(self, max_results: int = 5) -> list[str]:
        result = (
            self._gmail()
            .users()
            .messages()
            .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=max_results)
            .execute()
        )
        subjects = []
        for msg_meta in result.get("messages", []):
            msg = (
                self._gmail()
                .users()
                .messages()
                .get(userId="me", id=msg_meta["id"], format="metadata", metadataHeaders=["Subject"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            subjects.append(headers.get("Subject", "(без темы)"))
        return subjects

    def add_note(self, text: str) -> str:
        """Записывает заметку в Google Tasks (список по умолчанию) - видна потом
        в приложении Google Tasks на телефоне. Публичного API у Google Keep для
        обычных аккаунтов нет, поэтому используем Tasks как ближайший рабочий вариант."""
        created = (
            self._tasks()
            .tasks()
            .insert(tasklist="@default", body={"title": text})
            .execute()
        )
        return created.get("id", "")
