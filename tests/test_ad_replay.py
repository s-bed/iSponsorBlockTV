"""Check native ad replay and Lounge command batching."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from custom_components.isponsorblocktv.core import ad_replay
from custom_components.isponsorblocktv.core.ytlounge import YtLoungeApi


def _client(*, on_ad_skip=None, **options):
    config = SimpleNamespace(
        join_name="Test", mute_ads=False, skip_ads=True, auto_play=True, offset=0, **options
    )
    client = YtLoungeApi(config=config, on_ad_skip=on_ad_skip)
    client._sid = client._gsession = "connected"
    return client


async def _content(client, **changes):
    await client._process_event(
        "nowPlaying",
        [
            {
                "videoId": "content",
                "state": "1",
                "duration": "1364.521",
                "currentTime": "350",
                **changes,
            }
        ],
    )


@pytest.mark.asyncio
async def test_non_skippable_ad_reloads_content_then_seeks_when_ready():
    counted = Mock()
    client = _client(on_ad_skip=counted)
    with (
        patch.object(ad_replay, "monotonic", return_value=100) as now,
        patch.object(ad_replay, "_AD_WAIT", 0),
        patch.object(client, "_send_command", return_value=True) as command,
        patch.object(client, "_send_commands", return_value=True) as commands,
    ):
        await _content(client)
        now.return_value = 110
        await client._process_event(
            "onStateChange", [{"state": "1", "duration": "14", "currentTime": "0"}]
        )
        now.return_value = 112
        await client._process_event(
            "adPlaying", [{"isSkippable": "false", "isSkipEnabled": "false", "currentTime": "2"}]
        )
        await asyncio.gather(*client._actions)
        assert command.await_args_list == [
            call("pause", None),
            call("setPlaylist", {"videoId": "content", "currentTime": 360}),
        ]
        counted.assert_called_once_with()
        await client._process_event("nowPlaying", [{}])
        await client._process_event(
            "nowPlaying",
            [{"videoId": "content", "state": "3", "currentTime": "0", "duration": "0"}],
        )
        await _content(client, currentTime="0")
        await asyncio.gather(*client._actions)
        commands.assert_awaited_once_with([("seekTo", {"newTime": 360})])
        counted.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_video", "ad_ended", "session_stopped"])
async def test_countdown_is_cancelled_when_its_ad_is_no_longer_current(change):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    paused = asyncio.Event()

    async def send(command, parameters):
        if command == "pause":
            paused.set()
        return True

    with (
        patch.object(client, "_send_command", side_effect=send) as command,
        patch.object(client._ad_replay, "_send_toast", new_callable=AsyncMock),
    ):
        await _content(client)
        await client._process_event(
            "adPlaying", [{"isSkippable": "false", "isSkipEnabled": "false", "currentTime": "0"}]
        )
        task = client._ad_replay._task
        await asyncio.wait_for(paused.wait(), timeout=1)
        if change == "new_video":
            await _content(client, videoId="other")
        elif change == "ad_ended":
            await client._process_event(
                "onAdStateChange", [{"adState": "1082", "currentTime": "0"}]
            )
        else:
            await client.stop_subscription()
        await asyncio.gather(task, *client._actions, return_exceptions=True)

    assert command.await_args_list == [call("pause", None)]
    counted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "true"])
async def test_only_explicit_non_skippable_ad_starts_replay(flag):
    client = _client()
    await _content(client)
    data = {"isSkipEnabled": "false", "currentTime": "0"}
    if flag is not None:
        data["isSkippable"] = flag
    await client._process_event("adPlaying", [data])
    await asyncio.gather(*client._actions)
    assert client._ad_replay._task is None


@pytest.mark.asyncio
async def test_home_screen_clears_old_content_before_an_ad_without_content_id():
    client = _client()
    await _content(client)
    await client._process_event("nowPlaying", [{"videoId": "", "state": "-1"}])
    await client._process_event(
        "adPlaying", [{"isSkippable": "false", "isSkipEnabled": "false", "currentTime": "0"}]
    )
    await asyncio.gather(*client._actions)
    assert client._ad_replay._task is None


@pytest.mark.asyncio
async def test_explicit_ad_skipped_state_clears_inferred_ad_without_losing_content_position():
    client = _client()
    with patch.object(ad_replay, "monotonic", return_value=100) as now:
        await _content(client)
        now.return_value = 110
        await client._process_event(
            "onStateChange", [{"state": "1", "duration": "14", "currentTime": "0"}]
        )
        await client._process_event(
            "onStateChange", [{"state": "1082", "currentTime": "0", "duration": "14"}]
        )
        assert not client.ad_playing
        now.return_value = 120
        await client._process_event("adPlaying", [{"isSkipEnabled": "false"}])
        assert client._ad_replay._ad_position == 360
    await asyncio.gather(*client._actions)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, OSError("Request failed")])
async def test_failed_skip_releases_pause_after_ad_becomes_skippable(failure):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    paused = asyncio.Event()

    async def send(command, parameters):
        if command == "pause":
            paused.set()
        if command == "skipAd" and isinstance(failure, Exception):
            raise failure
        return command != "skipAd"

    with (
        patch.object(client, "_send_command", side_effect=send) as command,
        patch.object(client._ad_replay, "_send_toast", new_callable=AsyncMock),
    ):
        await _content(client)
        await client._process_event(
            "adPlaying", [{"isSkippable": "false", "isSkipEnabled": "false", "currentTime": "0"}]
        )
        task = client._ad_replay._task
        await asyncio.wait_for(paused.wait(), timeout=1)
        await client._process_event(
            "onAdStateChange", [{"adState": "1", "isSkipEnabled": "true", "currentTime": "0"}]
        )
        await asyncio.gather(task, *client._actions, return_exceptions=True)
    assert [item.args[0] for item in command.await_args_list] == ["pause", "skipAd", "play"]
    counted.assert_not_called()


@pytest.mark.asyncio
async def test_countdown_toast_terminates_its_remote_without_changing_main_session():
    events = json.dumps([[0, ["c", "toast-sid"]], [1, ["S", "toast-gsession"]]])
    response = SimpleNamespace(
        status=200,
        text=AsyncMock(return_value=f"{len(events) + 1}\n{events}\n"),
        raise_for_status=Mock(),
    )
    request = AsyncMock()
    request.__aenter__.return_value = response
    client = _client()
    client.use_web_binding = True
    client.auth.lounge_id_token = "test-token"
    client.session = SimpleNamespace(post=Mock(return_value=request))
    await client._ad_replay._send_toast("Ad will be skipped in 5 seconds")

    connection, cleanup = client.session.post.call_args_list
    assert connection.kwargs["data"]["name"] == "Ad will be skipped in 5 seconds"
    assert connection.kwargs["data"]["capabilities"] == "que,atp,vsp,ndt"
    assert cleanup.kwargs["data"]["TYPE"] == "terminate"
    assert cleanup.kwargs["params"]["app"] == connection.kwargs["data"]["app"]
    assert cleanup.kwargs["params"]["SID"] == "toast-sid"
    assert cleanup.kwargs["params"]["gsessionid"] == "toast-gsession"
    assert client._sid == client._gsession == "connected"
    assert client.auth.lounge_id_token == "test-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, OSError("Reload failed")])
async def test_failed_reload_resumes_the_same_paused_ad(failure):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    with (
        patch.object(ad_replay, "_AD_WAIT", 0),
        patch.object(client, "_send_command", side_effect=[True, failure, True]) as command,
    ):
        await _content(client)
        await client._process_event(
            "adPlaying", [{"isSkippable": "false", "isSkipEnabled": "false", "currentTime": "0"}]
        )
        await asyncio.gather(*client._actions)
    assert [item.args[0] for item in command.await_args_list] == ["pause", "setPlaylist", "play"]
    assert client._ad_replay._pending_seek is None
    counted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_mute", [True, False])
async def test_skippable_ad_restores_only_integration_owned_mute_in_one_batch(owned_mute):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    client._muted_for_ad = owned_mute
    client.volume_state = {"volume": 42, "muted": "true"}
    with (
        patch.object(client, "_send_command", return_value=True) as command,
        patch.object(client, "_send_commands", return_value=True) as commands,
    ):
        await client._process_event("adPlaying", [{"isSkipEnabled": "true"}])
        await asyncio.gather(*client._actions)

    if owned_mute:
        commands.assert_awaited_once_with(
            [
                ("skipAd", None),
                ("setVolume", {"volume": 42, "muted": "false"}),
            ]
        )
        command.assert_not_awaited()
    else:
        command.assert_awaited_once_with("skipAd", None)
        commands.assert_not_awaited()
    assert not client._muted_for_ad
    counted.assert_called_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_position", ["0", "350"])
async def test_replay_restores_owned_mute_with_only_the_seek_needed(reported_position):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    client.volume_state = {"volume": 42, "muted": "true"}
    with (
        patch.object(ad_replay, "monotonic", return_value=100),
        patch.object(ad_replay, "_AD_WAIT", 0),
        patch.object(client, "_send_command", return_value=True),
        patch.object(client, "_send_commands", return_value=True) as commands,
    ):
        await _content(client)
        await client._process_event(
            "adPlaying", [{"isSkippable": "false", "isSkipEnabled": "false", "currentTime": "0"}]
        )
        await asyncio.gather(*client._actions)
        client._muted_for_ad = True
        await _content(client, currentTime=reported_position)
        await asyncio.gather(*client._actions)
    expected = [] if reported_position == "350" else [("seekTo", {"newTime": 350})]
    expected.append(("setVolume", {"volume": 42, "muted": "false"}))
    commands.assert_awaited_once_with(expected)
    assert not client._muted_for_ad
    counted.assert_called_once_with()


@pytest.mark.asyncio
async def test_repeated_ad_events_count_once_and_distinct_back_to_back_ads_count_separately():
    counted = Mock()
    client = _client(on_ad_skip=counted)
    with patch.object(client, "_send_command", return_value=True):
        first = {"adVideoId": "ad-one", "isSkipEnabled": "true"}
        for _ in range(2):
            await client._process_event("adPlaying", [first])
            await asyncio.gather(*client._actions)
        await client._process_event(
            "onAdStateChange", [{"adState": "1", "isSkipEnabled": "true", "currentTime": "1"}]
        )
        await asyncio.gather(*client._actions)
        counted.assert_called_once_with()

        await client._process_event("adPlaying", [{"adVideoId": "ad-two", "isSkipEnabled": "true"}])
        await asyncio.gather(*client._actions)
        assert counted.call_count == 2

        await client._process_event("onAdStateChange", [{"adState": "1082", "currentTime": "0"}])
        await client._process_event("adPlaying", [first])
        await asyncio.gather(*client._actions)
        assert counted.call_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_mute", [False, True])
async def test_late_skip_ack_counts_its_own_ad_without_marking_next_ad_counted(owned_mute):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    client._muted_for_ad = owned_mute
    first_sent = asyncio.Event()
    second_sent = asyncio.Event()
    finish_first = asyncio.Event()
    finish_second = asyncio.Event()
    requests = 0

    async def send(commands):
        nonlocal requests
        if commands[0][0] == "skipAd":
            requests += 1
            if requests == 1:
                first_sent.set()
                await finish_first.wait()
            else:
                second_sent.set()
                await finish_second.wait()
        return True

    with patch.object(client, "_send_commands", side_effect=send) as transport:
        try:
            await client._process_event(
                "adPlaying", [{"adVideoId": "ad-one", "isSkipEnabled": "true"}]
            )
            await asyncio.wait_for(first_sent.wait(), timeout=1)
            await client._process_event(
                "adPlaying", [{"adVideoId": "ad-two", "isSkipEnabled": "true"}]
            )
            finish_first.set()
            await asyncio.wait_for(second_sent.wait(), timeout=1)
            counted.assert_called_once_with()
            finish_second.set()
            await asyncio.gather(*client._actions)
            assert counted.call_count == 2
            # The first skip already restored any mute held by the integration.
            assert transport.await_args_list[-1] == call([("skipAd", None)])
        finally:
            finish_first.set()
            finish_second.set()
            await client.stop_subscription()


@pytest.mark.asyncio
async def test_pre_roll_content_identity_discovery_preserves_counted_ad():
    counted = Mock()
    client = _client(on_ad_skip=counted)
    ad = {"adVideoId": "ad-one", "contentVideoId": "content", "isSkipEnabled": "true"}
    with patch.object(client, "_send_command", return_value=True):
        await client._process_event("adPlaying", [ad])
        await asyncio.gather(*client._actions)
        counted.assert_called_once_with()
        await client._process_event(
            "nowPlaying",
            [
                {
                    "videoId": "content",
                    "adVideoId": "ad-one",
                    "state": "1081",
                    "currentTime": "1",
                    "duration": "15",
                }
            ],
        )
        await client._process_event("adPlaying", [ad])
        await asyncio.gather(*client._actions)
    counted.assert_called_once_with()


@pytest.mark.asyncio
async def test_failed_old_ad_skip_does_not_resume_a_new_ad():
    client = _client()
    sent = asyncio.Event()
    finish = asyncio.Event()

    async def send(command, parameters):
        sent.set()
        await finish.wait()
        return False

    with (
        patch.object(client, "_send_command", side_effect=send),
        patch.object(client._ad_replay, "resume_paused_ad", new_callable=AsyncMock) as resume,
    ):
        try:
            await client._process_event(
                "adPlaying", [{"adVideoId": "old-ad", "isSkipEnabled": "true"}]
            )
            await asyncio.wait_for(sent.wait(), timeout=1)
            await client._process_event(
                "adPlaying", [{"adVideoId": "new-ad", "isSkipEnabled": "false"}]
            )
            finish.set()
            await asyncio.gather(*client._actions)
        finally:
            finish.set()
            await client.stop_subscription()
    resume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, OSError("Request failed"), asyncio.CancelledError()])
async def test_unacknowledged_skip_does_not_count(failure):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    send = AsyncMock(**({"return_value": False} if failure is False else {"side_effect": failure}))
    with patch.object(client, "_send_command", send):
        await client._process_event("adPlaying", [{"adVideoId": "ad-one", "isSkipEnabled": "true"}])
        await asyncio.gather(*client._actions, return_exceptions=True)
    counted.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_ad_ending_without_a_skip_command_does_not_count(disabled):
    counted = Mock()
    client = _client(on_ad_skip=counted)
    client.skip_ads = not disabled
    with patch.object(client, "_send_command", return_value=True) as send:
        await client._process_event(
            "adPlaying", [{"isSkippable": "true", "isSkipEnabled": "true" if disabled else "false"}]
        )
        await asyncio.gather(*client._actions)
        await client._process_event("onAdStateChange", [{"adState": "1082", "currentTime": "0"}])
        await asyncio.gather(*client._actions)
    send.assert_not_awaited()
    counted.assert_not_called()


@pytest.mark.asyncio
async def test_batch_and_single_command_use_separate_request_and_message_counters():
    response = SimpleNamespace(status=200, text=AsyncMock(return_value=""), raise_for_status=Mock())
    request = AsyncMock()
    request.__aenter__.return_value = response
    session = SimpleNamespace(post=Mock(return_value=request))
    client = _client()
    client.session = session

    assert await client._commands([("skipAd", None), ("setVolume", {"muted": "false"})])
    assert await client.get_now_playing()

    first, second = session.post.call_args_list
    assert first.kwargs["data"] == {
        "count": 2,
        "ofs": 1,
        "req0__sc": "skipAd",
        "req1__sc": "setVolume",
        "req1_muted": "false",
    }
    assert first.kwargs["params"]["RID"] == 2
    assert second.kwargs["data"] == {"count": 1, "ofs": 3, "req0__sc": "getNowPlaying"}
    assert second.kwargs["params"]["RID"] == 3
