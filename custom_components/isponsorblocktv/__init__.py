"""Run iSponsorBlockTV inside Home Assistant."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .const import DEFAULT_OPTIONS
from .runtime import ISponsorBlockTVConfigEntry, TVRuntime

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ISponsorBlockTVConfigEntry) -> bool:
    """Set up entities and start listening without blocking on a TV."""
    options = {**DEFAULT_OPTIONS, **entry.options}
    session = (
        async_create_clientsession(hass, trust_env=True)
        if options["use_proxy"]
        else async_get_clientsession(hass)
    )
    runtime = entry.runtime_data = TVRuntime(hass, entry, session, options)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ISponsorBlockTVConfigEntry) -> bool:
    """Unload entities and stop YouTube activity for this TV."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_stop()
    return True
