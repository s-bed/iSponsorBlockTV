"""Schedule playback actions from YouTube Lounge updates."""

import asyncio
from time import monotonic

from pyytlounge.models import State


class PlaybackScheduler:
    """Schedule SponsorBlock seeks and short-video dismissal for one screen."""

    def __init__(
        self,
        api_helper,
        client,
        offset_seconds,
        logger,
        *,
        on_skip=None,
        short_video_duration_threshold: float | None = None,
    ):
        self._api_helper = api_helper
        self._client = client
        self._offset = offset_seconds
        self._logger = logger
        self._on_skip = on_skip
        self._short_video_duration_threshold = short_video_duration_threshold
        self._pending = None
        self._tasks = set()
        self._seeking = False
        self._last_skip = None
        self._disturbed_video = None

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
        if state.state != State.Playing or not state.videoId:
            return
        if state.videoId != self._disturbed_video:
            self._disturbed_video = None

        threshold = self._short_video_duration_threshold
        if threshold is not None and state.duration is not None and 0 < state.duration < threshold:
            if self._disturbed_video == state.videoId:
                return
            work = self._disturb_short_video(state)
        elif state.currentTime is not None:
            work = self._schedule(state.videoId, state.currentTime, monotonic())
        else:
            return
        task = asyncio.create_task(work)
        self._pending = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _disturb_short_video(self, state):
        def before_send() -> bool:
            # Playback updates cancel pending work while it waits for the command lock.
            if self._client.ad_playing or not self._client.connected():
                return False
            self._pending = None
            self._disturbed_video = state.videoId
            self._logger.debug(
                "Dismissing short video (duration %.3f s, threshold %.3f s) with Back",
                state.duration,
                self._short_video_duration_threshold,
            )
            return True

        try:
            await self._client.press_back(only_if=before_send)
        except Exception as err:  # noqa: BLE001
            self._logger.debug("Short video dismissal failed (%s)", type(err).__name__)
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

    async def cancel(self):
        """Stop all pending work and discard the previous playback sample."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending = None
        self._last_skip = None
        self._disturbed_video = None
