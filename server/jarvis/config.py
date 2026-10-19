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
    vad_silence_ms: int = 450                # пауза, после которой Gemini считает фразу законченной
    agent_model: str = "gemini-flash-latest"   # модель фоновых агентов (с поиском Google)
    ha_url: str = ""                         # пусто — умного дома нет
    ha_token: str = ""


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
        vad_silence_ms = int(env.get("JARVIS_SILENCE_MS", "450"))
    except ValueError as e:
        raise ConfigError(f"JARVIS_SILENCE_MS должен быть числом (миллисекунды): {e}") from None

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
        vad_silence_ms=vad_silence_ms,
        agent_model=env.get("JARVIS_AGENT_MODEL", "gemini-flash-latest"),
        ha_url=env.get("JARVIS_HA_URL") or (SUPERVISOR_URL if env.get("SUPERVISOR_TOKEN") else ""),
        ha_token=env.get("JARVIS_HA_TOKEN") or env.get("SUPERVISOR_TOKEN", ""),
    )
