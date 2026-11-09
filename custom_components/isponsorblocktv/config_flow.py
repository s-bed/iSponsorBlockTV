"""Pair YouTube TVs and configure their skipping behavior in Home Assistant."""

import asyncio
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from hashlib import sha256
from math import isfinite
from typing import Any

import probatio
from aiohttp import ClientSession
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from yarl import URL

from .channel_lookup import (
    ChannelApiKeyRejected,
    ChannelLookupError,
    ChannelSearchResult,
    async_channel_titles,
    async_search_channels,
    async_validate_api_key,
    is_channel_id,
)
from .channel_settings import (
    ChannelSettingsSaveError,
    async_get_channel_settings,
    async_set_api_key,
    async_update_global_blocklist,
)
from .const import (
    CONF_DIAL_UDN,
    CONF_SCREEN_ID,
    DEFAULT_OPTIONS,
    DIAL_SERVICE,
    DOMAIN,
    GLOBAL_SETTINGS_ID,
)
from .core.constants import skip_categories
from .core.pairing import (
    PairedScreen,
    PairingRejected,
    PairingUnavailable,
    async_pair_with_code,
    async_verify_screen_id,
)
from .dial import DialScreen, async_probe_dial, dial_udn

_CONF_TV_CODE = "tv_code"
_CONF_SAME_TV = "same_tv"
_ADVANCED = "advanced"
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off"))
_CATEGORY_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[SelectOptionDict(value=key, label=label) for label, key in skip_categories],
        multiple=True,
    )
)


def _screen_unique_id(screen_id: str) -> str:
    return sha256(screen_id.encode()).hexdigest()


class ISponsorBlockTVConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure YouTube TVs and the shared channel settings entry."""

    VERSION = 1

    def __init__(self) -> None:
        self._repair_entry: ConfigEntry | None = None
        self._dial: DialScreen | None = None
        self._screen_id: str | None = None
        self._reported_name: str | None = None
        self._preferred_name: str | None = None
        self._verification: asyncio.Task[tuple[PairedScreen | None, str | None]] | None = None
        self._verification_step = ""
        self._verification_error: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlowWithReload:
        """Return the options flow for a TV or the shared settings entry."""
        return ISponsorBlockTVOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Offer manual pairing or restore a missing settings entry."""
        menu = ["tv_code", "existing_screen_id"]
        if GLOBAL_SETTINGS_ID not in self._async_current_ids():
            menu.append("global_settings")
        return self.async_show_menu(step_id="user", menu_options=menu)

    async def async_step_global_settings(self, _user_input=None) -> ConfigFlowResult:
        """Create the single integration-wide settings entry."""
        await self.async_set_unique_id(GLOBAL_SETTINGS_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Global channel settings", data={})

    async def async_step_import(self, _user_input=None) -> ConfigFlowResult:
        """Add the settings entry when an existing TV loads."""
        return await self.async_step_global_settings()

    async def async_step_tv_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the short-lived YouTube TV code."""
        if user_input is not None:
            return self._start_verification("tv_code", code=user_input[_CONF_TV_CODE])
        return self.async_show_form(
            step_id="tv_code",
            data_schema=probatio.Schema({probatio.Required(_CONF_TV_CODE): _PASSWORD}),
            errors=self._take_verification_errors(),
            last_step=False,
        )

    async def async_step_existing_screen_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a screen ID from an existing iSponsorBlockTV configuration."""
        if user_input is not None:
            self._preferred_name = user_input.get(CONF_NAME, "").strip() or None
            return self._start_verification(
                "existing_screen_id", screen_id=user_input[CONF_SCREEN_ID]
            )
        return self.async_show_form(
            step_id="existing_screen_id",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_SCREEN_ID): _PASSWORD, probatio.Optional(CONF_NAME): str}
            ),
            errors=self._take_verification_errors(),
            last_step=False,
        )

    async def async_step_screen_id_rejected(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer fresh TV-code pairing when the screen ID is rejected."""
        self._dial = None
        self._preferred_name = None
        await self.async_set_unique_id(None)
        return self.async_show_menu(
            step_id="screen_id_rejected", menu_options=["tv_code", "existing_screen_id"]
        )

    async def async_step_ssdp(self, discovery_info: SsdpServiceInfo) -> ConfigFlowResult:
        """Find a YouTube screen on a local DIAL device."""
        dial = await async_probe_dial(async_get_clientsession(self.hass), discovery_info)
        if dial is None:
            return self.async_abort(reason="no_youtube_screen")

        self._dial = dial
        self._screen_id = dial.screen_id
        await self.async_set_unique_id(_screen_unique_id(dial.screen_id))
        if existing := self._find_duplicate(dial.screen_id, dial.udn):
            if (
                dial.udn
                and existing.data.get(CONF_SCREEN_ID) == dial.screen_id
                and CONF_DIAL_UDN not in existing.data
            ):
                self.hass.config_entries.async_update_entry(
                    existing, data=existing.data | {CONF_DIAL_UDN: dial.udn}
                )
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured()

        self.context["title_placeholders"] = {"name": dial.name or "YouTube on TV"}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask before using the discovered screen ID."""
        assert self._dial is not None
        if user_input is not None:
            return self._start_verification("discovery_confirm", screen_id=self._dial.screen_id)
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"name": self._dial.name or "YouTube on TV"},
            errors=self._take_verification_errors(),
            last_step=False,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start a repair for a rejected credential."""
        self._repair_entry = self._get_reauth_entry()
        return await self.async_step_tv_code()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow re-pairing from the integration entry."""
        self._repair_entry = self._get_reconfigure_entry()
        if self._repair_entry.unique_id == GLOBAL_SETTINGS_ID:
            return self.async_abort(reason="global_settings_use_configure")
        return await self.async_step_tv_code()

    @callback
    def _start_verification(
        self, step: str, *, code: str | None = None, screen_id: str | None = None
    ) -> ConfigFlowResult:
        self._verification_step = step
        self._verification = self.hass.async_create_task(
            self._async_verify_credentials(code, screen_id),
            "Verify YouTube TV pairing",
            eager_start=False,
        )
        return self._show_verification_progress()

    @callback
    def _show_verification_progress(self) -> ConfigFlowResult:
        assert self._verification is not None
        return self.async_show_progress(
            step_id="verify", progress_action="verify", progress_task=self._verification
        )

    async def _async_verify_credentials(
        self, code: str | None, screen_id: str | None
    ) -> tuple[PairedScreen | None, str | None]:
        use_proxy = self._repair_entry is not None and self._repair_entry.options.get("use_proxy")
        session = (
            async_create_clientsession(self.hass, auto_cleanup=False, trust_env=True)
            if use_proxy
            else async_get_clientsession(self.hass)
        )
        try:
            if code is not None:
                return await async_pair_with_code(session, code), None
            assert screen_id is not None
            return await async_verify_screen_id(session, screen_id), None
        except PairingRejected:
            return None, "invalid_code" if code is not None else "invalid_screen_id"
        except PairingUnavailable:
            return None, "cannot_connect"
        finally:
            if use_proxy:
                session.detach()

    async def async_step_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show progress until YouTube finishes checking the credential."""
        assert self._verification is not None
        if not self._verification.done():
            return self._show_verification_progress()

        screen, error = self._verification.result()
        self._verification = None
        if error == "invalid_screen_id":
            return self.async_show_progress_done(next_step_id="screen_id_rejected")
        if screen is None:
            self._verification_error = error
            return self.async_show_progress_done(next_step_id=self._verification_step)

        self._screen_id = screen.screen_id
        self._reported_name = screen.name or (self._dial.name if self._dial else None)
        return self.async_show_progress_done(
            next_step_id="repair_confirm" if self._repair_entry is not None else "confirm"
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the verified TV and require a name if it did not supply one."""
        assert self._screen_id is not None
        udn = self._dial.udn if self._dial else None
        await self.async_set_unique_id(_screen_unique_id(self._screen_id))
        if self._find_duplicate(self._screen_id, udn):
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured()

        name = self._reported_name or self._preferred_name
        if name is None:
            return await self.async_step_name()
        if user_input is None:
            return self.async_show_form(step_id="confirm", description_placeholders={"name": name})
        return self._create_tv_entry(self._preferred_name or name)

    async def async_step_name(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Name a matched TV that did not report a name."""
        errors = {}
        if user_input is not None:
            if name := user_input[CONF_NAME].strip():
                return self._create_tv_entry(name)
            errors[CONF_NAME] = "name_required"
        return self.async_show_form(
            step_id="name",
            data_schema=probatio.Schema({probatio.Required(CONF_NAME): str}),
            errors=errors,
        )

    @callback
    def _create_tv_entry(self, name: str) -> ConfigFlowResult:
        assert self._screen_id is not None
        udn = self._dial.udn if self._dial else None
        if self._find_duplicate(self._screen_id, udn):
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured()
        data = {CONF_SCREEN_ID: self._screen_id}
        if udn:
            data[CONF_DIAL_UDN] = udn
        return self.async_create_entry(title=name, data=data)

    async def async_step_repair_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify the original TV before replacing its credential."""
        assert self._screen_id is not None and self._repair_entry is not None
        entry = self._repair_entry
        if user_input is None and (
            entry.data[CONF_SCREEN_ID] == self._screen_id or await self._async_matches_original_tv()
        ):
            return self._finish_repair()
        if user_input is not None and user_input[_CONF_SAME_TV]:
            return self._finish_repair()
        return self.async_show_form(
            step_id="repair_confirm",
            data_schema=probatio.Schema(
                {probatio.Required(_CONF_SAME_TV, default=False): BooleanSelector()}
            ),
            description_placeholders={"name": entry.title},
            errors={_CONF_SAME_TV: "confirm_original_tv"} if user_input is not None else {},
        )

    async def _async_matches_original_tv(self) -> bool:
        assert self._repair_entry is not None
        udn = self._repair_entry.data.get(CONF_DIAL_UDN)
        if not udn or "ssdp" not in self.hass.config.components:
            return False

        # The scanner is optional, and may not be installed for manual setup.
        from homeassistant.components import ssdp

        discovery_info = next(
            (
                discovery
                for discovery in await ssdp.async_get_discovery_info_by_st(self.hass, DIAL_SERVICE)
                if dial_udn(discovery) == udn
            ),
            None,
        )
        if discovery_info is None:
            return False
        screen = await async_probe_dial(async_get_clientsession(self.hass), discovery_info)
        return screen is not None and screen.udn == udn and screen.screen_id == self._screen_id

    @callback
    def _finish_repair(self) -> ConfigFlowResult:
        assert self._screen_id is not None and self._repair_entry is not None
        if self._find_duplicate(self._screen_id):
            return self.async_abort(reason="already_configured")
        return self.async_update_reload_and_abort(
            self._repair_entry,
            unique_id=_screen_unique_id(self._screen_id),
            data_updates={CONF_SCREEN_ID: self._screen_id},
        )

    @callback
    def _find_duplicate(self, screen_id: str, udn: str | None = None) -> ConfigEntry | None:
        return next(
            (
                entry
                for entry in self._async_current_entries(include_ignore=False)
                if entry is not self._repair_entry
                and (
                    entry.data.get(CONF_SCREEN_ID) == screen_id
                    or (udn is not None and entry.data.get(CONF_DIAL_UDN) == udn)
                )
            ),
            None,
        )

    def _take_verification_errors(self) -> dict[str, str]:
        error, self._verification_error = self._verification_error, None
        return {"base": error} if error else {}


def _sponsorblock_url(raw_url: str) -> str:
    """Return an HTTP(S) API base URL with a trailing slash."""
    url = URL(raw_url.strip())
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.raw_user is not None
        or url.raw_password is not None
        or url.query_string
        or url.fragment
        or url.port is None
        or not 1 <= url.port <= 65535
    ):
        raise ValueError("Invalid SponsorBlock URL")
    return str(url.with_path(url.raw_path.rstrip("/") + "/", encoded=True))


class ISponsorBlockTVOptionsFlow(OptionsFlowWithReload):
    """Edit shared channel settings or playback options for one TV."""

    def __init__(self) -> None:
        self._channel_matches: tuple[ChannelSearchResult, ...] = ()

    @property
    def _is_global(self) -> bool:
        return self.config_entry.unique_id == GLOBAL_SETTINGS_ID

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._is_global:
            return await self.async_step_global_channels()
        return await self.async_step_tv_settings()

    async def async_step_tv_settings(self, _user_input=None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="tv_settings", menu_options=["playback", "tv_blocklist"]
        )

    async def async_step_global_channels(self, _user_input=None) -> ConfigFlowResult:
        return await self._async_show_blocklist_menu("global_channels", "global_api_key")

    async def async_step_playback(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit this TV's settings."""
        options = DEFAULT_OPTIONS | self.config_entry.options
        errors = {}
        if user_input is not None:
            previous_threshold = options["short_video_duration_threshold"]
            previous_whitelist = options["channel_whitelist"]
            options.update({key: value for key, value in user_input.items() if key != _ADVANCED})
            options.update(user_input.get(_ADVANCED, {}))
            if not isfinite(options["short_video_duration_threshold"]):
                errors["short_video_duration_threshold"] = "invalid_short_video_duration_threshold"
                options["short_video_duration_threshold"] = previous_threshold
            options["channel_whitelist"] = list(
                dict.fromkeys(
                    channel.strip() for channel in options["channel_whitelist"] if channel.strip()
                )
            )
            options["join_name"] = options["join_name"].strip() or DEFAULT_OPTIONS["join_name"]
            if options["channel_whitelist"] and options["channel_whitelist"] != previous_whitelist:
                settings = await async_get_channel_settings(self.hass)
                if not settings.key_conflict:
                    options.pop("apikey", None)
                if not settings.api_key:
                    errors["base"] = "api_key_required"
            try:
                options["sponsorblock_api_url"] = _sponsorblock_url(options["sponsorblock_api_url"])
            except ValueError:
                errors["base"] = "invalid_sponsorblock_url"
            if not errors:
                return self.async_create_entry(data=options)

        return self.async_show_form(
            step_id="playback", data_schema=self._schema(options), errors=errors
        )

    async def async_step_tv_blocklist_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = dict(self.config_entry.options)
            options["override_channel_blocklist"] = user_input["override_channel_blocklist"]
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="tv_blocklist_mode",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        "override_channel_blocklist",
                        default=self.config_entry.options.get("override_channel_blocklist", False),
                    ): BooleanSelector()
                }
            ),
        )

    async def async_step_global_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        settings = await async_get_channel_settings(self.hass)
        errors = {"base": "legacy_api_key_conflict"} if settings.key_conflict else {}
        if user_input is not None:
            api_key = user_input.get("api_key", "").strip()
            clear = user_input.get("clear_api_key", False)
            if bool(api_key) == clear:
                errors["base"] = "provide_key_or_clear"
            else:
                try:
                    if api_key:
                        with self._channel_session() as session:
                            await async_validate_api_key(session, api_key)
                    await async_set_api_key(self.hass, api_key)
                except ChannelApiKeyRejected:
                    errors["base"] = "api_key_rejected"
                except ChannelLookupError:
                    errors["base"] = "channel_lookup_failed"
                except ChannelSettingsSaveError:
                    errors["base"] = "channel_settings_save_failed"
                else:
                    return self.async_create_entry(title="", data=None)

        return self.async_show_form(
            step_id="global_api_key",
            data_schema=probatio.Schema(
                {
                    probatio.Optional("api_key"): _PASSWORD,
                    probatio.Optional("clear_api_key", default=False): BooleanSelector(),
                }
            ),
            description_placeholders={"key_status": "✓" if settings.api_key else "—"},
            errors=errors,
        )

    async def async_step_tv_blocklist(self, _user_input=None) -> ConfigFlowResult:
        return await self._async_show_blocklist_menu("tv_blocklist", "tv_blocklist_mode")

    async def _async_show_blocklist_menu(
        self, step_id: str, settings_step: str
    ) -> ConfigFlowResult:
        blocklist = await self._async_get_blocklist()
        menu = [settings_step, "channel_search", "channel_add_id"]
        if blocklist:
            menu.append("channel_remove")
        return self.async_show_menu(
            step_id=step_id,
            menu_options=menu,
            description_placeholders={"count": str(len(blocklist))},
        )

    async def async_step_channel_search(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        settings = await async_get_channel_settings(self.hass)
        errors = {}
        query = user_input["query"].strip() if user_input is not None else ""
        if not settings.api_key:
            errors["base"] = "api_key_required_for_channel_search"
        elif user_input is not None:
            if not query:
                errors["query"] = "channel_query_required"
            else:
                try:
                    with self._channel_session() as session:
                        self._channel_matches = await async_search_channels(
                            session, settings.api_key, query
                        )
                except ChannelLookupError:
                    errors["base"] = "channel_search_failed"
                else:
                    if self._channel_matches:
                        return await self.async_step_channel_select()
                    errors["query"] = "channel_not_found"

        return self.async_show_form(
            step_id="channel_search",
            data_schema=probatio.Schema(
                {probatio.Required("query", default=query): TextSelector()}
            ),
            errors=errors,
        )

    async def async_step_channel_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            channel_ids = user_input["channel_ids"]
            if not channel_ids:
                errors["channel_ids"] = "select_channel_to_add"
            else:
                try:
                    return await self._async_save_blocklist(add=channel_ids)
                except ChannelSettingsSaveError:
                    errors["base"] = "channel_settings_save_failed"

        return self.async_show_form(
            step_id="channel_select",
            data_schema=probatio.Schema(
                {
                    probatio.Required("channel_ids"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=match.channel_id, label=match.label)
                                for match in self._channel_matches
                            ],
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_channel_add_id(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        channel_id = user_input["channel_id"].strip() if user_input is not None else ""
        if user_input is not None:
            if not is_channel_id(channel_id):
                errors["channel_id"] = "invalid_channel_id"
            elif channel_id in await self._async_get_blocklist():
                errors["channel_id"] = "channel_already_blocked"
            else:
                try:
                    return await self._async_save_blocklist(add=(channel_id,))
                except ChannelSettingsSaveError:
                    errors["base"] = "channel_settings_save_failed"

        return self.async_show_form(
            step_id="channel_add_id",
            data_schema=probatio.Schema(
                {probatio.Required("channel_id", default=channel_id): TextSelector()}
            ),
            errors=errors,
        )

    async def async_step_channel_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        blocklist = await self._async_get_blocklist()
        errors = {}
        if user_input is not None:
            if not user_input["channel_ids"]:
                errors["channel_ids"] = "select_channel_to_remove"
            else:
                try:
                    return await self._async_save_blocklist(remove=user_input["channel_ids"])
                except ChannelSettingsSaveError:
                    errors["base"] = "channel_settings_save_failed"

        titles = {}
        if blocklist and (settings := await async_get_channel_settings(self.hass)).api_key:
            with self._channel_session() as session:
                titles = await async_channel_titles(session, settings.api_key, blocklist)
        return self.async_show_form(
            step_id="channel_remove",
            data_schema=probatio.Schema(
                {
                    probatio.Required("channel_ids"): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    value=channel_id,
                                    label=f"{titles[channel_id]} ({channel_id})"
                                    if channel_id in titles
                                    else channel_id,
                                )
                                for channel_id in blocklist
                            ],
                            multiple=True,
                        )
                    )
                }
            ),
            errors=errors,
        )

    @contextmanager
    def _channel_session(self) -> Iterator[ClientSession]:
        if not self.config_entry.options.get("use_proxy"):
            yield async_get_clientsession(self.hass)
            return
        session = async_create_clientsession(self.hass, auto_cleanup=False, trust_env=True)
        try:
            yield session
        finally:
            session.detach()

    async def _async_get_blocklist(self) -> Collection[str]:
        if self._is_global:
            return (await async_get_channel_settings(self.hass)).blocklist
        return self.config_entry.options.get("channel_blocklist", ())

    async def _async_save_blocklist(
        self, *, add: Collection[str] = (), remove: Collection[str] = ()
    ) -> ConfigFlowResult:
        if self._is_global:
            await async_update_global_blocklist(self.hass, add=add, remove=remove)
            return self.async_create_entry(title="", data=None)

        options = dict(self.config_entry.options)
        remaining = (
            channel for channel in options.get("channel_blocklist", ()) if channel not in remove
        )
        options["channel_blocklist"] = list(dict.fromkeys((*remaining, *add)))
        return self.async_create_entry(title="", data=options)

    @staticmethod
    def _schema(options: dict[str, Any]) -> probatio.Schema:
        return probatio.Schema(
            {
                probatio.Required(
                    "skip_categories", default=options["skip_categories"]
                ): _CATEGORY_SELECTOR,
                probatio.Required(
                    "minimum_skip_length", default=options["minimum_skip_length"]
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0, step=0.1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                    )
                ),
                probatio.Required("offset", default=options["offset"]): NumberSelector(
                    NumberSelectorConfig(
                        step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="ms"
                    )
                ),
                probatio.Required("mute_ads", default=options["mute_ads"]): BooleanSelector(),
                probatio.Required("skip_ads", default=options["skip_ads"]): BooleanSelector(),
                probatio.Required(
                    "disturb_short_videos", default=options["disturb_short_videos"]
                ): BooleanSelector(),
                probatio.Required(
                    "short_video_duration_threshold",
                    default=options["short_video_duration_threshold"],
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="s"
                    )
                ),
                probatio.Required("auto_play", default=options["auto_play"]): BooleanSelector(),
                probatio.Required(
                    "skip_count_tracking", default=options["skip_count_tracking"]
                ): BooleanSelector(),
                probatio.Optional(_ADVANCED): section(
                    probatio.Schema(
                        {
                            probatio.Required("join_name", default=options["join_name"]): str,
                            probatio.Required(
                                "sponsorblock_api_url", default=options["sponsorblock_api_url"]
                            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                            probatio.Optional(
                                "channel_whitelist", default=options["channel_whitelist"]
                            ): TextSelector(TextSelectorConfig(multiple=True)),
                            probatio.Required(
                                "use_proxy", default=options["use_proxy"]
                            ): BooleanSelector(),
                        }
                    ),
                    {"collapsed": True},
                ),
            }
        )
