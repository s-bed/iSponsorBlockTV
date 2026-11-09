"""Connection, pairing, ad, and mute binary sensors."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import ISponsorBlockTVEntity
from .runtime import ISponsorBlockTVConfigEntry

BINARY_SENSORS = (
    BinarySensorEntityDescription(
        key="youtube_connected",
        translation_key="youtube_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="pairing_problem",
        translation_key="pairing_problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(key="ad_playing", translation_key="ad_playing"),
    BinarySensorEntityDescription(key="ad_skippable", translation_key="ad_skippable"),
    BinarySensorEntityDescription(
        key="muted", translation_key="muted", entity_registry_enabled_default=False
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ISponsorBlockTVConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add binary sensors for the paired device."""
    async_add_entities(
        ISponsorBlockTVBinarySensor(entry, description) for description in BINARY_SENSORS
    )


class ISponsorBlockTVBinarySensor(ISponsorBlockTVEntity, BinarySensorEntity):
    """Expose a flag from the current device snapshot."""

    def __init__(
        self, entry: ISponsorBlockTVConfigEntry, description: BinarySensorEntityDescription
    ) -> None:
        super().__init__(
            entry,
            description,
            requires_connection=description.key not in {"youtube_connected", "pairing_problem"},
        )

    @property
    def is_on(self) -> bool | None:
        """Return the most recently reported value."""
        key = self.entity_description.key
        return getattr(self.runtime.snapshot, "connected" if key == "youtube_connected" else key)
