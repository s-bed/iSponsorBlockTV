"""Exercise the Home Assistant connection and pairing state transitions."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pyytlounge
from aiohttp import ClientError
from custom_components.isponsorblocktv.core.pairing import (
    PairedScreen,
    PairingRejected,
    PairingUnavailable,
)
from homeassistant.components.logger.helpers import (
    DATA_LOGGER,
    LoggerSetting,
    LogPersistance,
    LogSettingsType,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.helpers import issue_registry
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.isponsorblocktv import runtime
from custom_components.isponsorblocktv.const import DEFAULT_OPTIONS, DOMAIN

SCREEN_ID = "a" * 64


def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"screen_id": SCREEN_ID}, title="Living room TV")
    entry.add_to_hass(hass)
    return entry


async def test_integration_debug_logging_traces_ad_skip_and_shorts(
    hass, enable_custom_integrations, caplog
):
    entry = _entry(hass)
    tv = runtime.TVRuntime(hass, entry, None, {**DEFAULT_OPTIONS, "skip_ads": True})
    client = tv._client
    client.auth.lounge_id_token = "private-lounge-token"
    caplog.set_level(logging.INFO)
    caplog.handler.setLevel(logging.DEBUG)

    await client._process_event("nowPlaying", [{"state": "1"}])
    assert "YouTube nowPlaying" not in caplog.text

    logger_class = logging.getLoggerClass()
    try:
        assert await async_setup_component(hass, "logger", {})
    finally:
        logging.setLoggerClass(logger_class)
    settings = hass.data[DATA_LOGGER].settings
    await settings.async_update(
        hass, DOMAIN, LoggerSetting("DEBUG", LogPersistance.NONE, LogSettingsType.INTEGRATION)
    )

    with patch.object(
        pyytlounge.YtLoungeApi,
        "_command",
        side_effect=[True, ClientError("https://example.com/?token=private-lounge-token")],
    ) as command:
        await client._process_event("adPlaying", [{"isSkipEnabled": "true"}])
        await asyncio.gather(*client._actions)
        await client._process_event("onAdStateChange", [{"adState": "1082", "currentTime": "0"}])
        await client._process_event("onStateChange", [{"state": "2", "currentTime": "8"}])
        await client._process_event(
            "loungeScreenDisconnected", [{"reason": "disconnectedByUserScreenInitiated"}]
        )
        await client._process_event("onSubtitlesTrackChanged", [{"videoId": "private-video-id"}])
        await asyncio.gather(*client._actions)

    assert [call.args[0] for call in command.await_args_list] == ["skipAd", "setPlaylist"]
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == f"{runtime.__name__}.{entry.title}"
    ]
    log = "\n".join(messages)
    assert "skipAd acknowledged=True" in log
    assert "ad state code=1082" in log
    assert "playback Playing -> Paused (state code=2, position=8.0 s" in log
    assert "reason='disconnectedByUserScreenInitiated', Shorts workaround pending=True" in log
    assert "setPlaylist ended (ClientError)" in log
    assert "private-lounge-token" not in caplog.text
    assert "private-video-id" not in caplog.text
    assert SCREEN_ID not in caplog.text

    await settings.async_update(
        hass, DOMAIN, LoggerSetting("NOTSET", LogPersistance.NONE, LogSettingsType.INTEGRATION)
    )
    caplog.clear()
    await client._process_event("onStateChange", [{"state": "1"}])
    assert "YouTube onStateChange" not in caplog.text


async def test_confirmed_rejection_creates_native_repair(hass, enable_custom_integrations):
    entry = _entry(hass)
    with (
        patch.object(runtime, "RETRY_INTERVAL", 0),
        patch.object(runtime, "async_verify_screen_id", side_effect=PairingRejected) as verify,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        tv = entry.runtime_data
        await asyncio.wait_for(asyncio.shield(tv._task), 5)
        await hass.async_block_till_done()

    assert verify.await_count == 2
    assert tv.snapshot.pairing_problem is True
    assert not tv.snapshot.connected
    assert list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
    repair_id = f"config_entry_reauth_{DOMAIN}_{entry.entry_id}"
    assert issue_registry.async_get(hass).async_get_issue("homeassistant", repair_id)


async def test_temporary_failure_and_offline_tv_do_not_start_repair(hass):
    entry = _entry(hass)
    tv = runtime.TVRuntime(hass, entry, None, DEFAULT_OPTIONS)
    availability_checked = asyncio.Event()
    keep_offline = asyncio.Event()

    async def offline():
        availability_checked.set()
        await keep_offline.wait()
        return False

    with (
        patch.object(runtime, "RETRY_INTERVAL", 0),
        patch.object(
            runtime,
            "async_verify_screen_id",
            side_effect=[
                PairingRejected(),
                PairingUnavailable(),
                PairingRejected(),
                PairedScreen(screen_id=SCREEN_ID, name=None, lounge_token="test-token"),
            ],
        ) as verify,
        patch.object(tv._client, "is_available", side_effect=offline),
    ):
        tv.async_start()
        await asyncio.wait_for(availability_checked.wait(), 5)
        await tv.async_stop()

    assert verify.await_count == 4
    assert tv.snapshot.pairing_problem is False
    assert not tv.snapshot.connected
    assert not list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_expired_session_and_disconnect_reverify_and_unload_cancels_subscription(hass):
    entry = _entry(hass)
    tv = runtime.TVRuntime(hass, entry, None, DEFAULT_OPTIONS)
    reconnecting = asyncio.Event()
    subscription_cancelled = asyncio.Event()
    subscription_calls = 0

    async def subscribe(_scheduler):
        nonlocal subscription_calls
        subscription_calls += 1
        if subscription_calls == 1:
            return
        reconnecting.set()
        try:
            await asyncio.Future()
        finally:
            subscription_cancelled.set()

    with (
        patch.object(runtime, "RETRY_INTERVAL", 0),
        patch.object(
            runtime,
            "async_verify_screen_id",
            return_value=PairedScreen(screen_id=SCREEN_ID, name=None, lounge_token="test-token"),
        ) as verify,
        patch.object(tv._client, "is_available", return_value=True),
        patch.object(tv._client, "connect", side_effect=[False, True, True]),
        patch.object(tv._client, "run_subscription", side_effect=subscribe),
        patch.object(tv._client, "stop_subscription", new_callable=AsyncMock) as stop,
    ):
        tv.async_start()
        await asyncio.wait_for(reconnecting.wait(), 5)
        assert tv.snapshot.connected
        await tv.async_stop()

    assert subscription_cancelled.is_set()
    assert stop.await_count == 3
    assert verify.await_count == 3
    assert tv.snapshot.pairing_problem is False
    assert not tv.snapshot.connected
    assert tv.snapshot.playback_state is None
