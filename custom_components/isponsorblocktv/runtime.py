"""Manage one TV's Lounge connection and Home Assistant state."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from pyytlounge.models import State

from .const import CONF_SCREEN_ID
from .core.api_helpers import ApiHelper
from .core.pairing import PairingRejected, PairingUnavailable, async_verify_screen_id
from .core.playback import PlaybackScheduler
from .core.ytlounge import YtLoungeApi

_LOGGER = logging.getLogger(__name__)
AUTH_CHECK_INTERVAL = 15 * 60
RETRY_INTERVAL = 10
MAX_RETRY_INTERVAL = 60
DISCONNECT_TIMEOUT = 3
PLAYBACK_STATES = {
    State.Stopped: "idle",
    State.Buffering: "idle",  # YouTube sends this value when playback ends.
    State.Playing: "playing",
    State.Paused: "paused",
    State.Starting: "buffering",
    State.Cued: "idle",
    State.Advertisement: "playing",
}


@dataclass(frozen=True, slots=True)
class TVSnapshot:
    """Latest reported values for the TV."""

    connected: bool = False
    pairing_problem: bool | None = None
    playback_state: str | None = None
    ad_playing: bool | None = None
    ad_skippable: bool | None = None
    skip_count: int = 0
    last_skip: datetime | None = None
    video_id: str | None = None
    duration: float | None = None
    playback_speed: float | None = None
    volume: int | None = None
    muted: bool | None = None


class TVRuntime:
    """Reconnect to YouTube and publish push updates to entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ISponsorBlockTVConfigEntry,
        session: ClientSession,
        options: dict[str, Any],
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._session = session
        self._screen_id = entry.data[CONF_SCREEN_ID]
        self._listeners: set[Callable[[], None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._rejections = 0
        self.snapshot = TVSnapshot()

        config = SimpleNamespace(
            **{
                **options,
                "devices": [entry.entry_id],
                "channel_whitelist": [
                    {"id": channel_id} for channel_id in options["channel_whitelist"]
                ],
            }
        )
        api = ApiHelper(config, session)
        self._client = YtLoungeApi(
            self._screen_id,
            config,
            api,
            _LOGGER,
            web_session=session,
            standalone=False,
            on_update=self._async_lounge_updated,
        )
        self._scheduler = PlaybackScheduler(
            api, self._client, options["offset"] / 1000, _LOGGER, on_skip=self._async_skipped
        )

    @callback
    def async_start(self) -> None:
        """Start the config entry's background worker."""
        self._task = self._entry.async_create_background_task(
            self._hass, self._async_run(), "YouTube Lounge listener"
        )

    async def async_stop(self) -> None:
        """Cancel and await the worker."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @callback
    def async_subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Notify an entity when the snapshot changes."""
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _async_publish(self, **changes: Any) -> None:
        snapshot = replace(self.snapshot, **changes)
        if snapshot == self.snapshot:
            return
        self.snapshot = snapshot
        for listener in tuple(self._listeners):
            listener()

    @callback
    def _async_lounge_updated(self) -> None:
        if not self.snapshot.connected:
            return
        state = self._client.playback_state
        duration = state.duration if state and state.videoId else None
        self._async_publish(
            playback_state=PLAYBACK_STATES.get(state.state) if state else None,
            video_id=(state.videoId or None) if state else None,
            duration=duration if duration and math.isfinite(duration) and duration > 0 else None,
            playback_speed=self._client.playback_speed,
            volume=self._client.volume,
            muted=self._client.muted,
            ad_playing=self._client.ad_playing,
            ad_skippable=self._client.ad_skippable,
        )

    @callback
    def _async_skipped(self) -> None:
        self._async_publish(skip_count=self.snapshot.skip_count + 1, last_skip=datetime.now(UTC))

    async def _async_verify(self) -> str:
        try:
            screen = await async_verify_screen_id(self._session, self._screen_id)
        except PairingRejected:
            self._rejections += 1
            raise
        except PairingUnavailable:
            self._rejections = 0
            raise
        self._rejections = 0
        self._async_publish(pairing_problem=False)
        return screen.lounge_token

    async def _async_run(self) -> None:
        retry = RETRY_INTERVAL
        while True:
            try:
                self._client.auth.lounge_id_token = await self._async_verify()
                if await self._client.is_available() and await self._client.connect():
                    retry = RETRY_INTERVAL
                    self._async_publish(connected=True)
                    self._async_lounge_updated()
                    await self._async_listen()
            except PairingRejected:
                if self._rejections >= 2:
                    self._async_publish(pairing_problem=True)
                    self._entry.async_start_reauth(self._hass)
                    return
                retry = RETRY_INTERVAL
            except Exception as err:  # noqa: BLE001
                # HTTP errors can include Lounge credentials in their URLs.
                _LOGGER.debug("YouTube connection will retry (%s)", type(err).__name__)
            finally:
                self._async_publish(
                    connected=False,
                    playback_state=None,
                    ad_playing=None,
                    ad_skippable=None,
                    video_id=None,
                    duration=None,
                    playback_speed=None,
                    volume=None,
                    muted=None,
                )
                await self._client.stop_subscription()
                await self._scheduler.cancel()
                with suppress(Exception):
                    async with asyncio.timeout(DISCONNECT_TIMEOUT):
                        await self._client.disconnect()
                self._client.reset_playback()

            await asyncio.sleep(retry)
            retry = min(retry * 2, MAX_RETRY_INTERVAL)

    async def _async_listen(self) -> None:
        subscription = asyncio.create_task(self._client.run_subscription(self._scheduler))
        try:
            while True:
                done, _ = await asyncio.wait({subscription}, timeout=AUTH_CHECK_INTERVAL)
                if done:
                    await subscription
                    return
                # Validate the saved ID without replacing the token of the live session.
                await self._async_verify()
        finally:
            subscription.cancel()
            await asyncio.gather(subscription, return_exceptions=True)


ISponsorBlockTVConfigEntry = ConfigEntry[TVRuntime]
