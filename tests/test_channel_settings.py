"""Test shared settings persistence, migration, and reloads."""

import asyncio
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.isponsorblocktv import channel_settings
from custom_components.isponsorblocktv.channel_settings import (
    ChannelSettingsSaveError,
    async_get_channel_settings,
    async_set_api_key,
    async_update_global_blocklist,
)
from custom_components.isponsorblocktv.const import CONF_SCREEN_ID, DOMAIN

pytestmark = pytest.mark.asyncio
FIRST_CHANNEL = "UC" + "a" * 22
SECOND_CHANNEL = "UC" + "b" * 22


def _add_tv(hass, screen_id, *, loaded=False, disabled=False, **options):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SCREEN_ID: screen_id},
        options=options,
        state=ConfigEntryState.LOADED if loaded else ConfigEntryState.NOT_LOADED,
        disabled_by=ConfigEntryDisabler.USER if disabled else None,
    )
    entry.add_to_hass(hass)
    return entry


async def test_promotes_one_legacy_key_and_keeps_exclusions(hass):
    tv = _add_tv(hass, "tv", apikey="old-key", channel_whitelist=[FIRST_CHANNEL])
    disabled = _add_tv(hass, "disabled", disabled=True, apikey=" old-key ")

    first, second = await asyncio.gather(
        async_get_channel_settings(hass), async_get_channel_settings(hass)
    )

    assert first is second
    assert first.api_key == "old-key"
    assert not first.key_conflict
    assert tv.options == {"channel_whitelist": [FIRST_CHANNEL]}
    assert disabled.options == {}
    hass.data.pop(channel_settings._MANAGER_KEY)
    assert await async_get_channel_settings(hass) == first


async def test_conflict_preserves_legacy_keys_until_explicit_shared_save(hass):
    tv = _add_tv(hass, "tv", loaded=True, apikey="first", channel_whitelist=[FIRST_CHANNEL])
    override = _add_tv(hass, "override", loaded=True, override_channel_blocklist=True)
    disabled = _add_tv(hass, "disabled", disabled=True, apikey="second")

    shared = await async_get_channel_settings(hass)
    assert shared.key_conflict
    assert not shared.api_key
    assert tv.options["apikey"] == "first"
    assert disabled.options["apikey"] == "second"

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await async_set_api_key(hass, "shared")
    assert {call.args[0] for call in reload.call_args_list} == {tv.entry_id, override.entry_id}
    assert tv.options == {"channel_whitelist": [FIRST_CHANNEL]}
    assert disabled.options == {}
    assert (await async_get_channel_settings(hass)).api_key == "shared"

    with patch.object(hass.config_entries, "async_schedule_reload"):
        await async_set_api_key(hass, "")
    # Cleanup must be safe to repeat if a restart interrupted a legacy option write.
    hass.config_entries.async_update_entry(disabled, options={"apikey": "second"})
    hass.data.pop(channel_settings._MANAGER_KEY)
    shared = await async_get_channel_settings(hass)
    assert not shared.api_key
    assert not shared.key_conflict
    assert disabled.options == {}


async def test_global_edits_serialize_and_reload_inheriting_tvs(hass):
    tv = _add_tv(hass, "tv", loaded=True)
    _add_tv(hass, "override", loaded=True, override_channel_blocklist=True)
    _add_tv(hass, "offline")

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await asyncio.gather(
            async_update_global_blocklist(hass, add=(FIRST_CHANNEL,)),
            async_update_global_blocklist(hass, add=(SECOND_CHANNEL,)),
        )
        await async_update_global_blocklist(hass, add=(FIRST_CHANNEL, SECOND_CHANNEL))
    assert [call.args[0] for call in reload.call_args_list] == [tv.entry_id, tv.entry_id]
    assert (await async_get_channel_settings(hass)).blocklist == (FIRST_CHANNEL, SECOND_CHANNEL)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await async_update_global_blocklist(hass, remove=[FIRST_CHANNEL])
    reload.assert_called_once_with(tv.entry_id)
    assert (await async_get_channel_settings(hass)).blocklist == (SECOND_CHANNEL,)


async def test_failed_global_write_keeps_previous_settings_and_skips_reload(hass):
    _add_tv(hass, "tv", loaded=True)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await async_set_api_key(hass, "old-key")

    manager = hass.data[channel_settings._MANAGER_KEY]
    with (
        patch.object(manager.store, "async_save"),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
        pytest.raises(ChannelSettingsSaveError),
    ):
        await async_set_api_key(hass, "new-key")

    reload.assert_not_called()
    assert (await async_get_channel_settings(hass)).api_key == "old-key"
