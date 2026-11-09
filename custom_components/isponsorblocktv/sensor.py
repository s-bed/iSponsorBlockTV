"""Playback and activity sensors."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ISponsorBlockTVEntity
from .runtime import ISponsorBlockTVConfigEntry

SENSORS = (
    SensorEntityDescription(
        key="playback_state",
        translation_key="playback_state",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "playing", "paused", "buffering"],
    ),
    SensorEntityDescription(key="skip_count", translation_key="skip_count"),
    SensorEntityDescription(
        key="last_skip",
        translation_key="last_skip",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="last_short_disruption",
        translation_key="last_short_disruption",
        device_class=SensorDeviceClass.TIMESTAMP,
    ),
    SensorEntityDescription(
        key="video_id", translation_key="video_id", entity_registry_enabled_default=False
    ),
    SensorEntityDescription(
        key="duration",
        translation_key="duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="playback_speed",
        translation_key="playback_speed",
        entity_registry_enabled_default=False,
    ),
    SensorEntityDescription(
        key="volume",
        translation_key="volume",
        native_unit_of_measurement=PERCENTAGE,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ISponsorBlockTVConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add sensors for the paired device."""
    async_add_entities(ISponsorBlockTVSensor(entry, description) for description in SENSORS)


class ISponsorBlockTVSensor(ISponsorBlockTVEntity, SensorEntity):
    """Expose a value from the current device snapshot."""

    def __init__(
        self, entry: ISponsorBlockTVConfigEntry, description: SensorEntityDescription
    ) -> None:
        requires_connection = description.key not in {
            "skip_count",
            "last_skip",
            "last_short_disruption",
        }
        super().__init__(entry, description, requires_connection=requires_connection)

    @property
    def native_value(self) -> str | int | float | datetime | None:
        """Return the most recently reported value."""
        return getattr(self.runtime.snapshot, self.entity_description.key)
