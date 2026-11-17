"""Run iSponsorBlockTV inside Home Assistant."""

from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .channel_settings import async_get_channel_settings
from .const import DEFAULT_OPTIONS, DOMAIN, GLOBAL_SETTINGS_ID
from .runtime import ISponsorBlockTVConfigEntry, TVRuntime, activity_store

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ISponsorBlockTVConfigEntry) -> bool:
    """Set up entities and start listening without blocking on a TV."""
    if entry.unique_id == GLOBAL_SETTINGS_ID:
        return True

    settings = await async_get_channel_settings(hass)
    options = {**DEFAULT_OPTIONS, **entry.options}
    blocklist = (
        options["channel_blocklist"]
        if options["override_channel_blocklist"]
        else settings.blocklist
    )
    channel_api_key = options.get("apikey", "") if settings.key_conflict else settings.api_key
    blocked_channels = (
        frozenset(blocklist) if settings.api_key and not settings.key_conflict else frozenset()
    )
    session = (
        async_create_clientsession(hass, trust_env=True)
        if options["use_proxy"]
        else async_get_clientsession(hass)
    )
    runtime = entry.runtime_data = TVRuntime(
        hass,
        entry,
        session,
        options,
        blocked_channels=blocked_channels,
        channel_api_key=channel_api_key,
    )
    await runtime.async_restore_activity()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    runtime.async_start()
    if hass.config_entries.async_entry_for_domain_unique_id(DOMAIN, GLOBAL_SETTINGS_ID) is None:
        # Create the settings entry outside the TV setup so it can load independently.
        hass.async_create_task(
            hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}),
            "Set up global channel settings",
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ISponsorBlockTVConfigEntry) -> bool:
    """Unload entities and stop YouTube activity for this TV."""
    if entry.unique_id == GLOBAL_SETTINGS_ID:
        return True
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_stop()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ISponsorBlockTVConfigEntry) -> None:
    """Remove saved playback activity when the TV entry is deleted."""
    if entry.unique_id != GLOBAL_SETTINGS_ID:
        await activity_store(hass, entry.entry_id).async_remove()
