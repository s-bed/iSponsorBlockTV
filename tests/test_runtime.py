"""Exercise the Home Assistant connection and pairing state transitions."""

import asyncio
import logging
from datetime import UTC
from unittest.mock import AsyncMock, patch

import pyytlounge
import pytest
from aiohttp import ClientError
from custom_components.isponsorblocktv.core import ytlounge
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


def _playback_runtime(hass, **options):
    tv = runtime.TVRuntime(
        hass, _entry(hass), None, {**DEFAULT_OPTIONS, "skip_categories": [], **options}
    )
    tv._client._state_callback = tv._scheduler
    tv._client._sid = tv._client._gsession = "connected"
    return tv


@pytest.mark.parametrize(
    ("options", "duration", "dismiss"),
    [
        ({}, "29.9", True),
        ({}, "30", False),
        ({"short_video_duration_threshold": 45}, "30", True),
        ({"disturb_short_videos": False}, "20", False),
        ({}, None, False),
        ({}, "0", False),
    ],
)
async def test_short_video_dismissal_uses_this_tvs_options(hass, options, duration, dismiss):
    tv = _playback_runtime(hass, **options)
    client = tv._client
    sample = {"videoId": "video", "state": "1", "currentTime": "0"}
    if duration is not None:
        sample["duration"] = duration

    with patch.object(pyytlounge.YtLoungeApi, "_command", return_value=True) as command:
        try:
            await client._process_event("nowPlaying", [sample])
            await asyncio.gather(*tv._scheduler._tasks)
            first_disruption = tv.snapshot.last_short_disruption
            await client._process_event("onStateChange", [{"state": "1", "currentTime": "1"}])
            await asyncio.gather(*tv._scheduler._tasks)
            assert tv.snapshot.last_short_disruption == first_disruption
            if dismiss:
                command.assert_awaited_once_with("dpadCommand", {"key": "BACK"})
                assert first_disruption is not None
                assert first_disruption.tzinfo is UTC
                await client._process_event(
                    "nowPlaying", [{**sample, "videoId": "next", "state": "3"}]
                )
                await asyncio.gather(*tv._scheduler._tasks)
                assert command.await_count == 1
                await client._process_event("onStateChange", [{"state": "1", "currentTime": "0"}])
                await asyncio.gather(*tv._scheduler._tasks)
                assert command.await_count == 2
            else:
                command.assert_not_awaited()
                assert first_disruption is None
        finally:
            await tv._scheduler.cancel()

    assert tv.snapshot.skip_count == 0


async def test_ad_duration_does_not_trigger_short_video_dismissal(hass):
    tv = _playback_runtime(hass)
    client = tv._client
    with patch.object(pyytlounge.YtLoungeApi, "_command", return_value=True) as command:
        try:
            await client._process_event(
                "nowPlaying",
                [{"videoId": "video", "state": "1081", "currentTime": "0", "duration": "18"}],
            )
            await asyncio.gather(*tv._scheduler._tasks)
            await client._process_event("onStateChange", [{"state": "1", "currentTime": "1"}])
            await asyncio.gather(*tv._scheduler._tasks)
            assert client.playback_state.duration is None
        finally:
            await tv._scheduler.cancel()

    command.assert_not_awaited()
    assert tv.snapshot.last_short_disruption is None


async def test_short_video_dismissal_waiting_for_command_lock_ignores_stale_video(hass):
    tv = _playback_runtime(hass)
    client = tv._client
    with patch.object(pyytlounge.YtLoungeApi, "_command", return_value=True) as command:
        try:
            async with client._command_mutex:
                await client._process_event(
                    "nowPlaying",
                    [{"videoId": "short", "state": "1", "currentTime": "0", "duration": "18"}],
                )
                await asyncio.sleep(0)
                queued = tv._scheduler._pending
                assert queued is not None
                await client._process_event(
                    "nowPlaying",
                    [{"videoId": "long", "state": "1", "currentTime": "0", "duration": "65"}],
                )
            with pytest.raises(asyncio.CancelledError):
                await queued
            await asyncio.gather(*tv._scheduler._tasks)
        finally:
            await tv._scheduler.cancel()

    command.assert_not_awaited()
    assert tv.snapshot.last_short_disruption is None


async def test_integration_debug_logging_traces_ad_skip_and_shorts(
    hass, enable_custom_integrations, caplog
):
    entry = _entry(hass)
    tv = runtime.TVRuntime(
        hass, entry, None, {**DEFAULT_OPTIONS, "skip_ads": True, "disturb_short_videos": False}
    )
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


@pytest.mark.parametrize(
    ("has_bind_playback", "expired", "acknowledged"),
    [(False, False, True), (True, False, True), (False, True, True), (False, False, False)],
)
async def test_screen_disconnect_sends_one_back_on_timely_reconnect(
    hass, has_bind_playback, expired, acknowledged
):
    tv = runtime.TVRuntime(hass, _entry(hass), None, {**DEFAULT_OPTIONS, "skip_categories": []})
    client = tv._client
    ordinary_reconnect = asyncio.Event()
    connection_attempt = 0
    back_attempts = []

    async def connect():
        nonlocal connection_attempt
        connection_attempt += 1
        if connection_attempt == 2:
            return False
        client._sid = client._gsession = "connected"
        if connection_attempt == 3:
            # YouTube can include the old subtitle track and playback in the bind response.
            await client._process_event("onSubtitlesTrackChanged", [{"videoId": "short"}])
            if has_bind_playback:
                await client._process_event(
                    "nowPlaying",
                    [{"videoId": "short", "state": "1", "currentTime": "1", "duration": "18"}],
                )
        return True

    async def subscribe():
        if connection_attempt == 1:
            await client._process_event(
                "loungeScreenDisconnected", [{"reason": "disconnectedByUserScreenInitiated"}]
            )
        elif connection_attempt == 3:
            await asyncio.gather(*tv._scheduler._tasks)
            await client._process_event("onSubtitlesTrackChanged", [{"videoId": "short"}])
            await asyncio.gather(*client._actions)
            client._connection_lost()
        else:
            ordinary_reconnect.set()
            await asyncio.Future()

    async def send_command(_command, _parameters):
        back_attempts.append(connection_attempt)
        return acknowledged

    with (
        patch.object(runtime, "RETRY_INTERVAL", 0),
        patch.object(ytlounge, "monotonic", side_effect=[100, 161 if expired else 110]),
        patch.object(
            runtime,
            "async_verify_screen_id",
            return_value=PairedScreen(screen_id=SCREEN_ID, name=None, lounge_token="test-token"),
        ),
        patch.object(client, "is_available", return_value=True),
        patch.object(client, "connect", side_effect=connect),
        patch.object(pyytlounge.YtLoungeApi, "subscribe", side_effect=subscribe),
        patch.object(pyytlounge.YtLoungeApi, "disconnect", return_value=True),
        patch.object(pyytlounge.YtLoungeApi, "_command", side_effect=send_command) as command,
    ):
        tv.async_start()
        try:
            await asyncio.wait_for(ordinary_reconnect.wait(), 5)
        finally:
            await tv.async_stop()

    if expired:
        command.assert_not_awaited()
    else:
        command.assert_awaited_once_with("dpadCommand", {"key": "BACK"})
        assert back_attempts == [3]
    assert (tv.snapshot.last_short_disruption is not None) == (not expired and acknowledged)


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


@pytest.mark.parametrize(
    ("stream_ends", "invalid_session"),
    [(1, False), (1, True), (3, False)],
    ids=["stream-ended", "session-invalid", "retries-exhausted"],
)
async def test_event_stream_recovery_reuses_session_before_rebinding(
    hass, stream_ends, invalid_session
):
    tv = runtime.TVRuntime(hass, _entry(hass), None, DEFAULT_OPTIONS)
    client = tv._client
    expected_binds = 2 if invalid_session or stream_ends > 1 else 1
    listening = asyncio.Event()
    playback_work_started = asyncio.Event()
    playback_work_cancelled = asyncio.Event()
    refreshed = asyncio.Event()
    binds = 0
    subscriptions = []

    async def connect():
        nonlocal binds
        binds += 1
        client._sid = f"session-{binds}"
        client._gsession = f"gsession-{binds}"
        client._last_event_id = 0
        return True

    async def subscribe():
        params = client._common_connection_parameters()
        subscriptions.append((params["SID"], params["gsessionid"], params["AID"]))
        if len(subscriptions) == 1:
            await client._process_events(
                [[42, ["nowPlaying", {"videoId": "video", "state": "1", "currentTime": "2"}]]]
            )
            await playback_work_started.wait()
            if invalid_session:
                assert not client._handle_session_result(400, "Unknown SID")
        if len(subscriptions) <= stream_ends:
            return
        if expected_binds == 1:
            # An update without a video ID must not reuse playback from the old stream.
            await client._process_event("onStateChange", [{"state": "1", "currentTime": "20"}])
            await asyncio.sleep(0)
        listening.set()
        await asyncio.Future()

    async def get_segments(_video_id):
        playback_work_started.set()
        try:
            await asyncio.Future()
        finally:
            playback_work_cancelled.set()

    async def refresh_playback():
        refreshed.set()
        return True

    with (
        patch.object(runtime, "RETRY_INTERVAL", 0),
        patch.object(runtime, "STREAM_RETRY_DELAY", 0),
        patch.object(runtime, "STREAM_RETRY_LIMIT", 2),
        patch.object(
            runtime,
            "async_verify_screen_id",
            return_value=PairedScreen(screen_id=SCREEN_ID, name=None, lounge_token="test-token"),
        ) as verify,
        patch.object(client, "is_available", return_value=True),
        patch.object(client, "connect", side_effect=connect),
        patch.object(client, "get_now_playing", side_effect=refresh_playback),
        patch.object(
            client.api_helper, "get_segments", new_callable=AsyncMock, side_effect=get_segments
        ) as segments,
        patch.object(pyytlounge.YtLoungeApi, "subscribe", side_effect=subscribe),
        patch.object(pyytlounge.YtLoungeApi, "disconnect", return_value=True) as terminate,
    ):
        tv.async_start()
        try:
            await asyncio.wait_for(listening.wait(), 5)
            assert subscriptions[0] == ("session-1", "gsession-1", 0)
            if invalid_session:
                assert subscriptions[1:] == [("session-2", "gsession-2", 0)]
            elif stream_ends == 1:
                assert subscriptions[1:] == [("session-1", "gsession-1", 42)]
                await asyncio.wait_for(refreshed.wait(), 5)
            else:
                assert subscriptions[1:] == [
                    ("session-1", "gsession-1", 42),
                    ("session-1", "gsession-1", 42),
                    ("session-2", "gsession-2", 0),
                ]
            assert binds == verify.await_count == expected_binds
            assert tv.snapshot.connected
            assert playback_work_cancelled.is_set()
            segments.assert_awaited_once_with("video")
            terminate.assert_not_awaited()
        finally:
            await tv.async_stop()

    terminate.assert_awaited_once()


async def test_expired_session_and_disconnect_reverify_and_unload_cancels_subscription(hass):
    entry = _entry(hass)
    tv = runtime.TVRuntime(hass, entry, None, DEFAULT_OPTIONS)
    reconnecting = asyncio.Event()
    subscription_cancelled = asyncio.Event()
    subscription_calls = 0

    async def subscribe(_scheduler, *, resume=False):
        nonlocal subscription_calls
        assert not resume
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
