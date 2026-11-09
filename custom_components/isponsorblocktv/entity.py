"""Shared behavior for iSponsorBlockTV entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN
from .runtime import ISponsorBlockTVConfigEntry


class ISponsorBlockTVEntity(Entity):
    """A push entity backed by a single paired YouTube app."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        entry: ISponsorBlockTVConfigEntry,
        description: EntityDescription,
        *,
        requires_connection: bool,
    ) -> None:
        self.runtime = entry.runtime_data
        self.entity_description = description
        self._requires_connection = requires_connection
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            model="YouTube on TV",
        )

    @property
    def available(self) -> bool:
        """Live telemetry requires an active connection to the app."""
        return not self._requires_connection or self.runtime.snapshot.connected

    async def async_added_to_hass(self) -> None:
        """Refresh entity state when the runtime snapshot changes."""
        self.async_on_remove(self.runtime.async_subscribe(self.async_write_ha_state))
