"""Resume content after the five-second non-skippable ad workaround."""

import asyncio
import hashlib
import math
from time import monotonic

from pyytlounge.models import State
from pyytlounge.wrapper import api_base, as_aiter

_AD_WAIT = 5
_REQUEST_TIMEOUT = 10


class AdReplay:
    """Keep content samples separate from the temporary ad player."""

    def __init__(self, client, offset: float) -> None:
        self._client = client
        self._offset = offset
        self.video_id = ""
        self.duration: float | None = None
        self._position = 0.0
        self._speed = 1.0
        self._sampled_at: float | None = None
        self._ad_position: float | None = None
        self._task = None
        self._pending_seek = None
        self._generation = 0
        self._paused = False

    def reset(self) -> None:
        """Forget content and invalidate delayed commands when the session ends."""
        self.cancel()
        self.video_id = ""
        self.duration = None
        self._position = 0.0
        self._sampled_at = None
        self._ad_position = None

    def cancel(self, *, keep_paused: bool = False) -> None:
        """Cancel replay or a pending seek after navigation or a new ad."""
        self._generation += 1
        self._pending_seek = None
        self._paused = keep_paused and self._paused
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
        self._task = None

    def video_changed(self, video_id: str) -> None:
        if video_id and video_id != self.video_id:
            self.reset()
            self.video_id = video_id

    def looks_like_ad(self, video_id: str, duration: float | None) -> bool:
        return bool(
            video_id
            and video_id == self.video_id
            and self.duration is not None
            and duration is not None
            and 0 < duration < self.duration * 0.5
        )

    def ad_ended(self) -> None:
        """Stop the countdown, preserving a seek already requested by replay."""
        self._ad_position = None
        self._paused = False
        if self._pending_seek is None:
            self.cancel()

    def ad_started(self) -> None:
        """Freeze content position when the first ad signal arrives."""
        self.cancel()
        elapsed = 0.0 if self._sampled_at is None else monotonic() - self._sampled_at
        self._position += elapsed * self._speed
        self._sampled_at = None
        self._ad_position = max(0.0, self._position - self._offset * self._speed)
        if self.duration is not None:
            self._ad_position = min(self._ad_position, self.duration)

    @property
    def restoring_content(self) -> bool:
        return self._pending_seek is not None

    def observe(self, state, *, clear_content: bool = False) -> None:
        """Save content progress and complete a replay only on matching content."""
        if self._client.ad_playing:
            return
        if not state.videoId:
            if clear_content and self._pending_seek is None:
                self.reset()
            return
        self.video_changed(state.videoId)
        if state.currentTime is not None and state.state in (State.Playing, State.Paused):
            self._position = state.currentTime
        if state.state is not State.Playing:
            self._sampled_at = None
            return
        if state.duration is not None and state.duration > 0:
            self.duration = state.duration
        if state.currentTime is not None:
            self._position = state.currentTime
            self._speed = self._client.playback_speed or 1.0
            self._sampled_at = monotonic()
        pending = self._pending_seek
        if pending is None or state.currentTime is None:
            return
        self._pending_seek = None
        video_id, position, generation = pending
        if video_id != state.videoId:
            return
        self._client._start_action(
            self._seek_when_ready,
            video_id,
            position,
            generation,
            abs(state.currentTime - position) > 1,
        )

    def start(self, ad_position: float) -> None:
        """Pause one non-skippable ad and schedule its content reload."""
        if (
            not self.video_id
            or self._pending_seek is not None
            or (self._task is not None and not self._task.done())
        ):
            return
        self.cancel()
        position = self._ad_position or 0.0
        wait = max(0.0, _AD_WAIT - ad_position - self._offset)
        task = asyncio.create_task(self._run(self.video_id, position, wait, self._generation))
        self._task = task
        self._client._actions.add(task)
        task.add_done_callback(self._client._actions.discard)

    def _same_ad(self, video_id: str, generation: int) -> bool:
        return (
            self._generation == generation
            and self.video_id == video_id
            and self._client.connected()
            and self._client.ad_playing
            and not self._client.ad_skippable
            and self._client.skip_ads
        )

    async def _run(self, video_id: str, position: float, wait: float, generation: int) -> None:
        toast = None
        paused = loaded = False
        try:
            paused = await self._client._command(
                "pause", only_if=lambda: self._same_ad(video_id, generation)
            )
            if not paused:
                return
            self._paused = True
            if wait > 0:
                toast = asyncio.create_task(
                    self._send_toast(f"Ad will be skipped in {math.ceil(wait)} seconds")
                )
            self._client.logger.debug(
                "Pausing non-skippable YouTube ad (resume position=%.3f s, wait=%.3f s)",
                position,
                wait,
            )
            await asyncio.sleep(wait)
            if not self._same_ad(video_id, generation):
                return
            attempt = self._client._ad_skip_attempt
            self._pending_seek = (video_id, position, generation)
            loaded = await self._client._command(
                "setPlaylist",
                {"videoId": video_id, "currentTime": position},
                only_if=lambda: self._same_ad(video_id, generation),
            )
            if not loaded:
                self._pending_seek = None
            else:
                self._paused = False
                self._client._record_ad_skip(attempt)
                self._client.logger.debug("Reloaded content after non-skippable YouTube ad")
        except Exception as err:  # noqa: BLE001
            self._pending_seek = None
            self._client.logger.debug("YouTube ad replay failed (%s)", type(err).__name__)
        finally:
            if (
                paused
                and not loaded
                and not asyncio.current_task().cancelling()
                and self._same_ad(video_id, generation)
            ):
                try:
                    await self._client._command(
                        "play", only_if=lambda: self._same_ad(video_id, generation)
                    )
                except Exception as err:  # noqa: BLE001
                    self._client.logger.debug("YouTube ad resume failed (%s)", type(err).__name__)
            if toast is not None:
                if not toast.done():
                    toast.cancel()
                await asyncio.gather(toast, return_exceptions=True)
            if self._task is asyncio.current_task():
                self._task = None

    async def resume_paused_ad(self) -> None:
        """Release our pause if a skip request fails."""
        video_id, generation = self.video_id, self._generation

        def still_paused_ad():
            return (
                self._paused
                and self._generation == generation
                and self.video_id == video_id
                and self._client.connected()
                and self._client.ad_playing
            )

        if await self._client._command("play", only_if=still_paused_ad):
            self._paused = False

    async def _seek_when_ready(
        self, video_id: str, position: float, generation: int, seek: bool
    ) -> None:
        def still_content() -> bool:
            state = self._client.playback_state
            return bool(
                self._generation == generation
                and self._client.connected()
                and not self._client.ad_playing
                and state
                and state.state is State.Playing
                and state.videoId == video_id
            )

        async with self._client._ad_mutex:
            commands = [("seekTo", {"newTime": position})] if seek else []
            if self._client._muted_for_ad:
                commands.append(
                    (
                        "setVolume",
                        {"volume": self._client.volume_state.get("volume", 100), "muted": "false"},
                    )
                )
            if commands and await self._client._commands(commands, only_if=still_content):
                if still_content() and commands[-1][0] == "setVolume":
                    self._client._muted_for_ad = False
                self._client.logger.debug("Restored content position after YouTube ad replay")

    async def _send_toast(self, message: str) -> None:
        """Use a temporary remote, keeping its credentials out of the main session."""
        client = self._client
        token = client.auth.lounge_id_token
        body = {
            "id": hashlib.md5(
                f"iSponsorBlockTV-toast{client.auth.screen_id or ''}".encode()
            ).hexdigest(),
            "name": message,
            "device": "REMOTE_CONTROL",
            "mdx-version": "3",
            "TYPE": "xmlhttp",
            "theme": "cl",
            "sessionSource": "MDX_SESSION_SOURCE_UNKNOWN",
            "RID": "1",
            "CVER": "1",
            "capabilities": "que,atp,vsp,ndt",
            "ui": "true",
            "app": "ytios-phone-20.15.1",
            "pairing_type": "manual",
            "VER": "8",
            "loungeIdToken": token,
        }
        sid = gsession = last_event_id = None
        try:
            async with asyncio.timeout(_REQUEST_TIMEOUT):
                async with client._required_session.post(
                    url=f"{api_base}/bc/bind", data=body
                ) as response:
                    response.raise_for_status()
                    text = await response.text()
                async for events in client._parse_event_chunks(as_aiter(text.splitlines())):
                    for event_id, (event_type, *args) in events:
                        last_event_id = event_id
                        if event_type == "c":
                            sid = args[0]
                        elif event_type == "S":
                            gsession = args[0]
        except Exception as err:  # noqa: BLE001
            client.logger.debug("YouTube ad countdown toast failed (%s)", type(err).__name__)
        finally:
            if sid is not None:
                params = {
                    **client._common_connection_parameters(),
                    "app": body["app"],
                    "loungeIdToken": token,
                    "name": message,
                    "SID": sid,
                    "AID": last_event_id,
                    "gsessionid": gsession,
                    "CVER": "1",
                    "RID": "2",
                    "auth_failure_option": "send_error",
                }
                try:
                    async with asyncio.timeout(_REQUEST_TIMEOUT):
                        async with client._required_session.post(
                            url=f"{api_base}/bc/bind",
                            data={
                                "ui": "true",
                                "TYPE": "terminate",
                                "clientDisconnectReason": "MDX_SESSION_DISCONNECT_REASON_DISCONNECTED_BY_USER",
                            },
                            params=params,
                        ) as response:
                            response.raise_for_status()
                            await response.text()
                except Exception as err:  # noqa: BLE001
                    client.logger.debug("YouTube ad toast cleanup failed (%s)", type(err).__name__)
