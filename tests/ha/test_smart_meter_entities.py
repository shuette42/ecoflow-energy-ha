"""Entity-set tests for the EcoFlow Smart Meter (BK21).

The parser tests prove the field map against the reporter's capture. These
run one of the same frames through the coordinator, because the failure this
family already had once was never in a parser: a BK-series serial that lands
on the wrong device type produces readings that are correct for a device
nobody has.

The meter shares two keys with the Stream, so "the meter's entities exist"
is not enough on its own - a Stream must keep its own set, and the per-phase
readings must not appear on it.
"""

from __future__ import annotations

import binascii
import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.binary_sensor import (
    async_setup_entry as binary_sensor_setup,
)
from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_SMART_METER,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    MODE_ENHANCED,
    SMARTMETER_BINARY_SENSORS,
    SMARTMETER_SENSORS,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ecoflow_energy.ecoflow.const import (
    get_device_name,
    get_device_type,
)
from custom_components.ecoflow_energy.sensor import async_setup_entry as sensor_setup

CAPTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "smart_meter"
    / "bk21_frames_issue331.json"
)

# The 146-byte full upload of the reporter's capture: the one frame that
# carries every reading at once, including the voltages and currents the
# shorter frames leave out.
FULL_UPLOAD_INDEX = 4

BK21_DEVICE: dict[str, Any] = {
    "sn": "BK21TEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_SMART_METER,
    "online": 1,
}

BK31_DEVICE: dict[str, Any] = {
    "sn": "BK31TEST00000001",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_STREAM,
    "online": 1,
}

# Readings only the meter has. `grid_w` and `grid_connection_state` are
# deliberately absent from this set: the Stream reports both, so neither
# would separate the two device types.
METER_ONLY_KEYS = {
    "grid_l1_w",
    "grid_l2_w",
    "grid_l3_w",
    "grid_l1_voltage_v",
    "grid_l2_voltage_v",
    "grid_l3_voltage_v",
    "grid_l1_current_a",
    "grid_l2_current_a",
    "grid_l3_current_a",
    "grid_import_energy_wh",
    "grid_export_energy_wh",
    "grid_net_energy_wh",
    "grid_l1_net_energy_wh",
    "grid_l2_net_energy_wh",
    "grid_l3_net_energy_wh",
    "grid_power_factor",
}


def _entry(device: dict[str, Any]) -> MockConfigEntry:
    """Build an Enhanced-mode entry for one device."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )


async def _setup_entities(
    hass: HomeAssistant,
    platform_setup,
    device: dict[str, Any],
    reported: dict[str, Any] | None = None,
) -> list[Any]:
    """Run one platform's setup and return the definition-driven entities."""
    entry = _entry(device)
    entry.add_to_hass(hass)
    coordinator = EcoFlowDeviceCoordinator(hass, entry, device)
    if reported:
        coordinator.async_set_updated_data(dict(reported))
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {device["sn"]: coordinator}

    created: list[Any] = []
    await platform_setup(hass, entry, created.extend)
    return [entity for entity in created if hasattr(entity, "_definition")]


def _frame(index: int) -> bytes:
    frames = json.loads(CAPTURE.read_text())["frames"]
    return binascii.unhexlify(frames[index]["hex"])


class TestSmartMeterRouting:
    def test_bk21_is_its_own_device_type(self) -> None:
        assert get_device_type("", "BK21TEST00000001") == DEVICE_TYPE_SMART_METER

    def test_bk21_display_name(self) -> None:
        assert get_device_name("", "BK21TEST00001234") == "Smart Meter (1234)"


class TestSmartMeterEntitySet:
    async def test_the_meter_gets_its_eighteen_sensors(
        self, hass: HomeAssistant
    ) -> None:
        """18, not 17: ADR-018 split the five old energy keys into six
        (import, export, net, three phase nets), one more than before."""
        entities = await _setup_entities(hass, sensor_setup, BK21_DEVICE)

        keys = {entity._definition.key for entity in entities}
        assert len(entities) == 18
        assert keys == {sensor.key for sensor in SMARTMETER_SENSORS}

    async def test_the_meter_gets_its_three_binary_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_entities(hass, binary_sensor_setup, BK21_DEVICE)

        keys = {entity._definition.key for entity in entities}
        assert len(entities) == 3
        assert keys == {sensor.key for sensor in SMARTMETER_BINARY_SENSORS}

    async def test_a_stream_does_not_get_the_meter_readings(
        self, hass: HomeAssistant
    ) -> None:
        """A BK31 is a Stream. It shares two keys with the meter and must
        keep exactly those, or the split has leaked."""
        entities = await _setup_entities(hass, sensor_setup, BK31_DEVICE)

        keys = {entity._definition.key for entity in entities}
        assert not keys & METER_ONLY_KEYS

    async def test_a_stream_does_not_get_the_phase_connection_flags(
        self, hass: HomeAssistant
    ) -> None:
        entities = await _setup_entities(hass, binary_sensor_setup, BK31_DEVICE)

        keys = {entity._definition.key for entity in entities}
        assert not keys & {sensor.key for sensor in SMARTMETER_BINARY_SENSORS}


class TestSmartMeterDefinitions:
    def test_import_and_export_are_the_only_monotonic_counters(self) -> None:
        """Direction, not a reset period, decides `total_increasing` now
        (ADR-018, PLAN-123): the meter has no daily reset at all, so import
        and export are monotonic because they only ever stand still or rise,
        and net and the three phase nets are excluded because a net exporter
        makes them fall.

        Before the rename this test held the four keys with a midnight
        reset (`_today_wh`) and excluded the single lifetime key; both the
        premise (a daily/lifetime split) and the answer have changed.
        """
        monotonic = [
            sensor.key
            for sensor in SMARTMETER_SENSORS
            if sensor.state_class == "total_increasing"
        ]

        assert sorted(monotonic) == [
            "grid_export_energy_wh",
            "grid_import_energy_wh",
        ]

    def test_the_net_counters_are_plain_totals(self) -> None:
        """There is no longer one lifetime sensor to single out - four
        sensors fall whenever the house exports (net plus the three phase
        nets), and all four must be `total`, which permits the negative
        delta a net exporter's total eventually crosses."""
        nets = [
            sensor
            for sensor in SMARTMETER_SENSORS
            if sensor.key
            in {
                "grid_net_energy_wh",
                "grid_l1_net_energy_wh",
                "grid_l2_net_energy_wh",
                "grid_l3_net_energy_wh",
            }
        ]

        assert len(nets) == 4
        assert all(sensor.state_class == "total" for sensor in nets)

    def test_the_energy_counters_have_the_new_key_names(self) -> None:
        """The entity key list ADR-018 replaced the five `_today_wh`/
        `_total_wh` keys with: no key here may still carry a period in its
        name, only a direction."""
        energy_keys = sorted(
            sensor.key
            for sensor in SMARTMETER_SENSORS
            if sensor.device_class == "energy"
        )

        assert energy_keys == [
            "grid_export_energy_wh",
            "grid_import_energy_wh",
            "grid_l1_net_energy_wh",
            "grid_l2_net_energy_wh",
            "grid_l3_net_energy_wh",
            "grid_net_energy_wh",
        ]

    def test_every_energy_counter_reports_watt_hours(self) -> None:
        """Same property as before the rename, one more sensor to check:
        the split added `grid_export_energy_wh`, six energy counters now
        instead of five."""
        energy = [
            sensor for sensor in SMARTMETER_SENSORS if sensor.device_class == "energy"
        ]

        assert len(energy) == 6
        assert all(sensor.unit == "Wh" for sensor in energy)

    def test_the_enum_offers_exactly_the_states_the_parser_produces(self) -> None:
        from custom_components.ecoflow_energy.ecoflow.parsers.stream_proto import (
            _GRID_CONNECTION_STATE,
        )

        state = next(
            sensor
            for sensor in SMARTMETER_SENSORS
            if sensor.key == "grid_connection_state"
        )

        assert set(state.options or []) == set(_GRID_CONNECTION_STATE.values())

    def test_the_phase_readings_are_labelled_the_way_the_app_counts_them(
        self,
    ) -> None:
        """The wire numbers the phases L1-L3 and the app letters them A-C.
        The keys follow the wire, the labels follow the app."""
        assert [
            sensor.name for sensor in SMARTMETER_SENSORS if sensor.key.endswith("_w")
        ] == ["Grid Power", "Phase A Power", "Phase B Power", "Phase C Power"]

    def test_every_phase_reading_has_a_device_class(self) -> None:
        assert all(sensor.device_class for sensor in SMARTMETER_SENSORS)


class TestTheCaptureReachesTheEntities:
    """The point of the whole phase: a frame the reporter's meter sent, in,
    and Home Assistant states out."""

    async def test_the_full_upload_fills_the_sensors(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(BK21_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, BK21_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{BK21_DEVICE['sn']}", _frame(FULL_UPLOAD_INDEX)
        )
        assert parsed is not None
        coordinator.async_set_updated_data(dict(parsed))
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            BK21_DEVICE["sn"]: coordinator
        }

        created: list[Any] = []
        await sensor_setup(hass, entry, created.extend)
        values = {
            entity._definition.key: entity.native_value
            for entity in created
            if hasattr(entity, "_definition")
        }

        # 406.65 W on the wire, shown as whole watts.
        assert values["grid_w"] == 407
        # Field .4 (import) and .7 (net) are both 1345 in this frame because
        # the house had not exported yet; .6 (export) is absent from the
        # wire and fills to zero (ADR-018, PLAN-123).
        assert values["grid_import_energy_wh"] == 1345
        assert values["grid_net_energy_wh"] == 1345
        assert values["grid_export_energy_wh"] == 0
        assert values["grid_connection_state"] == "grid_in"
        # Phase B carries most of the load in this frame, phase A none.
        assert values["grid_l1_w"] == 0
        assert values["grid_l2_w"] == 318
        assert values["grid_l2_voltage_v"] == 239.4
        assert values["grid_l2_current_a"] == 2.11

    async def test_the_full_upload_fills_the_connection_flags(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(BK21_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, BK21_DEVICE)

        parsed = coordinator._parse_message(
            f"/app/device/property/{BK21_DEVICE['sn']}", _frame(FULL_UPLOAD_INDEX)
        )
        assert parsed is not None
        coordinator.async_set_updated_data(dict(parsed))
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            BK21_DEVICE["sn"]: coordinator
        }

        created: list[Any] = []
        await binary_sensor_setup(hass, entry, created.extend)
        values = {
            entity._definition.key: entity.is_on
            for entity in created
            if hasattr(entity, "_definition")
        }

        assert values == {
            "grid_l1_connected": True,
            "grid_l2_connected": True,
            "grid_l3_connected": True,
        }


class TestTheNetCounterCanFall:
    """The whole reason `grid_net_energy_wh` and the phase nets are `total`
    and not `total_increasing` (ADR-018, PLAN-123): a net exporter's total
    genuinely falls, and the coordinator's monotonic guard must let that
    reading through to the sensor rather than freeze it at the earlier,
    higher value.

    This is new coverage, not a reshape - nothing before the rename could
    exercise it, because every energy key was either a daily counter (reset
    to zero, but never asked to fall mid-day) or the one lifetime key
    (which could only ever rise).
    """

    async def test_a_falling_net_reading_reaches_the_sensor(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(BK21_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, BK21_DEVICE)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            BK21_DEVICE["sn"]: coordinator
        }

        # First reading high, second one lower - the shape a net exporter's
        # total actually takes, and exactly what `_enforce_monotonic` would
        # discard if `grid_net_energy_wh` were still `total_increasing`.
        coordinator._apply_data({"grid_net_energy_wh": 500.0})
        coordinator._apply_data({"grid_net_energy_wh": 300.0})

        created: list[Any] = []
        await sensor_setup(hass, entry, created.extend)
        values = {
            entity._definition.key: entity.native_value
            for entity in created
            if hasattr(entity, "_definition")
        }

        assert values["grid_net_energy_wh"] == 300.0


class TestSmartMeterDiagnostics:
    """A device that reports fine but shows up as skipped in a diagnostics
    download sends every future reporter down the wrong path."""

    async def test_the_meter_is_a_device_not_a_skipped_one(
        self, hass: HomeAssistant
    ) -> None:
        entry = _entry(BK21_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, BK21_DEVICE)
        parsed = coordinator._parse_message(
            f"/app/device/property/{BK21_DEVICE['sn']}", _frame(FULL_UPLOAD_INDEX)
        )
        assert parsed is not None
        coordinator._device_data.update(parsed)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            BK21_DEVICE["sn"]: coordinator
        }

        diag = await async_get_config_entry_diagnostics(hass, entry)

        assert diag["skipped_devices"] == []
        assert len(diag["devices"]) == 1
        assert diag["devices"][0]["device_sn"].startswith("BK21")
        assert "grid_w" in diag["devices"][0]["data_keys"]

    async def test_the_meter_carries_no_raw_quota_section(
        self, hass: HomeAssistant
    ) -> None:
        """It has no HTTP quota at all, so an empty raw quota block would
        read as a device that answered with nothing."""
        entry = _entry(BK21_DEVICE)
        entry.add_to_hass(hass)
        coordinator = EcoFlowDeviceCoordinator(hass, entry, BK21_DEVICE)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            BK21_DEVICE["sn"]: coordinator
        }

        diag = await async_get_config_entry_diagnostics(hass, entry)

        assert "raw_quota" not in diag["devices"][0]
