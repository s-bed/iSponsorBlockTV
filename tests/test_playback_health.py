"""Check that Lounge acknowledgments cannot hide a silent playback stream."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pyytlounge

from custom_components.isponsorblocktv.core import ytlounge
from custom_components.isponsorblocktv.core.ytlounge import YtLoungeApi


def _client():
    client = YtLoungeApi()
    client._sid = client._gsession = "connected"
    return client


@pytest.fixture
async def clock(monkeypatch):
    loop = asyncio.get_running_loop()
    original_time = loop.time
    clock = SimpleNamespace(elapsed=0)
    monkeypatch.setattr(loop, "time", lambda: original_time() + clock.elapsed)
    return clock


async def test_acknowledged_query_times_out_despite_heartbeat_traffic(monkeypatch, clock):
    client = _client()
    subscribed = asyncio.Event()

    async def subscribe(_client):
        subscribed.set()
        await asyncio.Future()

    monkeypatch.setattr(pyytlounge.YtLoungeApi, "subscribe", subscribe)
    monkeypatch.setattr(ytlounge, "_WATCHDOG_POLL_INTERVAL", 0)
    with patch.object(client, "_send_command", return_value=True):
        assert await client.get_now_playing()
    assert client._playback_request_at is not None

    task = asyncio.create_task(client.run_subscription(AsyncMock()))
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=1)
        clock.elapsed += ytlounge._PLAYBACK_REPLY_TIMEOUT + 1
        await client._process_event("noop", [])
        with pytest.raises(ytlounge.PlaybackResponseTimeout):
            await asyncio.wait_for(task, timeout=1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert client._subscription is client._watchdog_task is None


@pytest.mark.parametrize("receiver_absent", [False, True])
async def test_another_missed_query_keeps_subscription_available_for_late_events(
    monkeypatch, clock, receiver_absent
):
    client = _client()
    client.receiver_present = False if receiver_absent else True
    client._binding_retry_used = not receiver_absent
    subscribed = asyncio.Event()

    async def subscribe(_client):
        subscribed.set()
        await asyncio.Future()

    monkeypatch.setattr(pyytlounge.YtLoungeApi, "subscribe", subscribe)
    monkeypatch.setattr(ytlounge, "_WATCHDOG_POLL_INTERVAL", 0)
    callback = AsyncMock()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(client, "_send_command", send)
    await client.get_now_playing()
    task = asyncio.create_task(client.run_subscription(callback))
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=1)
        clock.elapsed += ytlounge._PLAYBACK_REPLY_TIMEOUT + 1
        await client._process_event("noop", [])
        async with asyncio.timeout(1):
            while client._playback_request_at is not None:
                await asyncio.sleep(0)
        assert not task.done()
        assert client.connected()

        if not receiver_absent:
            clock.elapsed += 61
            await client._process_event("noop", [])
            async with asyncio.timeout(1):
                while send.await_count < 2:
                    await asyncio.sleep(0)
            assert send.await_args.args == ("getNowPlaying", None)
            assert not task.done()

        # A late receiver report still reaches the regular playback callback.
        await client._process_event(
            "nowPlaying",
            [{"videoId": "content", "state": "1", "currentTime": "20", "duration": "100"}],
        )
        callback.assert_awaited_once()
        assert callback.await_args.args[0].videoId == "content"
        assert not client._binding_retry_used
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("event", "data"),
    [
        ("nowPlaying", {}),
        ("nowPlaying", {"videoId": "content", "state": "1", "duration": "100"}),
        ("onStateChange", {"state": "1", "currentTime": "20"}),
        ("onAdStateChange", {"adState": "1", "currentTime": "2"}),
        ("adPlaying", {"contentVideoId": "content", "isSkipEnabled": "false"}),
    ],
)
async def test_receiver_playback_or_ad_reply_completes_pending_query(event, data):
    client = _client()
    with patch.object(client, "_send_command", return_value=True):
        await client.get_now_playing()
    assert client._playback_request_at is not None

    await client._process_event(event, [data])

    assert client._playback_request_at is None


@pytest.mark.parametrize(
    ("event", "args"),
    [
        ("noop", []),
        ("onVolumeChanged", [{"volume": "42"}]),
        ("nowPlaying", []),
        ("nowPlaying", ["malformed"]),
        ("adPlaying", [None]),
    ],
)
async def test_heartbeat_or_malformed_event_does_not_complete_query(event, args):
    client = _client()
    with patch.object(client, "_send_command", return_value=True):
        await client.get_now_playing()
    pending = client._playback_request_at

    await client._process_event(event, args)

    assert client._playback_request_at == pending


async def test_playback_reply_arriving_before_http_ack_is_not_lost():
    client = _client()

    async def send(_command, _parameters):
        assert client._playback_request_at is not None
        await client._process_event("nowPlaying", [{}])
        return True

    with patch.object(client, "_send_command", side_effect=send):
        assert await client.get_now_playing()

    assert client._playback_request_at is None


async def test_repeated_queries_preserve_first_unanswered_deadline(clock):
    client = _client()
    with patch.object(client, "_send_command", return_value=True):
        await client.get_now_playing()
        first = client._playback_request_at
        clock.elapsed += 5
        await client.get_now_playing()
        await client._process_event("noop", [])
        await client.stop_subscription()
        assert client._playback_request_at == first
        clock.elapsed += 5
        await client.get_now_playing()

    assert client._playback_request_at == first
    client.reset_session()
    assert client._playback_request_at is None


@pytest.mark.parametrize("failure", [False, OSError("Network unavailable")])
async def test_failed_query_does_not_leave_a_new_reply_deadline(failure):
    client = _client()
    send = (
        AsyncMock(side_effect=failure)
        if isinstance(failure, Exception)
        else AsyncMock(return_value=failure)
    )
    with patch.object(client, "_send_command", send):
        if isinstance(failure, Exception):
            with pytest.raises(OSError):
                await client.get_now_playing()
        else:
            assert not await client.get_now_playing()

    assert client._playback_request_at is None


async def test_rejected_followup_does_not_erase_earlier_unanswered_query():
    client = _client()
    with patch.object(client, "_send_command", side_effect=[True, False]):
        await client.get_now_playing()
        first = client._playback_request_at
        assert not await client.get_now_playing()

    assert client._playback_request_at == first
