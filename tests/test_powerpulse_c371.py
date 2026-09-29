"""Synthetic C371 heartbeat: no owner identifiers or captured payloads."""

from __future__ import annotations

import struct

from ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWERPULSE2, get_device_type
from ecoflow_energy.ecoflow.parsers.powerpulse_proto import (
    parse_powerpulse_message,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

from custom_components.ecoflow_energy.const import (
    POWERPULSE2_BINARY_SENSORS,
    POWERPULSE2_SENSORS,
    filter_defs_for_serial,
)


def test_c371_sensor_definitions() -> None:
    serial = "C371TEST00000001"
    assert get_device_type("", serial) == DEVICE_TYPE_POWERPULSE2
    assert filter_defs_for_serial(POWERPULSE2_SENSORS, serial) == POWERPULSE2_SENSORS
    assert (
        filter_defs_for_serial(POWERPULSE2_BINARY_SENSORS, serial)
        == POWERPULSE2_BINARY_SENSORS
    )


def test_synthetic_c371_suspended_heartbeat() -> None:
    # Dummy values constructed by hand using the observed field layout.
    readings = bytes([4 << 3 | 5]) + struct.pack("<f", 0.0)
    readings += bytes([7 << 3 | 5]) + struct.pack("<f", 230.0)
    pdata = encode_field_bytes(8, readings)
    for field, value in (
        (1, 5),
        (17, 60),
        (18, 320),
        (21, 1),
        (42, 0),
        (43, 10000),
        (44, 10000),
        (101, 5),
    ):
        pdata += encode_field_varint(field, value)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 2)
        + encode_field_varint(9, 33)
    )
    result = parse_powerpulse_message(encode_field_bytes(1, header))
    assert result is not None
    assert result["ev_charge_status"] == "suspended_vehicle"
    assert result["ev_charge_power_w"] == 0.0
    assert result["ev_voltage_l1_v"] == 230.0
    assert result["ev_max_current_a"] == 32.0
    assert result["ev_charge_current_a"] == 6.0
    assert (
        result["ev_total_energy_wh"] - result["ev_session_start_energy_wh"]
        == result["ev_session_energy_wh"]
        == 0
    )
