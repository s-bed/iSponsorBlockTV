"""Read YouTube channel metadata for the Home Assistant options flow."""

import asyncio
import html
import re
from collections.abc import Collection
from dataclasses import dataclass
from itertools import batched
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .core.constants import Youtube_api

_CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}")
_TIMEOUT = ClientTimeout(total=8)


class ChannelLookupError(Exception):
    """YouTube did not return the requested metadata."""


class ChannelApiKeyRejected(ChannelLookupError):
    """YouTube rejected access to the Data API."""


@dataclass(frozen=True, slots=True)
class ChannelSearchResult:
    """A channel name and ID returned by YouTube search."""

    channel_id: str
    title: str
    description: str = ""

    @property
    def label(self) -> str:
        description = " ".join(self.description.split())[:80]
        identity = f"{self.title} ({self.channel_id})"
        return f"{identity} · {description}" if description else identity


def is_channel_id(value: str) -> bool:
    """Check the shape of a canonical YouTube channel ID."""
    return _CHANNEL_ID.fullmatch(value) is not None


async def _get_items(
    session: ClientSession,
    api_key: str,
    endpoint: str,
    params: dict[str, str],
    *,
    validating_key: bool = False,
) -> list[dict[str, Any]]:
    try:
        async with session.get(
            Youtube_api + endpoint,
            params=params,
            headers={"X-Goog-Api-Key": api_key},
            timeout=_TIMEOUT,
        ) as response:
            if validating_key and response.status in (400, 401, 403):
                raise ChannelApiKeyRejected
            if response.status != 200:
                raise ChannelLookupError
            payload = await response.json()
    except (ClientError, TimeoutError, ValueError):
        # Keep request details out of options errors.
        raise ChannelLookupError from None
    if not isinstance(payload, dict) or not isinstance(items := payload.get("items"), list):
        raise ChannelLookupError
    if not all(isinstance(item, dict) for item in items):
        raise ChannelLookupError
    return items


async def async_validate_api_key(session: ClientSession, api_key: str) -> None:
    """Check access using a read outside the channel-search quota."""
    await _get_items(
        session,
        api_key,
        "i18nRegions",
        {"part": "snippet", "fields": "items(id)"},
        validating_key=True,
    )


async def async_search_channels(
    session: ClientSession, api_key: str, query: str
) -> tuple[ChannelSearchResult, ...]:
    """Find channel matches and return their canonical IDs."""
    items = await _get_items(
        session,
        api_key,
        "search",
        {
            "part": "snippet",
            "type": "channel",
            "q": query,
            "maxResults": "5",
            "fields": "items(id(channelId),snippet(title,description))",
        },
    )
    matches = {}
    for item in items:
        identity = item.get("id")
        snippet = item.get("snippet")
        if not isinstance(identity, dict) or not isinstance(snippet, dict):
            continue
        channel_id = identity.get("channelId")
        title = snippet.get("title")
        if not isinstance(channel_id, str) or not is_channel_id(channel_id):
            continue
        if not isinstance(title, str) or not title.strip():
            continue
        description = snippet.get("description")
        matches[channel_id] = ChannelSearchResult(
            channel_id,
            html.unescape(title),
            html.unescape(description) if isinstance(description, str) else "",
        )
    return tuple(matches.values())


async def async_channel_titles(
    session: ClientSession, api_key: str, channel_ids: Collection[str]
) -> dict[str, str]:
    """Resolve saved channel names for display, falling back to IDs on failure."""
    titles = {}
    try:
        async with asyncio.timeout(_TIMEOUT.total):
            for batch in batched(channel_ids, 50):
                items = await _get_items(
                    session,
                    api_key,
                    "channels",
                    {
                        "part": "snippet",
                        "id": ",".join(batch),
                        "fields": "items(id,snippet(title))",
                    },
                )
                for item in items:
                    channel_id = item.get("id")
                    snippet = item.get("snippet")
                    if (
                        isinstance(channel_id, str)
                        and isinstance(snippet, dict)
                        and isinstance(title := snippet.get("title"), str)
                        and title.strip()
                    ):
                        titles[channel_id] = html.unescape(title)
    except (ChannelLookupError, TimeoutError):
        pass
    return titles
