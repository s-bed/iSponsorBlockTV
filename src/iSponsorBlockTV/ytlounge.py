import asyncio
import hashlib
import json
import logging
import math
import sys
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import uuid4

import pyytlounge
from aiohttp import ClientSession
from pyytlounge.models import DpadCommand, State
from pyytlounge.wrapper import NotLinkedException, api_base, as_aiter

from .constants import youtube_client_blacklist

_LOGGER = logging.getLogger(__name__)
_WIRE_LOGGER = logging.getLogger(f"{__name__}.wire")
_WIRE_LOGGER.addHandler(logging.NullHandler())
_WIRE_LOGGER.propagate = False
_REQUEST_TIMEOUT = 10
_RECONNECT_BACK_WINDOW = 60


@dataclass
class PlaybackState:
    currentTime: float | None = None
    duration: float | None = None
    videoId: str = ""
    state: State = State.Stopped


def _nonnegative_float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


class YtLoungeApi(pyytlounge.YtLoungeApi):
    def __init__(
        self,
        screen_id=None,
        config=None,
        api_helper=None,
        logger=None,
        *,
        web_session: ClientSession | None = None,
        standalone=True,
        on_update=None,
    ):
        self.logger = logger or _LOGGER
        super().__init__(
            config.join_name if config else "iSponsorBlockTV",
            logger=self.logger if standalone else _WIRE_LOGGER,
        )
        self.session = web_session
        self._standalone = standalone
        self._on_update = on_update
        self.auth.screen_id = screen_id
        self.auth.lounge_id_token = None
        # Unique device_id for iSponsorBlockTV, allows other clients to connect simultaneously
        self.auth.device_id = hashlib.md5(f"iSponsorBlockTV{screen_id or ''}".encode()).hexdigest()
        self.api_helper = api_helper
        self.volume_state = {}
        self.playback_speed = None
        self.ad_playing = None
        self.ad_skippable = None
        self._subscription = None
        self._watchdog_task = None
        self._actions = set()
        self._state_callback = None
        self.playback_state = None
        self._shorts_disconnected_at: float | None = None
        self._disturb_short_videos = getattr(config, "disturb_short_videos", False)
        self.auto_play = config.auto_play if config else True
        self.mute_ads = config.mute_ads if config else False
        self.skip_ads = config.skip_ads if config else False
        self._muted_for_ad = False
        self.last_event_time: float | None = None
        self._command_mutex = asyncio.Lock()
        self._ad_mutex = asyncio.Lock()

    @property
    def volume(self) -> int | None:
        try:
            volume = int(self.volume_state.get("volume"))
        except (TypeError, ValueError):
            return None
        return volume if 0 <= volume <= 100 else None

    @property
    def muted(self) -> bool | None:
        value = self.volume_state.get("muted")
        if value in ("true", True):
            return True
        if value in ("false", False):
            return False
        return None

    def reset_playback(self):
        self.playback_state = None
        self.playback_speed = None
        self.volume_state = {}
        self.ad_playing = None
        self.ad_skippable = None
        self._notify_update()

    def reset_session(self) -> None:
        """Discard the local session and playback state."""
        self._connection_lost()
        self.reset_playback()

    def _notify_update(self):
        if self._on_update is not None:
            self._on_update()

    async def _handle_playback_event(self, event_type, data):
        previous = self.playback_state or PlaybackState()
        state_code = None
        try:
            state_code = int(data.get("state"))
            state = State(state_code)
        except (TypeError, ValueError, OverflowError):
            state = State.Stopped
        if state in (State.Playing, State.Stopped, State.Buffering, State.AdSkipped):
            self._set_ad(False, source=event_type)
        elif state is State.Advertisement and not self.ad_playing:
            self._set_ad(True, source=event_type)
        video_id = data.get("videoId", "" if event_type == "nowPlaying" else previous.videoId)
        if not isinstance(video_id, str) or state in (State.Stopped, State.Buffering):
            video_id = ""
        duration = _nonnegative_float(data.get("duration"))
        # Lounge can report an ad's duration with the content's video ID.
        ad_states = (State.Advertisement, State.AdSkipped)
        if (
            duration is None
            and video_id
            and video_id == previous.videoId
            and (state in ad_states) == (previous.state in ad_states)
        ):
            duration = previous.duration
        self.playback_state = PlaybackState(
            currentTime=_nonnegative_float(data.get("currentTime")),
            duration=duration if video_id else None,
            videoId=video_id,
            state=state,
        )
        self.logger.debug(
            "YouTube %s: playback %s -> %s (state code=%s, position=%s s, duration=%s s, "
            "has video=%s, video changed=%s)",
            event_type,
            previous.state.name,
            state.name,
            state_code,
            self.playback_state.currentTime,
            self.playback_state.duration,
            bool(video_id),
            video_id != previous.videoId,
        )
        if self._state_callback:
            await self._state_callback(self.playback_state)

    async def _watchdog(self, started_at: float):
        """Finish a subscription that has stopped receiving events."""
        loop = asyncio.get_running_loop()
        while loop.time() - (self.last_event_time or started_at) <= 60:
            await asyncio.sleep(10)
        self.logger.debug("YouTube Lounge subscription stopped receiving events")

    async def run_subscription(self, callback, *, resume: bool = False) -> bool:
        """Run the event stream and report whether it received events."""
        self._state_callback = callback
        if resume:
            # A partial playback event must not inherit the previous video's ID or duration.
            self.playback_state = None
        elif self.playback_state is not None:
            await callback(self.playback_state)
        self.last_event_time = None
        subscription = self._subscription = asyncio.create_task(super().subscribe())
        watchdog = self._watchdog_task = asyncio.create_task(
            self._watchdog(asyncio.get_running_loop().time())
        )
        if resume:
            self._start_action(self.get_now_playing)
        self.logger.debug("Listening for YouTube Lounge events")
        try:
            done, _ = await asyncio.wait(
                (subscription, watchdog), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
            if subscription in done:
                self.logger.debug(
                    "YouTube Lounge event stream ended (connected=%s)", self.connected()
                )
        finally:
            await self.stop_subscription()
        return self.last_event_time is not None

    async def stop_subscription(self):
        """Cancel and wait for subscription and event-triggered work."""
        self._state_callback = None
        tasks = set(self._actions)
        tasks.update(task for task in (self._subscription, self._watchdog_task) if task)
        self._subscription = self._watchdog_task = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _start_action(self, action, *args):
        task = asyncio.create_task(self._run_action(action, args))
        self._actions.add(task)
        task.add_done_callback(self._actions.discard)

    async def _run_action(self, action, args):
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                await action(*args)
        except Exception as err:  # noqa: BLE001
            # Commands can fail with a credential-bearing URL in the exception.
            self.logger.debug("YouTube Lounge event action failed (%s)", type(err).__name__)

    async def _apply_ad_behavior(self):
        async with self._ad_mutex:
            if not self.ad_playing:
                if self._muted_for_ad and await self.mute(False, override=True):
                    self._muted_for_ad = False
            elif self.skip_ads and self.ad_skippable:
                if (
                    await self.skip_ad()
                    and self._muted_for_ad
                    and await self.mute(False, override=True)
                ):
                    self._muted_for_ad = False
            elif self.mute_ads and self.muted is not True and not self._muted_for_ad:
                # A disconnect may cancel the response after the command has reached the TV.
                self._muted_for_ad = True
                if not await self.mute(True, override=True):
                    self._muted_for_ad = False

    def _set_ad(self, playing, skippable=False, *, source):
        if (playing, skippable) != (self.ad_playing, self.ad_skippable):
            self.logger.debug(
                "YouTube %s: interpreted ad state (playing=%s, skippable=%s)",
                source,
                playing,
                skippable,
            )
        self.ad_playing = playing
        self.ad_skippable = skippable
        if self._muted_for_ad or (playing and (self.mute_ads or self.skip_ads)):
            self._start_action(self._apply_ad_behavior)

    # Process a lounge subscription event
    # skipcq: PY-R1000
    async def _process_event(self, event_type: str, args: list[Any]):
        self.last_event_time = asyncio.get_running_loop().time()
        data = args[0] if args and isinstance(args[0], dict) else {}
        telemetry_changed = True

        if event_type in {"onStateChange", "nowPlaying"}:
            await self._handle_playback_event(event_type, data)
        elif event_type == "onAdStateChange":
            ad_state = str(data["adState"]) if "adState" in data else None
            skip_enabled = data.get("isSkipEnabled") in ("true", True)
            self.logger.debug(
                "YouTube onAdStateChange: ad state code=%s, ad position=%s s, skip enabled=%s",
                _nonnegative_float(ad_state),
                _nonnegative_float(data.get("currentTime")),
                skip_enabled,
            )
            if ad_state == "1082" or (ad_state == "0" and data.get("currentTime") != "0"):
                self._set_ad(False, source=event_type)
            elif ad_state:
                self._set_ad(True, skip_enabled, source=event_type)
        elif event_type == "onVolumeChanged":
            self.volume_state = data
            self.logger.debug("YouTube onVolumeChanged: muted=%s", self.muted)
        elif event_type == "autoplayUpNext":
            if self.api_helper and (video_id := data.get("videoId")):
                self._start_action(self.api_helper.get_segments, video_id)
            telemetry_changed = False
        elif event_type == "adPlaying":
            if self.api_helper and (video_id := data.get("contentVideoId")):
                self._start_action(self.api_helper.get_segments, video_id)
            self._set_ad(True, data.get("isSkipEnabled") in ("true", True), source=event_type)
        elif event_type == "loungeStatus":
            devices = json.loads(data["devices"])
            for device in devices:
                if device["type"] == "LOUNGE_SCREEN":
                    device_info = json.loads(device.get("deviceInfo", "{}")) or {}
                    if device_info.get("clientName", "") in youtube_client_blacklist:
                        self.logger.debug(
                            "YouTube Lounge session cleared for an unsupported client"
                        )
                        self._connection_lost()
                        return
        elif event_type == "onSubtitlesTrackChanged":
            video_id = data.get("videoId")
            pending = self._shorts_disconnected_at is not None
            self.logger.debug(
                "YouTube onSubtitlesTrackChanged: Shorts workaround pending=%s, has video=%s",
                pending,
                bool(video_id),
            )
            if pending and not self._disturb_short_videos:
                self._shorts_disconnected_at = None
                if video_id:
                    self.logger.debug("Shorts workaround is requesting video playback")
                    self._start_action(self.play_video, video_id)
            telemetry_changed = False
        elif event_type == "loungeScreenDisconnected":
            reason = data.get("reason")
            if reason == "disconnectedByUserScreenInitiated":
                self._shorts_disconnected_at = monotonic()
            self.logger.debug(
                "YouTube loungeScreenDisconnected: reason=%r, Shorts workaround pending=%s",
                reason if isinstance(reason, str) else None,
                self._shorts_disconnected_at is not None,
            )
        elif event_type == "onAutoplayModeChanged":
            self._start_action(self.set_auto_play_mode, self.auto_play)
            telemetry_changed = False
        elif event_type == "onPlaybackSpeedChanged":
            self.playback_speed = _nonnegative_float(data.get("playbackSpeed")) or None
            if self.playback_speed is not None:
                self._start_action(self.get_now_playing)
        else:
            telemetry_changed = False

        if event_type in {"loungeStatus", "loungeScreenDisconnected"}:
            await super()._process_event(event_type, args)
        if telemetry_changed:
            self._notify_update()

    # Set the volume to a specific value (0-100)
    async def set_volume(self, volume: int) -> None:
        await self._command("setVolume", {"volume": volume})

    async def mute(self, mute: bool, override: bool = False) -> bool:
        """
        Mute or unmute the device (if the device already
        is in the desired state, nothing happens)

        :param bool mute: True to mute, False to unmute
        :param bool override: If True, the command is sent even if the
        device already is in the desired state

        TODO: Only works if the device is subscribed to the lounge
        """
        if mute:
            mute_str = "true"
        else:
            mute_str = "false"
        if override or self.muted is not mute:
            # YouTube wants the volume when unmuting, so we send it
            return await self._command(
                "setVolume",
                {"volume": self.volume_state.get("volume", 100), "muted": mute_str},
            )
        return True

    async def play_video(self, video_id: str) -> bool:
        return await self._command("setPlaylist", {"videoId": video_id})

    async def get_now_playing(self):
        return await self._command("getNowPlaying")

    def take_reconnect_back(self) -> bool:
        """Consume a recent screen disconnect after a successful connection."""
        if not self._disturb_short_videos or self._shorts_disconnected_at is None:
            return False
        disconnected_at = self._shorts_disconnected_at
        self._shorts_disconnected_at = None
        if monotonic() - disconnected_at > _RECONNECT_BACK_WINDOW:
            self.logger.debug("Screen-initiated disconnect is too old for reconnect Back")
            return False
        return True

    async def press_back(self, *, only_if: Callable[[], bool]) -> bool:
        """Dismiss the player if the pending action is still valid."""
        return await self._command("dpadCommand", {"key": DpadCommand.BACK.value}, only_if=only_if)

    # Test to wrap the command function in a mutex to avoid race conditions with
    # the _command_offset (TODO: move to upstream if it works)
    async def _command(
        self,
        command: str,
        command_parameters: dict | None = None,
        *,
        only_if: Callable[[], bool] | None = None,
    ) -> bool:
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT), self._command_mutex:
                if only_if is not None and not only_if():
                    return False
                self.logger.debug("Sending YouTube Lounge command %s", command)
                acknowledged = await super()._command(command, command_parameters)
        except (Exception, asyncio.CancelledError) as err:
            self.logger.debug("YouTube Lounge command %s ended (%s)", command, type(err).__name__)
            raise
        self.logger.debug("YouTube Lounge command %s acknowledged=%s", command, acknowledged)
        return acknowledged

    def _handle_session_result(self, status_code: int, reason: str) -> bool:
        accepted = super()._handle_session_result(status_code, reason)
        if status_code != 200:
            self.logger.debug("YouTube Lounge session response: HTTP %s", status_code)
        return accepted

    async def is_available(self) -> bool:
        async with asyncio.timeout(_REQUEST_TIMEOUT):
            return await super().is_available()

    async def restore_ad_mute(self) -> None:
        """Restore audio muted by this client while a session is available."""
        if self.connected() and self._muted_for_ad:
            try:
                async with asyncio.timeout(1):
                    if await self.mute(False, override=True):
                        self._muted_for_ad = False
            except Exception as err:  # noqa: BLE001
                self.logger.debug("Could not restore TV volume (%s)", type(err).__name__)

    async def disconnect(self) -> bool:
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                await self.restore_ad_mute()
                if self.connected():
                    self.logger.debug("Sending YouTube Lounge disconnect request")
                    acknowledged = await super().disconnect()
                    self.logger.debug("YouTube Lounge disconnect acknowledged=%s", acknowledged)
                    return acknowledged
                return False
        finally:
            self._connection_lost()

    def _common_connection_parameters(self) -> dict[str, Any]:
        return {
            "name": self.device_name,
            "loungeIdToken": self.auth.lounge_id_token,
            "SID": self._sid,
            "AID": self._last_event_id,
            "gsessionid": self._gsession,
            "device": "REMOTE_CONTROL",
            "app": "ytios-phone-20.15.1",
            "VER": "8",
            "v": "2",
        }

    async def connect(self) -> bool:
        """Attempt to connect using the previously set tokens"""
        if not self.linked():
            raise NotLinkedException("Not linked")

        connect_body = {
            "id": self.auth.device_id,
            "mdx-version": "3",
            "TYPE": "xmlhttp",
            "theme": "cl",
            "sessionSource": "MDX_SESSION_SOURCE_UNKNOWN",
            "connectParams": '{"setStatesParams": "{"playbackSpeed":0}"}',
            "RID": "1",
            "CVER": "1",
            "capabilities": "que,dsdtr,atp,vsp",
            "ui": "false",
            "app": "ytios-phone-20.15.1",
            "pairing_type": "manual",
            "VER": "8",
            "loungeIdToken": self.auth.lounge_id_token,
            "device": "REMOTE_CONTROL",
            "name": self.device_name,
        }
        connect_url = f"{api_base}/bc/bind"
        async with (
            asyncio.timeout(_REQUEST_TIMEOUT),
            self._required_session.post(url=connect_url, data=connect_body) as resp,
        ):
            try:
                text = await resp.text()
                self.logger.debug("YouTube Lounge connection response: HTTP %s", resp.status)
                if resp.status == 401:
                    if self._standalone and "Connection denied" in text:
                        self._logger.warning(
                            "Connection denied, attempting to circumvent the issue"
                        )
                        await self.connect_as_screen()
                    self._lounge_token_expired()
                    return False

                if resp.status != 200:
                    self._logger.warning("Unknown reply to connect %i %s", resp.status, resp.reason)
                    return False
                lines = text.splitlines()
                async for events in self._parse_event_chunks(as_aiter(lines)):
                    await self._process_events(events)
                self._command_offset = 1
                return self.connected()
            except Exception:
                self._logger.exception(
                    "Handle connect failed, status %s reason %s",
                    resp.status,
                    resp.reason,
                )
                raise

    async def connect_as_screen(self) -> bool:
        """Attempt to connect using the previously set tokens"""
        if not self.linked():
            raise NotLinkedException("Not linked")

        connect_body = {
            "id": str(uuid4()),
            "mdx-version": "3",
            "TYPE": "xmlhttp",
            "theme": "cl",
            "sessionSource": "MDX_SESSION_SOURCE_UNKNOWN",
            "connectParams": '{"setStatesParams": "{"playbackSpeed":0}"}',
            "sessionNonce": str(uuid4()),
            "RID": "1",
            "CVER": "1",
            "capabilities": "que,dsdtr,atp,vsp",
            "ui": "false",
            "app": "ytios-phone-20.15.1",
            "pairing_type": "manual",
            "VER": "8",
            "loungeIdToken": self.auth.lounge_id_token,
            "device": "LOUNGE_SCREEN",
            "name": self.device_name,
        }
        connect_url = f"{api_base}/bc/bind"
        async with self.session.post(url=connect_url, data=connect_body) as resp:
            try:
                await resp.text()
                self.logger.error(
                    "Connected as screen: please force close the app on the device for iSponsorBlockTV to work properly"
                )
                self.logger.warning("Exiting in 5 seconds")
                await asyncio.sleep(5)
                sys.exit(0)
            except:
                self._logger.exception(
                    "Handle connect failed, status %s reason %s",
                    resp.status,
                    resp.reason,
                )
                raise
