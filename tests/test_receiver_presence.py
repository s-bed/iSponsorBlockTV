"""Synchronize playback when the TV receiver joins an existing Lounge session."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest
import pyytlounge

from custom_components.isponsorblocktv.core import ytlounge
from custom_components.isponsorblocktv.core.ytlounge import YtLoungeApi


def _client() -> YtLoungeApi:
    client = YtLoungeApi()
    client._sid = client._gsession = "connected"
    return client


async def _listen(client) -> None:
    client._subscription = asyncio.create_task(asyncio.sleep(3600))


@pytest.mark.parametrize("encoded_roster", [False, True])
@pytest.mark.parametrize("encoded_info", [False, True])
async def test_receiver_roster_preserves_metadata_and_queries_once(
    encoded_roster, encoded_info, caplog
):
    client = _client()
    await _listen(client)
    info = {"clientName": "TVHTML5", "deviceId": "private-device-id"}
    screen = {
        "type": "LOUNGE_SCREEN",
        "name": "private-screen-name",
        "deviceInfo": json.dumps(info) if encoded_info else info,
    }
    devices = json.dumps([screen]) if encoded_roster else [screen]
    caplog.set_level(logging.DEBUG, logger=client.logger.name)
    with patch.object(client, "_send_command", return_value=True) as command:
        try:
            await client._process_event("loungeStatus", [{"devices": devices}])
            await asyncio.gather(*client._actions)
            await client._process_event("loungeStatus", [{"devices": devices}])
            await client._process_event("loungeScreenConnected", [])
            await asyncio.gather(*client._actions)
            assert client.receiver_present is True
            assert client._screen_name == "private-screen-name"
            assert client._device_info == info
            command.assert_awaited_once_with("getNowPlaying", None)
            assert "receiver presence changed (present=True)" in caplog.text
            assert "private-device-id" not in caplog.text
            assert "private-screen-name" not in caplog.text
        finally:
            await client.stop_subscription()


@pytest.mark.parametrize("devices", [None, "invalid-json", {}, [None], ["screen"]])
async def test_malformed_roster_preserves_known_presence(devices):
    client = _client()
    client.receiver_present = True
    with patch.object(pyytlounge.YtLoungeApi, "_process_event", new_callable=AsyncMock) as base:
        await client._process_event("loungeStatus", [{"devices": devices}])
    assert client.receiver_present is True
    base.assert_not_awaited()


async def test_receiver_join_resends_a_query_sent_before_the_receiver_arrived():
    client = _client()
    await _listen(client)
    with patch.object(client, "_send_command", return_value=True) as command:
        try:
            client._queue_playback_query()
            await asyncio.gather(*client._actions)
            assert client._playback_request_at is not None
            await client._process_event("loungeScreenConnected", [])
            await asyncio.gather(*client._actions)
            assert command.await_count == 2
            await client._process_event("loungeStatus", [{"devices": []}])
            assert client.receiver_present is False
            assert client._playback_request_at is None
            await client._process_event("loungeScreenConnected", [])
            await asyncio.gather(*client._actions)
            assert command.await_count == 3
        finally:
            await client.stop_subscription()


@pytest.mark.parametrize("resume", [False, True])
async def test_bootstrap_join_and_subscription_start_share_one_query(resume):
    client = _client()
    client._request_initial_playback = True
    await client._process_event("loungeScreenConnected", [])
    assert not client._actions
    started = asyncio.Event()

    async def subscribe():
        started.set()
        await client._process_event("loungeScreenConnected", [])
        await asyncio.Future()

    with (
        patch.object(pyytlounge.YtLoungeApi, "subscribe", side_effect=subscribe),
        patch.object(client, "_send_command", return_value=True) as command,
    ):
        task = asyncio.create_task(client.run_subscription(AsyncMock(), resume=resume))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.gather(*client._actions)
            command.assert_awaited_once_with("getNowPlaying", None)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_playback_received_before_join_avoids_an_extra_query():
    client = _client()
    await _listen(client)
    await client._process_event("nowPlaying", [{"state": "1", "videoId": "content"}])
    with patch.object(client, "_send_command", return_value=True) as command:
        try:
            await client._process_event("loungeScreenConnected", [])
            await asyncio.gather(*client._actions)
            command.assert_not_awaited()
            client.reset_session()
            assert client.receiver_present is None
            assert client._receiver_has_playback is False
        finally:
            await client.stop_subscription()


@pytest.mark.parametrize(
    "event_type", ["nowPlaying", "onStateChange", "onAdStateChange", "adPlaying"]
)
async def test_actual_playback_event_confirms_receiver_after_an_empty_roster(event_type):
    client = _client()
    await client._process_event("loungeStatus", [{"devices": []}])
    assert client.receiver_present is False
    await client._process_event(event_type, [{}])
    await asyncio.gather(*client._actions)
    assert client.receiver_present is True
    assert client._playback_request_at is None


@pytest.mark.parametrize("client_name", ["LOCAL_UNSUPPORTED", "LIBRARY_UNSUPPORTED"])
async def test_receiver_roster_preserves_both_blacklists(client_name):
    client = _client()
    with (
        patch.object(ytlounge, "youtube_client_blacklist", ["LOCAL_UNSUPPORTED"]),
        patch.object(ytlounge, "BLACKLISTED_CLIENTS", ["LIBRARY_UNSUPPORTED"]),
    ):
        await client._process_event(
            "loungeStatus",
            [{"devices": [{"type": "LOUNGE_SCREEN", "deviceInfo": {"clientName": client_name}}]}],
        )
    assert not client.connected()
    assert client.receiver_present is False
