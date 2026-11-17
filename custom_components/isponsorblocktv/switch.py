"""Controls for the paired YouTube TV."""

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_DISTURB_SHORT_VIDEOS
from .entity import ISponsorBlockTVEntity
from .runtime import ISponsorBlockTVConfigEntry

SHORTS_SWITCH = SwitchEntityDescription(
    key=CONF_DISTURB_SHORT_VIDEOS,
    translation_key=CONF_DISTURB_SHORT_VIDEOS,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ISponsorBlockTVConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a Shorts switch to the TV's device page."""
    async_add_entities([ISponsorBlockTVShortsSwitch(entry)])


class ISponsorBlockTVShortsSwitch(ISponsorBlockTVEntity, SwitchEntity):
    """Control whether this TV interrupts short videos."""

    def __init__(self, entry: ISponsorBlockTVConfigEntry) -> None:
        super().__init__(entry, SHORTS_SWITCH, requires_connection=False)

    @property
    def is_on(self) -> bool:
        """Return the current Shorts interruption preference."""
        return self.runtime.disturb_short_videos

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable Shorts interruptions for this TV."""
        self.runtime.async_set_disturb_short_videos(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable Shorts interruptions for this TV."""
        self.runtime.async_set_disturb_short_videos(False)
        self.async_write_ha_state()
