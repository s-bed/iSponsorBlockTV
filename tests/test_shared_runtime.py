"""Exercise the Lounge wrapper and segment scheduling in the installable ZIP."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pyytlounge
from custom_components.isponsorblocktv.core.playback import PlaybackScheduler
from custom_components.isponsorblocktv.core.ytlounge import YtLoungeApi
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pyytlounge.models import State

_LOGGER = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledged", [True, False])
async def test_sponsor_skip_counts_only_acknowledged_seeks(acknowledged):
    seeked = asyncio.Event()

    async def seek_to(position):
        assert position == 12
        seeked.set()
        return acknowledged

    client = SimpleNamespace(playback_speed=None, ad_playing=False, seek_to=seek_to)
    helper = SimpleNamespace(
        get_segments=AsyncMock(return_value=[{"start": 0, "end": 12, "UUID": ["segment"]}]),
        mark_viewed_segments=AsyncMock(),
    )
    count_skip = Mock()
    scheduler = PlaybackScheduler(helper, client, 0, _LOGGER, on_skip=count_skip)
    state = SimpleNamespace(videoId="video", currentTime=0, state=State.Playing)
    await scheduler(state)
    await asyncio.wait_for(seeked.wait(), timeout=1)
    await scheduler.cancel()
    if acknowledged:
        count_skip.assert_called_once()
        helper.mark_viewed_segments.assert_awaited_once_with(["segment"])
    else:
        count_skip.assert_not_called()
        helper.mark_viewed_segments.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_skip_cancelled_when_playback_pauses():
    client = SimpleNamespace(playback_speed=1.0, ad_playing=False, seek_to=AsyncMock())
    helper = SimpleNamespace(
        get_segments=AsyncMock(return_value=[{"start": 5, "end": 12, "UUID": ["segment"]}])
    )
    scheduler = PlaybackScheduler(helper, client, 0, _LOGGER)
    await scheduler(SimpleNamespace(videoId="video", currentTime=0, state=State.Playing))
    await asyncio.sleep(0)
    helper.get_segments.assert_awaited_once_with("video")
    await scheduler(SimpleNamespace(videoId="video", currentTime=0, state=State.Paused))
    await scheduler.cancel()
    client.seek_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_telemetry_reflects_lounge_events_and_disconnect_reset():
    notify = Mock()
    client = YtLoungeApi(standalone=False, on_update=notify)
    await client._process_event(
        "nowPlaying", [{"videoId": "test", "state": "1", "duration": "30", "currentTime": "2"}]
    )
    assert client.playback_state.state is State.Playing
    assert client.playback_state.videoId == "test"
    assert client.playback_state.duration == 30

    await client._process_event("adPlaying", [{"isSkipEnabled": "true"}])
    assert client.ad_playing and client.ad_skippable
    await client._process_event("onAdStateChange", [{"adState": "1082", "currentTime": "0"}])
    assert not client.ad_playing and not client.ad_skippable

    await client._process_event("onVolumeChanged", [{"volume": "63", "muted": "true"}])
    assert client.volume == 63
    assert client.muted is True
    assert notify.call_count == 4

    client.reset_playback()
    assert client.playback_state is client.ad_playing is client.volume is client.muted is None


@pytest.mark.asyncio
async def test_malformed_playback_numbers_and_ended_video():
    client = YtLoungeApi(standalone=False)
    await client._process_event(
        "nowPlaying",
        [{"videoId": "test", "state": "1", "duration": "NaN", "currentTime": None}],
    )
    assert client.playback_state.state is State.Playing
    assert client.playback_state.duration is client.playback_state.currentTime is None
    await client._process_event("onStateChange", [{"state": "0", "currentTime": "10"}])
    assert client.playback_state.videoId == ""
    assert client.playback_state.duration is None


@pytest.mark.asyncio
async def test_ad_mute_restored_on_disconnect_and_prefetch_still_runs(monkeypatch):
    config = SimpleNamespace(join_name="Test", mute_ads=True, skip_ads=False, auto_play=True)
    helper = SimpleNamespace(get_segments=AsyncMock())
    monkeypatch.setattr(pyytlounge.YtLoungeApi, "disconnect", AsyncMock(return_value=True))
    client = YtLoungeApi(config=config, api_helper=helper, standalone=False)
    client._sid = client._gsession = "connected"
    client.mute = AsyncMock(return_value=True)

    await client._process_event("adPlaying", [{"contentVideoId": "test"}])
    await asyncio.gather(*client._actions)
    client.mute.assert_awaited_once_with(True, override=True)
    helper.get_segments.assert_awaited_once_with("test")
    assert await client.disconnect()
    client.mute.assert_awaited_with(False, override=True)
    assert not client.connected()


@pytest.mark.asyncio
async def test_subscription_cancellation_releases_tasks_without_closing_session(hass, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def subscribe(_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(pyytlounge.YtLoungeApi, "subscribe", subscribe)
    session = async_get_clientsession(hass)
    client = YtLoungeApi(standalone=False, web_session=session)
    task = asyncio.create_task(client.run_subscription(AsyncMock()))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert client._subscription is client._watchdog_task is None
    assert not session.closed


@pytest.mark.asyncio
async def test_bind_denial_does_not_invoke_standalone_workaround():
    response = SimpleNamespace(status=401, text=AsyncMock(return_value="Connection denied"))
    request = AsyncMock()
    request.__aenter__.return_value = response
    client = YtLoungeApi(
        "screen-id",
        standalone=False,
        web_session=SimpleNamespace(post=Mock(return_value=request)),
    )
    client.connect_as_screen = AsyncMock()
    client.auth.lounge_id_token = "session-token"

    assert not await client.connect()
    assert client.auth.lounge_id_token is None
    client.connect_as_screen.assert_not_awaited()
