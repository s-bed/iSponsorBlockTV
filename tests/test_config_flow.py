"""Test pairing and settings through Home Assistant's flow manager."""

from hashlib import sha256
from unittest.mock import patch

import pytest
from custom_components.isponsorblocktv.core.pairing import (
    PairedScreen,
    PairingRejected,
    PairingUnavailable,
)
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_SSDP, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.ssdp import SsdpServiceInfo
from homeassistant.loader import async_get_ssdp
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.isponsorblocktv import config_flow
from custom_components.isponsorblocktv.const import (
    CONF_DIAL_UDN,
    CONF_SCREEN_ID,
    DIAL_SERVICE,
    DOMAIN,
)
from custom_components.isponsorblocktv.dial import DialScreen

pytestmark = pytest.mark.asyncio
SCREEN_ID = "A" * 64
SECOND_ID = "B" * 64
UDN = "uuid:test-tv"


async def _start_manual(hass, step):
    flow = hass.config_entries.flow
    menu = await flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert menu["menu_options"] == ["tv_code", "existing_screen_id"]
    return await flow.async_configure(menu["flow_id"], {"next_step_id": step})


async def _submit_credential(hass, form, data):
    result = await hass.config_entries.flow.async_configure(form["flow_id"], data)
    if result["type"] is FlowResultType.SHOW_PROGRESS:
        assert result["progress_action"] == "verify"
        await hass.async_block_till_done()
        return await hass.config_entries.flow.async_configure(form["flow_id"])
    return result


async def test_existing_screen_id_keeps_copied_name(hass, enable_custom_integrations):
    form = await _start_manual(hass, "existing_screen_id")
    with (
        patch.object(
            config_flow,
            "async_verify_screen_id",
            return_value=PairedScreen(SCREEN_ID, "YouTube TV", "temporary-token"),
        ),
        patch("custom_components.isponsorblocktv.TVRuntime.async_start"),
    ):
        confirm = await _submit_credential(
            hass, form, {CONF_SCREEN_ID: SCREEN_ID, "name": "Living room"}
        )
        assert confirm["step_id"] == "confirm"
        assert confirm["description_placeholders"] == {"name": "YouTube TV"}
        result = await hass.config_entries.flow.async_configure(confirm["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SCREEN_ID: SCREEN_ID}
    assert result["title"] == "Living room"
    assert result["result"].unique_id == sha256(SCREEN_ID.encode()).hexdigest()


async def test_rejected_screen_id_offers_tv_code_and_requests_name(
    hass, enable_custom_integrations
):
    form = await _start_manual(hass, "existing_screen_id")
    with patch.object(config_flow, "async_verify_screen_id", side_effect=PairingRejected):
        fallback = await _submit_credential(hass, form, {CONF_SCREEN_ID: SCREEN_ID, "name": "Old"})
    assert fallback["type"] is FlowResultType.MENU
    assert "tv_code" in fallback["menu_options"]
    code_form = await hass.config_entries.flow.async_configure(
        fallback["flow_id"], {"next_step_id": "tv_code"}
    )
    with (
        patch.object(
            config_flow,
            "async_pair_with_code",
            return_value=PairedScreen(SECOND_ID, None, "temporary-token"),
        ),
        patch("custom_components.isponsorblocktv.TVRuntime.async_start"),
    ):
        name = await _submit_credential(hass, code_form, {"tv_code": "0123-4567 8901"})
        assert name["step_id"] == "name"
        assert (await hass.config_entries.flow.async_configure(name["flow_id"], {"name": " "}))[
            "errors"
        ] == {"name": "name_required"}
        result = await hass.config_entries.flow.async_configure(name["flow_id"], {"name": "Den"})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_SCREEN_ID: SECOND_ID}
    assert result["title"] == "Den"


@pytest.mark.parametrize(
    ("error", "reason"), [(PairingRejected, "invalid_code"), (PairingUnavailable, "cannot_connect")]
)
async def test_code_failure_can_retry(hass, enable_custom_integrations, error, reason):
    form = await _start_manual(hass, "tv_code")
    with patch.object(config_flow, "async_pair_with_code", side_effect=error):
        result = await _submit_credential(hass, form, {"tv_code": "012345678901"})
    assert result["step_id"] == "tv_code"
    assert result["errors"] == {"base": reason}


async def test_manual_entry_learns_dial_identity_and_blocks_duplicates(
    hass, enable_custom_integrations
):
    assert (await async_get_ssdp(hass))[DOMAIN] == [{"st": DIAL_SERVICE}, {"nt": DIAL_SERVICE}]
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SCREEN_ID: SCREEN_ID})
    entry.add_to_hass(hass)
    form = await _start_manual(hass, "tv_code")
    with patch.object(
        config_flow, "async_pair_with_code", return_value=PairedScreen(SCREEN_ID, "TV", "token")
    ):
        result = await _submit_credential(hass, form, {"tv_code": "012345678901"})
    assert result["reason"] == "already_configured"

    discovery = SsdpServiceInfo(ssdp_usn=UDN, ssdp_st=DIAL_SERVICE, upnp={})
    for screen_id in (SCREEN_ID, SECOND_ID):
        with patch.object(
            config_flow, "async_probe_dial", return_value=DialScreen(screen_id, "TV", UDN)
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_SSDP}, data=discovery
            )
        assert result["reason"] == "already_configured"
    assert entry.data == {CONF_SCREEN_ID: SCREEN_ID, CONF_DIAL_UDN: UDN}


async def test_repair_replaces_id_in_existing_entry(hass, enable_custom_integrations):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Living room",
        data={CONF_SCREEN_ID: SCREEN_ID, CONF_DIAL_UDN: UDN},
        options={"skip_ads": True},
    )
    entry.add_to_hass(hass)
    flow = hass.config_entries.flow
    form = await flow.async_init(
        DOMAIN, context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id}, data=dict(entry.data)
    )
    with patch.object(
        config_flow,
        "async_pair_with_code",
        return_value=PairedScreen(SECOND_ID, "Different name", "token"),
    ):
        confirm = await _submit_credential(hass, form, {"tv_code": "012345678901"})
    assert confirm["step_id"] == "repair_confirm"
    assert entry.data[CONF_SCREEN_ID] == SCREEN_ID

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_configure(confirm["flow_id"], {"same_tv": True})
    assert result["reason"] == "reauth_successful"
    reload.assert_called_once_with(entry.entry_id)
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    assert entry.data == {CONF_SCREEN_ID: SECOND_ID, CONF_DIAL_UDN: UDN}
    assert entry.title == "Living room"
    assert entry.options == {"skip_ads": True}


async def test_settings_allow_empty_categories_and_reload_only_selected_tv(
    hass, enable_custom_integrations
):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_SCREEN_ID: SCREEN_ID})
    other = MockConfigEntry(domain=DOMAIN, data={CONF_SCREEN_ID: SECOND_ID})
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    options = hass.config_entries.options
    form = await options.async_init(entry.entry_id)
    defaults = form["data_schema"]({})
    assert defaults["disturb_short_videos"] is True
    assert defaults["short_video_duration_threshold"] == 30

    invalid = await options.async_configure(
        form["flow_id"], {"short_video_duration_threshold": "nan"}
    )
    assert invalid["errors"] == {
        "short_video_duration_threshold": "invalid_short_video_duration_threshold"
    }
    assert invalid["data_schema"]({})["short_video_duration_threshold"] == 30

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await options.async_configure(
            form["flow_id"],
            {
                "skip_categories": [],
                "mute_ads": True,
                "disturb_short_videos": False,
                "short_video_duration_threshold": 45,
                "advanced": {"sponsorblock_api_url": "https://example.com/api", "use_proxy": True},
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    reload.assert_called_once_with(entry.entry_id)
    assert entry.options["skip_categories"] == []
    assert entry.options["mute_ads"]
    assert entry.options["disturb_short_videos"] is False
    assert entry.options["short_video_duration_threshold"] == 45
    assert entry.options["use_proxy"]
    assert entry.options["sponsorblock_api_url"] == "https://example.com/api/"
    assert other.options == {}
