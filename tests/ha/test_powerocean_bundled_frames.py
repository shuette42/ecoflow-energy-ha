"""Coordinator-level regression tests for bundled PowerOcean frames."""

from unittest.mock import patch

import pytest

from custom_components.ecoflow_energy.coordinator.mqtt_ingest import MqttIngestMixin
from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1BpHeartbeatReport,
    JTS1EmsChangeReport,
    JTS1EmsHeartbeat,
    JTS1EmsParamChangeReport,
    JTS1EmsPVInvEnergyStreamReport,
    JTS1EnergyStreamReport,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)


class _PowerOceanParser(MqttIngestMixin):
    """Minimal parser host for the frame merge test."""

    device_sn = "R374MASKEDTEST"

    def __init__(self) -> None:
        self._bp_sn_to_index: dict[str, int] = {}


def _build_header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    header = bytearray()
    if pdata:
        header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def test_powerocean_parser_merges_bundled_stream_and_heartbeat() -> None:
    """The coordinator receives one sensor mapping from the whole bundle."""
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0,
        sys_grid_pwr=-6280.0,
        mppt_pwr=6730.0,
    )
    heartbeat = JTS1EmsHeartbeat(pcs_ac_freq=50.0)
    container = heartbeat.mppt_heart_beat.add()
    pv = container.mppt_pv.add()
    pv.pwr = 3529.0
    pv.vol = 501.0
    pv.amp = 7.04

    frame = b"".join(
        (
            _build_header(96, 33, stream.SerializeToString()),
            _build_header(96, 39, b""),
            _build_header(96, 1, heartbeat.SerializeToString()),
        )
    )

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert parsed is not None
    assert parsed["solar_w"] == 6730.0
    assert parsed["home_w"] == 450.0
    assert parsed["grid_w"] == -6280.0
    assert parsed["grid_export_power_w"] == 6280.0
    assert parsed["pcs_ac_freq_hz"] == 50.0
    assert parsed["mppt_pv1_power_w"] == 3529.0


def _energy_stream_frame() -> bytes:
    message = JTS1EnergyStreamReport(
        sys_load_pwr=450.0, sys_grid_pwr=-6280.0, mppt_pwr=6730.0, bp_pwr=-1200.0
    )
    return _build_header(96, 33, message.SerializeToString())


def _ems_heartbeat_frame() -> bytes:
    message = JTS1EmsHeartbeat(pcs_ac_freq=50.0)
    container = message.mppt_heart_beat.add()
    pv = container.mppt_pv.add()
    pv.pwr = 3529.0
    pv.vol = 501.0
    pv.amp = 7.04
    message.pcs_a_phase.vol = 237.3
    message.pcs_a_phase.act_pwr = -1221.4
    return _build_header(96, 1, message.SerializeToString())


def _bp_heartbeat_frame() -> bytes:
    message = JTS1BpHeartbeatReport()
    pack = message.bp_heart_beat.add()
    pack.bp_sn = b"BPTESTPACK000001"
    pack.bp_soc = 87
    pack.bp_pwr = -1200.0
    pack.bp_design_cap = 4000
    pack.bp_full_cap = 3980
    return _build_header(96, 7, message.SerializeToString())


def _ems_change_frame() -> bytes:
    message = JTS1EmsChangeReport(bp_soc=87, sys_bat_chg_up_limit=100)
    return _build_header(96, 8, message.SerializeToString())


def _ems_param_change_frame() -> bytes:
    message = JTS1EmsParamChangeReport(dev_soc=42)
    return _build_header(96, 13, message.SerializeToString())


# Exact key sets a single-header HJ31/HJ32 frame produced before bundled
# replies were supported. The PR that added per-header decoding must not
# change what a one-header device reports.
_LEGACY_SINGLE_HEADER_KEYS: list[tuple[str, object, set[str]]] = [
    (
        "energy_stream",
        _energy_stream_frame,
        {
            "solar_w",
            "home_w",
            "grid_w",
            "batt_w",
            "grid_import_power_w",
            "grid_export_power_w",
            "batt_charge_power_w",
            "batt_discharge_power_w",
        },
    ),
    (
        "ems_heartbeat",
        _ems_heartbeat_frame,
        {
            "pcs_ac_freq_hz",
            "mppt_pv1_power_w",
            "mppt_pv1_voltage_v",
            "mppt_pv1_current_a",
            "grid_phase_a_voltage_v",
            "grid_phase_a_current_a",
            "grid_phase_a_active_power_w",
            "grid_phase_a_reactive_power_var",
            "grid_phase_a_apparent_power_va",
            "grid_status",
        },
    ),
    (
        "bp_heartbeat",
        _bp_heartbeat_frame,
        {
            "pack1_soc",
            "pack1_power_w",
            "pack1_design_cap_mah",
            "pack1_full_cap_mah",
        },
    ),
    (
        "ems_change",
        _ems_change_frame,
        {"ems_charge_upper_limit_pct"},
    ),
    (
        "ems_param_change",
        _ems_param_change_frame,
        {"ems_app_surplus_pct"},
    ),
]


@pytest.mark.parametrize(
    ("name", "build_frame", "expected_keys"),
    _LEGACY_SINGLE_HEADER_KEYS,
    ids=[case[0] for case in _LEGACY_SINGLE_HEADER_KEYS],
)
def test_hj31_single_header_key_set_matches_legacy(
    name: str, build_frame, expected_keys: set[str]
) -> None:
    """One-header PowerOcean frames keep exactly their previous sensor keys."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(build_frame())

    assert parsed is not None
    assert set(parsed) == expected_keys


def test_hj31_single_header_unknown_command_yields_nothing() -> None:
    """An unregistered command tuple produces no sensor keys at all."""
    frame = _build_header(96, 99, b"\x08\x01")

    assert _PowerOceanParser()._parse_powerocean_proto_frame(frame) is None


def test_one_bad_header_keeps_the_rest_of_the_bundle() -> None:
    """A header that raises during remap does not discard the other headers."""
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0, sys_grid_pwr=-6280.0, mppt_pwr=6730.0
    )
    heartbeat = JTS1EmsHeartbeat(pcs_ac_freq=50.0)

    frame = _build_header(96, 1, heartbeat.SerializeToString()) + _build_header(
        96, 33, stream.SerializeToString()
    )

    with patch(
        "custom_components.ecoflow_energy.coordinator.mqtt_ingest."
        "flatten_heartbeat",
        side_effect=ValueError("bad header"),
    ):
        parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert parsed is not None
    assert parsed["solar_w"] == 6730.0
    assert parsed["home_w"] == 450.0
    assert "pcs_ac_freq_hz" not in parsed


def test_empty_pv_inv_companion_does_not_zero_heartbeat_value() -> None:
    """An empty (96,39) companion leaves the heartbeat's inverter power alone.

    JTS1EmsPVInvEnergyStreamReport carries a single field, so a report that
    omits it serializes to zero bytes. That is exactly what the R374 bundle
    sends next to the heartbeat.
    """
    heartbeat = JTS1EmsHeartbeat(ems_pv_inv_pwr=987.0)
    companion = JTS1EmsPVInvEnergyStreamReport()

    frame = _build_header(96, 1, heartbeat.SerializeToString()) + _build_header(
        96, 39, companion.SerializeToString()
    )

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert parsed is not None
    assert parsed["pv_inverter_power_w"] == 987.0
