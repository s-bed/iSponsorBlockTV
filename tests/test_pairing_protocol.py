"""Verify YouTube pairing responses and passive DIAL requests."""

from asyncio import IncompleteReadError
from unittest.mock import AsyncMock, Mock

import pytest
from custom_components.isponsorblocktv.core.pairing import (
    PairingRejected,
    PairingUnavailable,
    async_pair_with_code,
    async_verify_screen_id,
)
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo

from custom_components.isponsorblocktv.const import DIAL_SERVICE
from custom_components.isponsorblocktv.dial import async_probe_dial

SCREEN = {"screenId": "a" * 64, "loungeToken": "fake-token", "name": "Living room"}
UDN = "uuid:test-tv"


def _response(status, payload=None, *, headers=None, content=None):
    response = AsyncMock(status=status, headers=headers or {})
    response.__aenter__.return_value = response
    response.json.return_value = payload
    if content is not None:
        response.content.readexactly.side_effect = IncompleteReadError(content, len(content) + 1)
    return response


async def test_tv_code_normalizes_and_verifies():
    session = Mock()
    session.post.return_value = _response(200, {"screen": SCREEN})

    paired = await async_pair_with_code(session, "0123-4567 8901")

    assert paired.screen_id == SCREEN["screenId"]
    assert paired.name == SCREEN["name"]
    assert session.post.call_args.kwargs["data"] == {"pairing_code": "012345678901"}


async def test_existing_id_only_needs_token_lookup():
    session = Mock()
    session.post.return_value = _response(200, {"screens": [SCREEN]})

    paired = await async_verify_screen_id(session, f" {SCREEN['screenId']} ")

    assert paired.lounge_token == SCREEN["loungeToken"]
    assert session.post.call_args.args[0].endswith("/get_lounge_token_batch")
    assert session.post.call_args.kwargs["data"] == {"screen_ids": SCREEN["screenId"]}
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    ("status", "payload", "error"),
    [
        (200, {"screens": []}, PairingRejected),
        (503, {"screens": []}, PairingUnavailable),
        (200, {}, PairingUnavailable),
    ],
)
async def test_screen_rejection_is_distinct_from_an_unusable_response(status, payload, error):
    session = Mock()
    session.post.return_value = _response(status, payload)

    with pytest.raises(error):
        await async_verify_screen_id(session, SCREEN["screenId"])


def _discovery():
    return SsdpServiceInfo(
        ssdp_usn=f"{UDN}::{DIAL_SERVICE}",
        ssdp_st=DIAL_SERVICE,
        ssdp_headers={"_remote_addr": ("192.168.1.10", 1900)},
        ssdp_location="http://192.168.1.10:8008/ssdp/device-desc.xml",
        upnp={"friendlyName": "Living room"},
    )


async def test_dial_probe_only_reads_device_and_app():
    session = Mock()
    xml = (
        '<service xmlns="urn:dial-multiscreen-org:schemas:dial">'
        f"<additionalData><screenId>{SCREEN['screenId']}</screenId></additionalData>"
        "</service>"
    )
    session.get.side_effect = [
        _response(200, headers={"Application-URL": "http://192.168.1.10:8008/apps/"}),
        _response(200, content=xml.encode()),
    ]

    screen = await async_probe_dial(session, _discovery())

    assert screen is not None
    assert (screen.screen_id, screen.udn) == (SCREEN["screenId"], UDN)
    assert session.get.call_count == 2
    assert str(session.get.call_args.args[0]) == "http://192.168.1.10:8008/apps/YouTube"
    assert all(not call.kwargs["allow_redirects"] for call in session.get.call_args_list)
    session.post.assert_not_called()


async def test_dial_probe_ignores_app_url_on_another_host():
    session = Mock()
    session.get.return_value = _response(
        200, headers={"Application-URL": "http://192.168.1.11:8008/apps/"}
    )

    assert await async_probe_dial(session, _discovery()) is None
    session.get.assert_called_once()
