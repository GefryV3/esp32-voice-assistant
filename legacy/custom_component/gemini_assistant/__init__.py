"""Настройка интеграции gemini_assistant из configuration.yaml.

Пример секции в configuration.yaml (см. homeassistant/configuration_snippet.yaml):

gemini_assistant:
  api_key: !secret gemini_api_key
  memory_path: /config/gemini_assistant/memory.json
  google_credentials_path: /config/gemini_assistant/credentials.json
  google_token_path: /config/gemini_assistant/token.json
  keep_token_path: /config/gemini_assistant/keep_token.json   # опционально, см. get_keep_master_token.py

Как это устроено (важно для понимания, почему столько шагов):
в современных версиях HA (2024+) conversation-агент обязан быть
conversation-ENTITY, привязанной к config entry, иначе он не появится в
списке "Диалоговый агент" в настройках пайплайна. Старый способ
(conversation.async_set_agent(hass, None, agent)) больше не работает -
интеграция с ним падала при загрузке, и агента "тупо не было" в списке.
Поэтому: YAML-секция читается здесь, затем создаётся config entry через
import-flow (config_flow.py), а сам агент-entity добавляется в
conversation.py::async_setup_entry.
"""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .google_tools import GoogleServices
from .keep_tools import KeepNotes
from .memory import MemoryStore

_LOGGER = logging.getLogger(__name__)

DOMAIN = "gemini_assistant"
PLATFORMS = ["conversation"]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required("api_key"): cv.string,
                vol.Required("memory_path"): cv.string,
                vol.Optional("google_credentials_path"): cv.string,
                vol.Optional("google_token_path"): cv.string,
                vol.Optional("keep_token_path"): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    if DOMAIN not in config:
        # YAML-секцию убрали, а config entry остался - не падаем.
        return True

    conf = config[DOMAIN]

    memory = MemoryStore(conf["memory_path"])

    google = None
    if conf.get("google_credentials_path") and conf.get("google_token_path"):
        google = GoogleServices(
            conf["google_credentials_path"],
            conf["google_token_path"],
            timezone_name=memory.get_timezone_name() or hass.config.time_zone,
        )

    keep = None
    if conf.get("keep_token_path"):
        keep = KeepNotes(conf["keep_token_path"])

    # Пробрасываем memory наружу, чтобы автоматизации (напоминания, дайджесты)
    # и sensor.py могли к нему обращаться через hass.data.
    hass.data[DOMAIN] = {"conf": conf, "memory": memory, "google": google, "keep": keep}

    # --- Сервисы для автоматизаций (описания - services.yaml) ---

    async def _svc_mark_reminder_done(call) -> None:
        # Вызывается автоматизацией "проверка напоминаний" сразу после
        # announce/Telegram - иначе то же напоминание повторялось бы каждые 5 минут.
        await hass.async_add_executor_job(memory.mark_reminder_done, call.data["text"])

    hass.services.async_register(
        DOMAIN,
        "mark_reminder_done",
        _svc_mark_reminder_done,
        schema=vol.Schema({vol.Required("text"): cv.string}),
    )

    async def _svc_compact_history(call) -> None:
        agent = hass.data[DOMAIN].get("agent")
        if agent is None:
            _LOGGER.warning("compact_history: агент ещё не поднят, пропускаю")
            return
        await agent.async_compact_history()

    hass.services.async_register(DOMAIN, "compact_history", _svc_compact_history)

    # Создаём config entry (один раз), чтобы HA поднял conversation-entity.
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data={}
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if DOMAIN not in hass.data:
        _LOGGER.error(
            "Секция gemini_assistant отсутствует в configuration.yaml - "
            "добавь её (см. configuration_snippet.yaml) и перезапусти HA"
        )
        return False
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _LOGGER.info("Gemini Assistant conversation agent зарегистрирован")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
