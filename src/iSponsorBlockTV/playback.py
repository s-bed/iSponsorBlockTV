"""Schedule playback actions from YouTube Lounge updates."""

import asyncio
from time import monotonic

from pyytlounge.models import State


class PlaybackScheduler:
    """Schedule SponsorBlock seeks and video dismissal for one screen."""

    def __init__(
        self,
        api_helper,
        client,
        offset_seconds,
        logger,
        *,
        on_skip=None,
        on_short_disruption=None,
        short_video_duration_threshold: float | None = None,
        blocked_channels: frozenset[str] = frozenset(),
    ):
        self._api_helper = api_helper
        self._client = client
        self._offset = offset_seconds
        self._logger = logger
        self._on_skip = on_skip
        self._on_short_disruption = on_short_disruption
        self._short_video_duration_threshold = short_video_duration_threshold
        self._blocked_channels = blocked_channels
        self._pending = None
        self._tasks = set()
        self._seeking = False
        self._last_skip = None
        self._disturbed_video = None
        self._channel_video = ""
        self._channel_blocked: bool | None = None

    async def __call__(self, state):
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._last_skip is not None:
            last_video, last_end = self._last_skip
            if state.videoId != last_video or (
                state.currentTime is not None and state.currentTime >= last_end
            ):
                self._last_skip = None
        if state.state in (State.Stopped, State.Buffering):
            self._disturbed_video = None
        if state.state in (State.Stopped, State.Buffering) or (
            state.videoId and state.videoId != self._channel_video
        ):
            self._channel_video = state.videoId
            self._channel_blocked = None
        if state.state != State.Playing or not state.videoId:
            return
        if state.videoId != self._disturbed_video:
            self._disturbed_video = None

        threshold = self._short_video_duration_threshold
        if threshold is not None and state.duration is not None and 0 < state.duration < threshold:
            if self._disturbed_video == state.videoId:
                return
            work = self._dismiss_video(state)
        elif self._blocked_channels and self._channel_blocked is not False:
            if self._disturbed_video == state.videoId:
                return
            work = self._check_channel(state, monotonic())
        elif state.currentTime is not None:
            work = self._schedule(state.videoId, state.currentTime, monotonic())
        else:
            return
        task = asyncio.create_task(work)
        self._pending = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _check_channel(self, state, sampled_at):
        try:
            if self._channel_blocked is None:
                channel_id = await self._api_helper.get_channel_id(state.videoId)
                self._channel_blocked = channel_id in self._blocked_channels
            if self._channel_blocked:
                await self._dismiss_video(state, blocked_channel=True)
            elif state.currentTime is not None:
                await self._schedule(state.videoId, state.currentTime, sampled_at)
        finally:
            if self._pending is asyncio.current_task():
                self._pending = None

    async def dismiss_after_reconnect(self):
        """Dismiss the Shorts attempt on a newly restored connection."""
        await self._dismiss_video(self._client.playback_state, after_reconnect=True)

    async def _dismiss_video(self, state, *, after_reconnect=False, blocked_channel=False):
        def before_send() -> bool:
            # Playback updates cancel pending work while it waits for the command lock.
            if not self._client.connected() or (self._client.ad_playing and not after_reconnect):
                return False
            if blocked_channel:
                current = self._client.playback_state
                if (
                    current is None
                    or current.state != State.Playing
                    or current.videoId != state.videoId
                    or self._disturbed_video == state.videoId
                ):
                    return False
            self._pending = None
            self._disturbed_video = state.videoId if state else None
            if after_reconnect:
                self._logger.debug("Sending Back after reconnecting from a screen disconnect")
            elif blocked_channel:
                self._logger.debug("Dismissing video from a blocked YouTube channel with Back")
            else:
                self._logger.debug(
                    "Dismissing short video (duration %.3f s, threshold %.3f s) with Back",
                    state.duration,
                    self._short_video_duration_threshold,
                )
            return True

        try:
            dismissed = await self._client.press_back(only_if=before_send)
            if dismissed and not blocked_channel and self._on_short_disruption is not None:
                self._on_short_disruption()
        except Exception as err:  # noqa: BLE001
            reason = "Channel blocklist" if blocked_channel else "Short video"
            self._logger.debug("%s dismissal failed (%s)", reason, type(err).__name__)
        finally:
            if self._pending is asyncio.current_task():
                self._pending = None

    async def _schedule(self, video_id, position, sampled_at):
        try:
            async with asyncio.timeout(10):
                segments = await self._api_helper.get_segments(video_id)
            for segment in segments:
                start, end = segment["start"], segment["end"]
                if self._last_skip == (video_id, end):
                    continue
                if start >= position or start <= position < 1 < end:
                    speed = self._client.playback_speed or 1.0
                    delay = (max(start, position) - position) / speed
                    await asyncio.sleep(delay - (monotonic() - sampled_at) - self._offset)
                    break
            else:
                return

            self._pending = None
            if self._seeking or self._client.ad_playing or self._last_skip == (video_id, end):
                return
            self._seeking = True
            try:
                self._logger.debug(
                    "SponsorBlock seeking past segment %.3f to %.3f s (sampled position %.3f s)",
                    start,
                    end,
                    position,
                )
                skipped = await self._client.seek_to(end)
                if skipped:
                    self._last_skip = (video_id, end)
                    if self._on_skip is not None:
                        self._on_skip()
            finally:
                self._seeking = False

            if skipped:
                async with asyncio.timeout(10):
                    await self._api_helper.mark_viewed_segments(segment["UUID"])
        except Exception as err:  # noqa: BLE001
            # An HTTP error can expose API credentials in its URL.
            self._logger.debug("SponsorBlock playback update failed (%s)", type(err).__name__)
        finally:
            if self._pending is asyncio.current_task():
                self._pending = None

    async def cancel(self, *, reset: bool = True):
        """Stop pending work and optionally clear the record of recent commands."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending = None
        if reset:
            self._last_skip = None
            self._disturbed_video = None
            self._channel_video = ""
            self._channel_blocked = None
