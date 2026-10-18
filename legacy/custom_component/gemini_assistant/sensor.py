"""Сенсор количества активных напоминаний — его читает дисплей ESP32-S3
(text_sensor в voice-satellite.yaml, entity_id: sensor.assistant_pending_reminders),
чтобы показать бейдж, даже если ассистент молчит.

Подключение: добавь `sensor: - platform: gemini_assistant` в configuration.yaml
(см. homeassistant/configuration_snippet.yaml).
"""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import DOMAIN

SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities: AddEntitiesCallback, discovery_info=None
) -> None:
    memory = hass.data[DOMAIN]["memory"]
    entity = PendingRemindersSensor(memory)
    async_add_entities([entity])

    async def _refresh(_now) -> None:
        entity.async_schedule_update_ha_state(force_refresh=True)

    async_track_time_interval(hass, _refresh, SCAN_INTERVAL)


class PendingRemindersSensor(SensorEntity):
    _attr_name = "Assistant Pending Reminders"
    _attr_unique_id = "gemini_assistant_pending_reminders"
    _attr_icon = "mdi:bell-alert"

    def __init__(self, memory) -> None:
        self._memory = memory
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {"due_texts": []}

    def update(self) -> None:
        # Значение сенсора (бейдж на дисплее) - ВСЕ невыполненные напоминания,
        # а атрибут due_texts - только те, чей срок уже наступил: его читает
        # автоматизация "проверка напоминаний" и озвучивает/шлёт в Telegram.
        self._attr_native_value = self._memory.pending_reminder_count()
        self._attr_extra_state_attributes = {
            "due_texts": [r["text"] for r in self._memory.get_due_reminders()]
        }
