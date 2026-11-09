"""Test the push sensors exposed by a paired YouTube TV."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import PERCENTAGE, STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.isponsorblocktv import binary_sensor, runtime, sensor
from custom_components.isponsorblocktv.const import CONF_SCREEN_ID, DOMAIN
from custom_components.isponsorblocktv.runtime import TVSnapshot


@pytest.fixture
def entry() -> MockConfigEntry:
    """Return a TV entry and sample telemetry from its runtime."""
    entry = MockConfigEntry(domain=DOMAIN, title="Living Room")
    entry.runtime_data = SimpleNamespace(
        snapshot=TVSnapshot(
            connected=True,
            pairing_problem=False,
            playback_state="playing",
            skip_count=3,
            last_skip=datetime(2026, 11, 8, tzinfo=UTC),
            last_short_disruption=datetime(2026, 11, 9, tzinfo=UTC),
            video_id="dQw4w9WgXcQ",
            duration=213.5,
            playback_speed=1.25,
            volume=42,
            ad_playing=True,
            ad_skippable=True,
            muted=False,
        ),
        async_subscribe=Mock(),
    )
    return entry


@pytest.mark.asyncio
async def test_sensors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Publish telemetry and preserve activity when the app closes."""
    entities = []
    await sensor.async_setup_entry(hass, entry, entities.extend)
    sensors = {entity.entity_description.key: entity for entity in entities}

    assert {key: entity.native_value for key, entity in sensors.items()} == {
        "playback_state": "playing",
        "skip_count": 3,
        "last_skip": entry.runtime_data.snapshot.last_skip,
        "last_short_disruption": entry.runtime_data.snapshot.last_short_disruption,
        "video_id": "dQw4w9WgXcQ",
        "duration": 213.5,
        "playback_speed": 1.25,
        "volume": 42,
    }
    assert sensors["playback_state"].device_class == SensorDeviceClass.ENUM
    assert sensors["last_skip"].device_class == SensorDeviceClass.TIMESTAMP
    assert sensors["last_short_disruption"].device_class == SensorDeviceClass.TIMESTAMP
    assert sensors["duration"].native_unit_of_measurement == UnitOfTime.SECONDS
    assert sensors["volume"].native_unit_of_measurement == PERCENTAGE
    assert {key for key, entity in sensors.items() if entity.entity_registry_enabled_default} == {
        "playback_state",
        "skip_count",
        "last_skip",
        "last_short_disruption",
    }

    entry.runtime_data.snapshot = replace(entry.runtime_data.snapshot, connected=False)
    assert {key for key, entity in sensors.items() if entity.available} == {
        "skip_count",
        "last_skip",
        "last_short_disruption",
    }


@pytest.mark.asyncio
async def test_binary_sensors(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Keep connection loss distinct from a confirmed pairing problem."""
    entities = []
    await binary_sensor.async_setup_entry(hass, entry, entities.extend)
    sensors = {entity.entity_description.key: entity for entity in entities}

    assert {key: entity.is_on for key, entity in sensors.items()} == {
        "youtube_connected": True,
        "pairing_problem": False,
        "ad_playing": True,
        "ad_skippable": True,
        "muted": False,
    }
    assert sensors["youtube_connected"].device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert sensors["pairing_problem"].device_class == BinarySensorDeviceClass.PROBLEM
    assert {
        key for key, entity in sensors.items() if not entity.entity_registry_enabled_default
    } == {"muted"}

    entry.runtime_data.snapshot = replace(entry.runtime_data.snapshot, connected=False)
    assert {key for key, entity in sensors.items() if entity.available} == {
        "youtube_connected",
        "pairing_problem",
    }
    assert sensors["youtube_connected"].is_on is False
    assert sensors["pairing_problem"].is_on is False

    entry.runtime_data.snapshot = replace(entry.runtime_data.snapshot, pairing_problem=True)
    assert sensors["pairing_problem"].is_on is True


@pytest.mark.asyncio
async def test_entity_registration_and_updates(entry: MockConfigEntry) -> None:
    """Anchor device and entity IDs to the entry and unsubscribe on removal."""
    entity = sensor.ISponsorBlockTVSensor(entry, sensor.SENSORS[0])
    assert entity.unique_id == f"{entry.entry_id}_playback_state"
    assert entity.device_info["identifiers"] == {(DOMAIN, entry.entry_id)}
    assert entity.device_info["name"] == "Living Room"
    assert not entity.should_poll

    unsubscribe = Mock()
    entry.runtime_data.async_subscribe.return_value = unsubscribe
    with (
        patch.object(entity, "async_write_ha_state") as write_state,
        patch.object(entity, "async_on_remove") as on_remove,
    ):
        await entity.async_added_to_hass()

    entry.runtime_data.async_subscribe.assert_called_once_with(write_state)
    on_remove.assert_called_once_with(unsubscribe)


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_packaged_entry_registers_device_and_entities(hass: HomeAssistant) -> None:
    """Load and unload the packaged integration while the paired TV is offline."""
    screen_id = "example-screen-id"
    entry = MockConfigEntry(domain=DOMAIN, title="Living Room", data={CONF_SCREEN_ID: screen_id})
    entry.add_to_hass(hass)
    checked = asyncio.Event()

    async def is_offline() -> bool:
        checked.set()
        return False

    with (
        patch.object(
            runtime,
            "async_verify_screen_id",
            return_value=SimpleNamespace(lounge_token="example-token"),
        ),
        patch.object(runtime.YtLoungeApi, "is_available", side_effect=is_offline),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(checked.wait(), timeout=2)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        (device,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        assert device.identifiers == {(DOMAIN, entry.entry_id)}
        assert device.name == "Living Room"
        registered = {
            entity.unique_id.removeprefix(f"{entry.entry_id}_"): entity
            for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        }
        assert len(registered) == 13
        assert all(entity.device_id == device.id for entity in registered.values())
        assert {key for key, entity in registered.items() if entity.disabled} == {
            "video_id",
            "duration",
            "playback_speed",
            "volume",
            "muted",
        }
        connection = registered["youtube_connected"].entity_id
        assert hass.states.get(connection).state == STATE_OFF
        assert hass.states.get(registered["pairing_problem"].entity_id).state == STATE_OFF
        assert hass.states.get(registered["ad_playing"].entity_id).state == STATE_UNAVAILABLE
        assert hass.states.get(registered["skip_count"].entity_id).state == "0"
        assert hass.states.get(registered["last_short_disruption"].entity_id).state == STATE_UNKNOWN

        assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(connection).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.asyncio
async def test_playback_activity_survives_reload_per_tv(hass: HomeAssistant) -> None:
    """Restore playback activity and carry forward previously saved skips."""
    entry = MockConfigEntry(domain=DOMAIN, title="Living Room", data={CONF_SCREEN_ID: "living"})
    other = MockConfigEntry(domain=DOMAIN, title="Bedroom", data={CONF_SCREEN_ID: "bedroom"})
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    saved_skip = datetime(2026, 11, 8, tzinfo=UTC)
    store = runtime.activity_store(hass, entry.entry_id)
    await store.async_save({"count": 1, "last_skip": saved_skip.isoformat()})
    registry = er.async_get(hass)
    count = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_skip_count",
        suggested_object_id="living_room_sponsorblock_skips_since_load",
        config_entry=entry,
    ).entity_id

    with (
        patch.object(
            runtime,
            "async_verify_screen_id",
            return_value=SimpleNamespace(lounge_token="example-token"),
        ),
        patch.object(runtime.YtLoungeApi, "is_available", return_value=False),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert other.state is ConfigEntryState.LOADED
        last_skip = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_last_skip")
        last_short_disruption = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_last_short_disruption"
        )
        assert last_skip is not None
        assert last_short_disruption is not None
        assert hass.states.get(count).state == "1"
        assert hass.states.get(last_skip).state == saved_skip.isoformat(timespec="seconds")
        assert hass.states.get(last_short_disruption).state == STATE_UNKNOWN

        previous_runtime = entry.runtime_data
        previous_runtime._async_skipped()
        previous_runtime._async_short_disrupted()
        skip_time = previous_runtime.snapshot.last_skip
        disruption_time = previous_runtime.snapshot.last_short_disruption
        assert skip_time is not None
        assert disruption_time is not None
        assert hass.states.get(count).state == "2"
        assert hass.states.get(last_short_disruption).state == disruption_time.isoformat(
            timespec="seconds"
        )

        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.runtime_data is not previous_runtime
        assert hass.states.get(count).state == "2"
        assert hass.states.get(last_skip).state == skip_time.isoformat(timespec="seconds")
        assert hass.states.get(last_short_disruption).state == disruption_time.isoformat(
            timespec="seconds"
        )
        assert entry.runtime_data.snapshot.last_skip == skip_time
        assert entry.runtime_data.snapshot.last_short_disruption == disruption_time
        assert other.runtime_data.snapshot.skip_count == 0
        assert other.runtime_data.snapshot.last_skip is None
        assert other.runtime_data.snapshot.last_short_disruption is None

        entry.runtime_data._async_skipped()
        assert hass.states.get(count).state == "3"
        assert await hass.config_entries.async_remove(entry.entry_id) == {"require_restart": False}
        assert await runtime.activity_store(hass, entry.entry_id).async_load() is None
        assert await hass.config_entries.async_unload(other.entry_id)
