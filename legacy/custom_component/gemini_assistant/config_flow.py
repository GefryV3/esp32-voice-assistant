"""Технический config flow.

Нужен только чтобы создать config entry из YAML-секции (import) - без entry
HA не поднимет conversation-entity и агента не будет в списке диалоговых
агентов пайплайна. Никаких настроек в UI тут нет и не надо: всё задаётся
в configuration.yaml.
"""
from __future__ import annotations

from homeassistant import config_entries

from . import DOMAIN


class GeminiAssistantConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_import(self, import_data=None) -> config_entries.ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Gemini Assistant", data={})

    async def async_step_user(self, user_input=None) -> config_entries.ConfigFlowResult:
        # Добавление через UI не поддерживаем - конфиг в YAML.
        return self.async_abort(reason="yaml_only")
