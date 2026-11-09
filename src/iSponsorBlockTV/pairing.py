"""Validate YouTube TV credentials without starting a playback session."""

from dataclasses import dataclass, field
from http import HTTPStatus

from aiohttp import ClientError, ClientSession, ClientTimeout
from pyytlounge.wrapper import api_base

_TIMEOUT = ClientTimeout(total=10)
_CODE_REJECTION_STATUSES = {HTTPStatus.BAD_REQUEST, HTTPStatus.NOT_FOUND, HTTPStatus.GONE}


class PairingRejected(Exception):
    """YouTube rejected the code or the saved screen ID."""


class PairingUnavailable(Exception):
    """YouTube could not return a usable pairing response."""


@dataclass(frozen=True)
class PairedScreen:
    """A verified screen and its temporary Lounge token."""

    screen_id: str = field(repr=False)
    name: str | None
    lounge_token: str = field(repr=False)


def normalize_pairing_code(code: str) -> str:
    """Remove formatting from a TV code."""
    return code.replace("-", "").replace(" ", "")


def is_valid_screen_id(screen_id: str) -> bool:
    """Accept opaque IDs without allowing a batch of IDs in the request."""
    return (
        0 < len(screen_id) <= 512
        and screen_id.isascii()
        and screen_id.isprintable()
        and " " not in screen_id
        and "," not in screen_id
    )


async def _async_post_pairing(
    session: ClientSession,
    endpoint: str,
    data: dict[str, str],
    *,
    code_lookup: bool = False,
) -> dict:
    try:
        async with session.post(
            f"{api_base}/pairing/{endpoint}",
            data=data,
            timeout=_TIMEOUT,
            allow_redirects=False,
        ) as response:
            if code_lookup and response.status in _CODE_REJECTION_STATUSES:
                raise PairingRejected
            if response.status != HTTPStatus.OK:
                raise PairingUnavailable
            payload = await response.json(content_type=None)
    except (ClientError, TimeoutError, ValueError):
        raise PairingUnavailable from None

    if not isinstance(payload, dict) or "error" in payload:
        raise PairingUnavailable
    return payload


def _parse_screen(screen: object) -> PairedScreen:
    if not isinstance(screen, dict):
        raise PairingUnavailable
    screen_id = screen.get("screenId")
    token = screen.get("loungeToken")
    if not isinstance(screen_id, str) or not is_valid_screen_id(screen_id):
        raise PairingUnavailable
    if not isinstance(token, str) or not token.strip():
        raise PairingUnavailable
    name = screen.get("name")
    return PairedScreen(screen_id, (name.strip() or None) if isinstance(name, str) else None, token)


async def async_pair_with_code(session: ClientSession, code: str) -> PairedScreen:
    """Exchange a short-lived TV code for a screen ID."""
    code = normalize_pairing_code(code)
    if len(code) != 12 or not code.isascii() or not code.isdecimal():
        raise PairingRejected
    payload = await _async_post_pairing(
        session, "get_screen", {"pairing_code": code}, code_lookup=True
    )
    if payload in ({}, {"screen": None}, {"screen": {}}):
        raise PairingRejected
    return _parse_screen(payload.get("screen"))


async def async_verify_screen_id(session: ClientSession, screen_id: str) -> PairedScreen:
    """Verify that YouTube still issues a token for this saved screen ID."""
    screen_id = screen_id.strip()
    if not is_valid_screen_id(screen_id):
        raise PairingRejected
    payload = await _async_post_pairing(
        session, "get_lounge_token_batch", {"screen_ids": screen_id}
    )
    screens = payload.get("screens")
    if screens == []:
        raise PairingRejected
    if not isinstance(screens, list) or len(screens) != 1:
        raise PairingUnavailable
    screen = _parse_screen(screens[0])
    if screen.screen_id != screen_id:
        raise PairingUnavailable
    return screen
