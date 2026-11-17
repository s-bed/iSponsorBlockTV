"""Fetch SponsorBlock segments and YouTube channel metadata."""

import logging
from asyncio import Task, create_task, gather, shield, timeout
from collections import OrderedDict
from hashlib import sha256
from time import monotonic

from aiohttp import ClientError, ClientSession

from . import constants
from .conditional_ttl_cache import AsyncConditionalTTL

_LOGGER = logging.getLogger(__name__)
_CHANNEL_CACHE_SIZE = 256
_CHANNEL_CACHE_TTL = 24 * 60 * 60
_CHANNEL_MISS_TTL = 5
_CHANNEL_LOOKUP_TIMEOUT = 3


class ApiHelper:
    """Cache playback metadata and report viewed SponsorBlock segments."""

    def __init__(self, config, web_session: ClientSession) -> None:
        self.apikey = config.apikey
        self.skip_categories = config.skip_categories
        self.channel_whitelist = config.channel_whitelist
        self.skip_count_tracking = config.skip_count_tracking
        self.web_session = web_session
        self.minimum_skip_length = config.minimum_skip_length
        self.sponsorblock_api_url = config.sponsorblock_api_url
        self._channel_cache: OrderedDict[str, tuple[str | None, float]] = OrderedDict()
        self._channel_lookups: dict[str, Task[str | None]] = {}

    async def is_whitelisted(self, vid_id):
        if self.apikey and self.channel_whitelist:
            channel_id = await self.get_channel_id(vid_id)
            return channel_id is not None and any(
                channel["id"] == channel_id for channel in self.channel_whitelist
            )
        return False

    async def get_channel_id(self, vid_id: str) -> str | None:
        """Resolve a video to its channel, sharing lookups between playback rules."""
        if not self.apikey:
            return None
        if (cached := self._channel_cache.get(vid_id)) is not None:
            channel_id, expires = cached
            if monotonic() < expires:
                self._channel_cache.move_to_end(vid_id)
                return channel_id
            del self._channel_cache[vid_id]

        if (lookup := self._channel_lookups.get(vid_id)) is None:
            lookup = self._channel_lookups[vid_id] = create_task(self._lookup_channel_id(vid_id))
            lookup.add_done_callback(lambda _: self._channel_lookups.pop(vid_id, None))
        # A new playback sample can replace a waiter while the lookup is still useful.
        return await shield(lookup)

    async def cancel_channel_lookups(self) -> None:
        """Stop metadata requests before shutting down their owner."""
        tasks = tuple(self._channel_lookups.values())
        for task in tasks:
            task.cancel()
        await gather(*tasks, return_exceptions=True)

    async def _lookup_channel_id(self, vid_id: str) -> str | None:
        channel_id = await self._fetch_channel_id(vid_id)
        ttl = _CHANNEL_CACHE_TTL if channel_id is not None else _CHANNEL_MISS_TTL
        self._channel_cache[vid_id] = (channel_id, monotonic() + ttl)
        if len(self._channel_cache) > _CHANNEL_CACHE_SIZE:
            self._channel_cache.popitem(last=False)
        return channel_id

    async def _fetch_channel_id(self, vid_id: str) -> str | None:
        params = {
            "id": vid_id,
            "part": "snippet",
            "fields": "items(snippet/channelId)",
        }
        headers = {"X-Goog-Api-Key": self.apikey}
        try:
            url = constants.Youtube_api + "videos"
            async with (
                timeout(_CHANNEL_LOOKUP_TIMEOUT),
                self.web_session.get(url, params=params, headers=headers) as resp,
            ):
                if resp.status != 200:
                    _LOGGER.debug("YouTube channel lookup returned HTTP %s", resp.status)
                    return None
                data = await resp.json()
            if not data["items"]:
                return None
            channel_id = data["items"][0]["snippet"]["channelId"]
            return channel_id if isinstance(channel_id, str) and channel_id else None
        except (ClientError, KeyError, RuntimeError, TimeoutError, TypeError, ValueError) as err:
            _LOGGER.debug("YouTube channel lookup failed (%s)", type(err).__name__)
            return None

    @AsyncConditionalTTL(time_to_live=300, maxsize=10)  # 5 minutes for non-locked segments
    async def get_segments(self, vid_id):
        if not self.skip_categories:
            return ([], True)  # Categories explicitly empty, skip segment fetching
        if await self.is_whitelisted(vid_id):
            return (
                [],
                True,
            )  # Return empty list and True to indicate
            # that the cache should last forever
        vid_id_hashed = sha256(vid_id.encode("utf-8")).hexdigest()[
            :4
        ]  # Hashes video id and gets the first 4 characters
        params = {
            "category": self.skip_categories,
            "actionType": constants.SponsorBlock_actiontype,
            "service": constants.SponsorBlock_service,
        }
        headers = {"Accept": "application/json"}
        url = self.sponsorblock_api_url + "skipSegments/" + vid_id_hashed
        async with self.web_session.get(url, headers=headers, params=params) as response:
            if response.status == 404:
                return [], False
            if response.status != 200:
                _LOGGER.debug("SponsorBlock segment lookup returned HTTP %s", response.status)
                return [], False
            response_json = await response.json()
        for i in response_json:
            if str(i["videoID"]) == str(vid_id):
                response_json = i
                break
        return self.process_segments(response_json, self.minimum_skip_length)

    @staticmethod
    def process_segments(response, minimum_skip_length):
        segments = []
        ignore_ttl = True
        try:
            response_segments = response["segments"]
            # sort by end
            response_segments.sort(key=lambda x: x["segment"][1])
            # extend ends of overlapping segments to make one big segment
            for i in response_segments:
                for j in response_segments:
                    if j["segment"][0] <= i["segment"][1] <= j["segment"][1]:
                        i["segment"][1] = j["segment"][1]

            # sort by start
            response_segments.sort(key=lambda x: x["segment"][0])
            # extend starts of overlapping segments to make one big segment
            for i in reversed(response_segments):
                for j in reversed(response_segments):
                    if j["segment"][0] <= i["segment"][0] <= j["segment"][1]:
                        i["segment"][0] = j["segment"][0]

            for i in response_segments:
                ignore_ttl = (
                    ignore_ttl and i["locked"] == 1
                )  # If all segments are locked, ignore ttl
                segment = i["segment"]
                UUID = i["UUID"]
                segment_dict = {"start": segment[0], "end": segment[1], "UUID": [UUID]}
                try:
                    # Get segment before to check if they are too close to each other
                    segment_before_end = segments[-1]["end"]
                    segment_before_start = segments[-1]["start"]
                    segment_before_UUID = segments[-1]["UUID"]

                except IndexError:
                    segment_before_end = -10
                if (
                    segment_dict["start"] - segment_before_end < 1
                ):  # Less than 1 second apart, combine them and skip them together
                    segment_dict["start"] = segment_before_start
                    segment_dict["UUID"].extend(segment_before_UUID)
                    segments.pop()
                # Only add segments greater than minimum skip length
                if segment_dict["end"] - segment_dict["start"] > minimum_skip_length:
                    segments.append(segment_dict)
        except KeyError, TypeError, ValueError:
            pass
        return segments, ignore_ttl

    async def mark_viewed_segments(self, uuids):
        """Marks the segments as viewed in the SponsorBlock API
        if skip_count_tracking is enabled.
        Lets the contributor know that someone skipped the segment (thanks)"""
        if self.skip_count_tracking:
            for i in uuids:
                url = self.sponsorblock_api_url + "viewedVideoSponsorTime/"
                params = {"UUID": i}
                async with self.web_session.post(url, params=params):
                    pass
