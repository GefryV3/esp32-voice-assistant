"""Настройки сервера: переменные окружения JARVIS_* и файл .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SUPERVISOR_URL = "ws://supervisor/core/websocket"   # так аддон HA ходит к ядру
VIENNA_TZ = "CET-1CEST,M3.5.0,M10.5.0/3"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    device_token: str
    host: str = "0.0.0.0"
    port: int = 8765
    tz: str = VIENNA_TZ
    log_level: str = "INFO"
    data_dir: str = "data"  # настройки колонки и прочее, что должно пережить перезапуск
    gemini_api_key: str = ""                 # пусто — голоса нет, остальное работает
    gemini_model: str = "gemini-3.8-live"
    voice_name: str = "Charon"               # встроенный голос Gemini (мужской, спокойный)
    wake_threshold: float = 0.5
    affective: bool = False                  # эмоциональный диалог Gemini (v1alpha)
    wake_words: str = "мага,jarvis"          # через запятую: «мага» (Vosk) и «jarvis» (openWakeWord)
    barge_in: bool = True                    # можно перебить колонку голосом
    barge_ratio: float = 4.0                 # во сколько раз голос должен быть громче эха ответа
    vad_silence_ms: int = 650                # пауза, после которой Gemini считает фразу законченной
    agent_model: str = "gemini-flash-lite-latest"   # модель фоновых агентов (пересказ найденного)
    agent_search: str = "web"                       # web (DuckDuckGo), brave, searxng или google (поиск Gemini)
    brave_key: str = ""                             # ключ Brave Search API для agent_search=brave
    searxng_url: str = ""                           # адрес SearXNG для agent_search=searxng
    ha_url: str = ""                         # пусто — умного дома нет
    ha_token: str = ""
    telegram_token: str = ""                 # пусто — Telegram-бота нет
    telegram_chat_ids: str = ""              # через запятую; чужим chat_id бот не отвечает
    telegram_model: str = "gemini-flash-lite-latest"  # текстовая модель (не Live) для ответов в Telegram


def read_env_file(path: Path) -> dict[str, str]:
    """Простой разбор KEY=VALUE: пустые строки и # комментарии пропускаются."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(env_file: Path | str = ".env", environ: dict[str, str] | None = None) -> Settings:
    """Значения из окружения важнее значений из .env."""
    env = read_env_file(Path(env_file))
    env.update(os.environ if environ is None else environ)

    token = env.get("JARVIS_DEVICE_TOKEN", "")
    if not token:
        raise ConfigError(
            "JARVIS_DEVICE_TOKEN не задан. Скопируй .env.example в .env и впиши токен "
            "(тот же, что DEVICE_TOKEN в прошивке)."
        )
    try:
        port = int(env.get("JARVIS_PORT", "8765"))
    except ValueError as e:
        raise ConfigError(f"JARVIS_PORT должен быть числом: {e}") from None
    try:
        wake_threshold = float(env.get("JARVIS_WAKE_THRESHOLD", "0.5"))
    except ValueError as e:
        raise ConfigError(f"JARVIS_WAKE_THRESHOLD должен быть числом 0…1: {e}") from None

    try:
        vad_silence_ms = int(env.get("JARVIS_SILENCE_MS", "650"))
    except ValueError as e:
        raise ConfigError(f"JARVIS_SILENCE_MS должен быть числом (миллисекунды): {e}") from None

    try:
        barge_ratio = float(env.get("JARVIS_BARGE_RATIO", "4.0"))
    except ValueError as e:
        raise ConfigError(f"JARVIS_BARGE_RATIO должен быть числом (1.2…20): {e}") from None

    return Settings(
        device_token=token,
        host=env.get("JARVIS_HOST", "0.0.0.0"),
        port=port,
        tz=env.get("JARVIS_TZ", VIENNA_TZ),
        log_level=env.get("JARVIS_LOG_LEVEL", "INFO").upper(),
        data_dir=env.get("JARVIS_DATA", "data"),
        gemini_api_key=env.get("GEMINI_API_KEY", ""),
        gemini_model=env.get("JARVIS_GEMINI_MODEL", "gemini-3.8-live"),
        voice_name=env.get("JARVIS_VOICE", "Charon"),
        wake_threshold=wake_threshold,
        wake_words=env.get("JARVIS_WAKE_WORDS", "мага,jarvis"),
        affective=env.get("JARVIS_AFFECTIVE", "0").strip() in ("1", "true", "on"),
        barge_in=env.get("JARVIS_BARGE_IN", "1").lower() not in ("0", "false", "no", "off"),
        barge_ratio=barge_ratio,
        vad_silence_ms=vad_silence_ms,
        telegram_token=env.get("TELEGRAM_TOKEN", ""),
        telegram_chat_ids=env.get("TELEGRAM_CHAT_IDS", ""),
        telegram_model=env.get("JARVIS_TELEGRAM_MODEL", "gemini-flash-lite-latest"),
        agent_model=env.get("JARVIS_AGENT_MODEL", "gemini-flash-lite-latest"),
        agent_search=env.get("JARVIS_AGENT_SEARCH", "web").lower(),
        brave_key=env.get("BRAVE_API_KEY", ""),
        searxng_url=env.get("JARVIS_SEARXNG_URL", ""),
        ha_url=env.get("JARVIS_HA_URL") or (SUPERVISOR_URL if env.get("SUPERVISOR_TOKEN") else ""),
        ha_token=env.get("JARVIS_HA_TOKEN") or env.get("SUPERVISOR_TOKEN", ""),
    )
