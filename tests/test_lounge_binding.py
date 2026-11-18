"""Verify the mobile and native web Lounge binding profiles."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pyytlounge
import pytest
from custom_components.isponsorblocktv.core import ytlounge
from custom_components.isponsorblocktv.core.ytlounge import YtLoungeApi


def _request(response):
    request = AsyncMock()
    request.__aenter__.return_value = response
    return request


def _response(*, status=200, text=""):
    return SimpleNamespace(
        status=status,
        reason="private-response-reason",
        text=AsyncMock(return_value=text),
        raise_for_status=Mock(),
        content=SimpleNamespace(readline=AsyncMock(return_value=b"")),
    )


def _client(*, web=False, response=None):
    events = json.dumps([[0, ["c", "session"]], [1, ["S", "gsession"]]])
    response = response or _response(text=f"{len(events) + 1}\n{events}\n")
    session = SimpleNamespace(
        post=Mock(return_value=_request(response)),
        get=Mock(return_value=_request(_response())),
    )
    client = YtLoungeApi("private-screen-id", web_session=session)
    client.auth.lounge_id_token = "private-token"
    client.use_web_binding = web
    return client


@pytest.mark.parametrize("web", [False, True])
async def test_binding_profiles_keep_subscription_and_command_parameters_consistent(web, caplog):
    client = _client(web=web)
    device_id = client.auth.device_id
    caplog.set_level(logging.DEBUG, logger=client.logger.name)
    assert await client.connect()

    binding = client.session.post.call_args
    body = binding.kwargs["data"]
    assert body["id"] == device_id
    assert body["device"] == "REMOTE_CONTROL"
    assert body["capabilities"] == "que,dsdtr,atp,vsp"
    assert "connectParams" not in body
    assert body["app"] == ("web" if web else "ytios-phone-20.15.1")
    if web:
        assert body["magnaKey"] == "cloudPairedDevice"
        assert "deviceContext" in body
    else:
        assert body["pairing_type"] == "manual"
        assert body["RID"] == "1"

    await pyytlounge.YtLoungeApi.subscribe(client)
    assert await client.get_now_playing()
    subscribe_params = client.session.get.call_args.kwargs["params"]
    command = client.session.post.call_args
    expected_app = "youtube-desktop" if web else "ytios-phone-20.15.1"
    assert subscribe_params["app"] == command.kwargs["params"]["app"] == expected_app
    assert subscribe_params["RID"] == "rpc"
    assert command.kwargs["params"]["RID"] == 2
    assert command.kwargs["data"] == {"count": 1, "ofs": 1, "req0__sc": "getNowPlaying"}
    assert f"profile={'web' if web else 'mobile'}" in caplog.text
    assert "private-token" not in caplog.text
    assert "private-screen-id" not in caplog.text


@pytest.mark.parametrize("web", [False, True])
async def test_both_counters_reset_before_binding_events_are_processed(web):
    client = _client(web=web)
    client._command_offset = 19
    client._command_message_offset = 27
    process_events = client._process_events

    async def process(events):
        assert client._command_offset == client._command_message_offset == 1
        await process_events(events)

    with patch.object(client, "_process_events", side_effect=process) as bootstrap:
        assert await client.connect()
        bootstrap.assert_awaited_once()
    assert client._command_offset == client._command_message_offset == 1


@pytest.mark.parametrize("web", [False, True])
async def test_bootstrap_command_waits_until_bind_has_finished(web):
    client = _client(web=web)
    process_events = client._process_events

    async def bootstrap(events):
        await process_events(events)
        client._start_action(client.get_now_playing)
        await asyncio.sleep(0)
        assert client.session.post.call_count == 1
        assert client._command_offset == client._command_message_offset == 1

    with patch.object(client, "_process_events", side_effect=bootstrap):
        assert await client.connect()
        await asyncio.gather(*client._actions)
    assert client.session.post.call_count == 2
    command = client.session.post.call_args
    assert command.kwargs["params"]["RID"] == 2
    assert command.kwargs["data"]["ofs"] == 1
    assert client._command_offset == client._command_message_offset == 2


@pytest.mark.parametrize("web", [False, True])
@pytest.mark.parametrize("status", [401, 503])
async def test_rejected_bind_cannot_retain_previous_session(web, status, caplog):
    client = _client(web=web, response=_response(status=status, text="private-body"))
    client._sid = client._gsession = "old-session"
    assert not await client.connect()
    assert not client.connected()
    assert (client.auth.lounge_id_token is None) == (status == 401)
    assert client._command_offset == client._command_message_offset == 1
    assert "private-response-reason" not in caplog.text
    assert "private-body" not in caplog.text


@pytest.mark.parametrize("web", [False, True])
async def test_both_binding_profiles_bound_the_response_wait(web, monkeypatch, caplog):
    response = _response()

    async def wait_forever():
        await asyncio.Future()

    response.text.side_effect = wait_forever
    client = _client(web=web, response=response)
    monkeypatch.setattr(ytlounge, "_REQUEST_TIMEOUT", 0.001)
    with pytest.raises(TimeoutError):
        await client.connect()
    assert not client.connected()
    assert "private-response-reason" not in caplog.text
    assert "private-token" not in caplog.text
