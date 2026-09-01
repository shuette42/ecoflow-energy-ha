"""Registry cleanup for the former HW51 Stream misclassification."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy import (
    _HW51_LEGACY_STREAM_KEYS_BY_DOMAIN,
    _async_remove_legacy_hw51_stream_entities,
)
from custom_components.ecoflow_energy.const import (
    CONF_DEVICES,
    DOMAIN,
    POWERSTREAM_SENSORS,
)

HW51 = "HW51TEST00000001"
BK31 = "BK31TEST00000001"

# Exact v1.17 Stream surface minus the 16 sensor keys that are valid current
# PowerStream entities. Kept independent of the production cleanup table so a
# future accidental omission cannot make both implementation and test agree.
V117_STALE_BY_DOMAIN = {
    "sensor": frozenset({
        "ac_current_a",
        "ac_grid_connection_power_w",
        "ac_outlet_1_w",
        "ac_outlet_2_w",
        "backup_reserve_pct",
        "batt_charge_capacity_ah",
        "batt_charge_discharge_state",
        "batt_charge_energy_kwh",
        "batt_charge_power_w",
        "batt_design_cap_mah",
        "batt_discharge_capacity_ah",
        "batt_discharge_energy_kwh",
        "batt_discharge_power_w",
        "batt_full_cap_mah",
        "batt_max_cell_temp_c",
        "batt_max_cell_vol_mv",
        "batt_max_mos_temp_c",
        "batt_min_cell_temp_c",
        "batt_min_cell_vol_mv",
        "batt_remain_cap_mah",
        "bms_soh_pct",
        "feed_grid_power_limit_w",
        "grid_connection_power_w",
        "grid_connection_state",
        "home_energy_kwh",
        "home_from_batt_w",
        "home_from_grid_w",
        "home_from_solar_w",
        "home_w",
        "pv2_current_a",
        "pv3_energy_kwh",
        "pv3_w",
        "pv4_energy_kwh",
        "pv4_w",
        "pv_current_a",
        "pv_voltage_v",
        "soc_precise_pct",
        "sys_grid_connection_power_w",
    }),
    "binary_sensor": frozenset({
        "ac_outlet_1_enabled",
        "ac_outlet_2_enabled",
    }),
    "switch": frozenset({"ac_outlet_1_switch", "ac_outlet_2_switch"}),
    "number": frozenset({
        "backup_reserve",
        "led_brightness",
        "stream_charge_limit",
        "stream_discharge_limit",
    }),
}


def _entry(hass: HomeAssistant, devices: list[dict] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={CONF_DEVICES: devices or []},
    )
    entry.add_to_hass(hass)
    return entry


def _register(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    domain: str,
    unique_id: str,
    *,
    integration: str = DOMAIN,
) -> str:
    return er.async_get(hass).async_get_or_create(
        domain, integration, unique_id, config_entry=entry
    ).entity_id


def _ids(hass: HomeAssistant) -> set[str]:
    return set(er.async_get(hass).entities)


def test_cleanup_table_matches_complete_v117_surface() -> None:
    assert _HW51_LEGACY_STREAM_KEYS_BY_DOMAIN == V117_STALE_BY_DOMAIN


def test_removes_complete_surface_for_deselected_hw51(
    hass: HomeAssistant,
) -> None:
    # The device is intentionally absent from CONF_DEVICES. Registry rows are
    # the durable evidence and must still be cleaned after deselection.
    entry = _entry(hass)
    stale = {
        _register(hass, entry, domain, f"{HW51}_{key}")
        for domain, keys in V117_STALE_BY_DOMAIN.items()
        for key in keys
    }

    _async_remove_legacy_hw51_stream_entities(hass, entry)

    assert not (stale & _ids(hass))


def test_every_current_powerstream_id_and_customization_survives(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    current = {
        definition.key: _register(
            hass, entry, "sensor", f"{HW51}_{definition.key}"
        )
        for definition in POWERSTREAM_SENSORS
    }
    registry = er.async_get(hass)
    registry.async_update_entity(current["solar_w"], name="Roof solar")

    _async_remove_legacy_hw51_stream_entities(hass, entry)

    assert set(current.values()) <= _ids(hass)
    assert registry.async_get(current["solar_w"]).name == "Roof solar"


def test_cleanup_is_platform_and_key_exact(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    stale_number = _register(hass, entry, "number", f"{HW51}_led_brightness")
    keep = {
        # Same key is a current read-only PowerStream sensor.
        _register(hass, entry, "sensor", f"{HW51}_led_brightness"),
        # Stale keys on the wrong platform are not historical entity ids.
        _register(hass, entry, "sensor", f"{HW51}_stream_charge_limit"),
        _register(hass, entry, "number", f"{HW51}_home_w"),
        # Similar but not exact unique-id key.
        _register(hass, entry, "sensor", f"{HW51}_home_w_extra"),
        # A row owned by another integration is outside the cleanup.
        _register(
            hass, entry, "sensor", f"{HW51}_home_w", integration="example"
        ),
    }

    _async_remove_legacy_hw51_stream_entities(hass, entry)

    assert stale_number not in _ids(hass)
    assert keep <= _ids(hass)


def test_other_entry_and_bk_device_survive(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    other_entry = _entry(hass)
    other_entry_row = _register(
        hass, other_entry, "sensor", f"{HW51}_home_w"
    )
    bk_row = _register(hass, entry, "sensor", f"{BK31}_home_w")

    _async_remove_legacy_hw51_stream_entities(hass, entry)

    assert {other_entry_row, bk_row} <= _ids(hass)


def test_invalid_or_lowercase_serial_does_not_trigger(
    hass: HomeAssistant,
) -> None:
    entry = _entry(hass)
    keep = {
        _register(hass, entry, "sensor", f"{serial}_home_w")
        for serial in ("HW51SHORT", "hw51test00000001")
    }

    _async_remove_legacy_hw51_stream_entities(hass, entry)

    assert keep <= _ids(hass)


def test_cleanup_is_idempotent(hass: HomeAssistant) -> None:
    entry = _entry(hass)
    _register(hass, entry, "sensor", f"{HW51}_home_w")

    _async_remove_legacy_hw51_stream_entities(hass, entry)
    _async_remove_legacy_hw51_stream_entities(hass, entry)
