"""Store the YouTube API key and channel blocklist shared by all TVs."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from typing import TypedDict

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from .const import CONF_SCREEN_ID, DOMAIN

_SETTINGS_KEY = f"{DOMAIN}.channel_settings"


class ChannelSettingsSaveError(Exception):
    """The shared settings could not be saved."""


@dataclass(frozen=True, slots=True)
class ChannelSettings:
    """Shared settings available to a TV or an options flow."""

    api_key: str = field(default="", repr=False)
    blocklist: tuple[str, ...] = ()
    key_conflict: bool = False


class _StoredSettings(TypedDict):
    api_key: str
    blocklist: list[str]
    key_conflict: bool


class _ChannelSettingsManager:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = Store[_StoredSettings](
            hass, 1, _SETTINGS_KEY, private=True, atomic_writes=True
        )
        self.lock = asyncio.Lock()
        self.settings: ChannelSettings | None = None

    def tv_entries(self) -> list[ConfigEntry]:
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN, include_ignore=False)
            if CONF_SCREEN_ID in entry.data
        ]

    def remove_legacy_keys(self) -> None:
        for entry in self.tv_entries():
            if "apikey" in entry.options:
                options = dict(entry.options)
                del options["apikey"]
                self.hass.config_entries.async_update_entry(entry, options=options)

    async def async_save(self, settings: ChannelSettings) -> None:
        data: _StoredSettings = {
            "api_key": settings.api_key,
            "blocklist": list(settings.blocklist),
            "key_conflict": settings.key_conflict,
        }
        await self.store.async_save(data)
        # Store can log a write error without raising it. Confirm before clearing legacy keys.
        if await self.store.async_load() != data:
            raise ChannelSettingsSaveError
        self.settings = settings
        if not settings.key_conflict:
            self.remove_legacy_keys()

    async def async_load(self) -> ChannelSettings:
        """Load or initialize shared settings while holding the domain lock."""
        if self.settings is not None:
            return self.settings
        if (stored := await self.store.async_load()) is not None:
            self.settings = ChannelSettings(
                api_key=stored["api_key"],
                blocklist=tuple(stored["blocklist"]),
                key_conflict=stored["key_conflict"],
            )
            if not self.settings.key_conflict:
                self.remove_legacy_keys()
            return self.settings

        legacy_keys = {
            key for entry in self.tv_entries() if (key := entry.options.get("apikey", "").strip())
        }
        settings = ChannelSettings(
            api_key=next(iter(legacy_keys)) if len(legacy_keys) == 1 else "",
            key_conflict=len(legacy_keys) > 1,
        )
        try:
            await self.async_save(settings)
        except ChannelSettingsSaveError:
            # Keep the legacy settings available and retry initialization on the next access.
            pass
        return settings

    def schedule_reloads(self, *, inheriting_only: bool = False) -> None:
        for entry in self.tv_entries():
            if entry.disabled_by or entry.state not in (
                ConfigEntryState.LOADED,
                ConfigEntryState.SETUP_IN_PROGRESS,
            ):
                continue
            if inheriting_only and entry.options.get("override_channel_blocklist", False):
                continue
            self.hass.config_entries.async_schedule_reload(entry.entry_id)


_MANAGER_KEY: HassKey[_ChannelSettingsManager] = HassKey(_SETTINGS_KEY)


def _manager(hass: HomeAssistant) -> _ChannelSettingsManager:
    if (manager := hass.data.get(_MANAGER_KEY)) is None:
        manager = hass.data[_MANAGER_KEY] = _ChannelSettingsManager(hass)
    return manager


async def async_get_channel_settings(hass: HomeAssistant) -> ChannelSettings:
    """Return the current shared settings, initializing them when needed."""
    manager = _manager(hass)
    async with manager.lock:
        return await manager.async_load()


async def async_set_api_key(hass: HomeAssistant, api_key: str) -> None:
    """Set or clear the shared key and reload TVs using channel lookups."""
    manager = _manager(hass)
    async with manager.lock:
        current = await manager.async_load()
        updated = replace(current, api_key=api_key.strip(), key_conflict=False)
        if updated != current or manager.settings is None:
            await manager.async_save(updated)
        if updated != current:
            manager.schedule_reloads()


async def async_update_global_blocklist(
    hass: HomeAssistant, *, add: Collection[str] = (), remove: Collection[str] = ()
) -> None:
    """Apply an edit to the latest global blocklist and reload inheriting TVs."""
    manager = _manager(hass)
    async with manager.lock:
        current = await manager.async_load()
        remaining = (channel for channel in current.blocklist if channel not in remove)
        blocklist = tuple(dict.fromkeys((*remaining, *add)))
        if blocklist != current.blocklist:
            await manager.async_save(replace(current, blocklist=blocklist))
            manager.schedule_reloads(inheriting_only=True)
