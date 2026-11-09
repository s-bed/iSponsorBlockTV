"""Manage one TV's Lounge connection and Home Assistant state."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, TypedDict

from aiohttp import ClientSession
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from pyytlounge.models import State

from .const import CONF_SCREEN_ID, DOMAIN
from .core.api_helpers import ApiHelper
from .core.pairing import PairingRejected, PairingUnavailable, async_verify_screen_id
from .core.playback import PlaybackScheduler
from .core.ytlounge import YtLoungeApi

AUTH_CHECK_INTERVAL = 15 * 60
RETRY_INTERVAL = 10
MAX_RETRY_INTERVAL = 60
DISCONNECT_TIMEOUT = 3
SKIP_STATS_SAVE_DELAY = 5
PLAYBACK_STATES = {
    State.Stopped: "idle",
    State.Buffering: "idle",  # YouTube sends this value when playback ends.
    State.Playing: "playing",
    State.Paused: "paused",
    State.Starting: "buffering",
    State.Cued: "idle",
    State.Advertisement: "playing",
}


class SkipStats(TypedDict):
    """The two values retained between Home Assistant sessions."""

    count: int
    last_skip: str | None


def skip_stats_store(hass: HomeAssistant, entry_id: str) -> Store[SkipStats]:
    """Open storage for one TV's skip totals."""
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.skips", atomic_writes=True)


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
        self._logger = logging.getLogger(f"{__name__}.{entry.title}")
        self._listeners: set[Callable[[], None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._rejections = 0
        self._skip_store = skip_stats_store(hass, entry.entry_id)
        self._skip_stats_changed = False
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
            self._logger,
            web_session=session,
            standalone=False,
            on_update=self._async_lounge_updated,
        )
        self._scheduler = PlaybackScheduler(
            api,
            self._client,
            options["offset"] / 1000,
            self._logger,
            on_skip=self._async_skipped,
            short_video_duration_threshold=(
                options["short_video_duration_threshold"]
                if options["disturb_short_videos"]
                else None
            ),
        )

    async def async_restore_skip_stats(self) -> None:
        """Load the saved total before exposing the sensors."""
        if (stats := await self._skip_store.async_load()) is not None:
            last_skip = stats["last_skip"]
            self.snapshot = replace(
                self.snapshot,
                skip_count=stats["count"],
                last_skip=datetime.fromisoformat(last_skip) if last_skip else None,
            )

    @callback
    def async_start(self) -> None:
        """Start the config entry's background worker."""
        self._task = self._entry.async_create_background_task(
            self._hass, self._async_run(), "YouTube Lounge listener"
        )

    async def async_stop(self) -> None:
        """Stop playback work and flush the total before the entry reloads."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._skip_stats_changed:
            await self._skip_store.async_save(self._skip_stats_data())
            self._skip_stats_changed = False

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
        self._skip_stats_changed = True
        self._skip_store.async_delay_save(self._skip_stats_data, SKIP_STATS_SAVE_DELAY)

    @callback
    def _skip_stats_data(self) -> SkipStats:
        last_skip = self.snapshot.last_skip
        return {
            "count": self.snapshot.skip_count,
            "last_skip": last_skip.isoformat() if last_skip else None,
        }

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
                if not await self._client.is_available():
                    self._logger.debug("YouTube screen is unavailable")
                elif await self._client.connect():
                    self._logger.debug(
                        "Connected to YouTube Lounge (skip ads=%s, mute ads=%s)",
                        self._client.skip_ads,
                        self._client.mute_ads,
                    )
                    retry = RETRY_INTERVAL
                    if self._client.take_reconnect_back():
                        await self._scheduler.dismiss_after_reconnect()
                    self._async_publish(connected=True)
                    self._async_lounge_updated()
                    await self._async_listen()
            except PairingRejected:
                self._logger.debug("YouTube rejected the saved pairing (%s)", self._rejections)
                if self._rejections >= 2:
                    self._async_publish(pairing_problem=True)
                    self._entry.async_start_reauth(self._hass)
                    return
                retry = RETRY_INTERVAL
            except Exception as err:  # noqa: BLE001
                # HTTP errors can include Lounge credentials in their URLs.
                self._logger.debug("YouTube connection failed (%s)", type(err).__name__)
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
                try:
                    async with asyncio.timeout(DISCONNECT_TIMEOUT):
                        await self._client.disconnect()
                except Exception as err:  # noqa: BLE001
                    self._logger.debug("YouTube Lounge disconnect failed (%s)", type(err).__name__)
                self._client.reset_playback()

            self._logger.debug("Retrying YouTube Lounge connection in %s s", retry)
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
