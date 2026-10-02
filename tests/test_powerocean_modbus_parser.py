"""Tests for the PowerOcean local Modbus register map."""

import json
import struct
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.modbus_local import (
    BACKUP_RATIO_OFFSET,
    BRIGHTNESS_OFFSET,
)
from ecoflow_energy.ecoflow.parsers.powerocean_modbus import (
    LIFETIME_COUNTER_KEYS,
    POLL_BLOCKS,
    SETUP_BLOCKS,
    is_supported_device,
    parse_device_info,
    parse_registers,
)

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "powerocean_modbus_frames.json").read_text()
)
DEVICE = _FIXTURE["device"]
# Not keyed "frames": test_frame_capture scans every tests/fixtures JSON and
# reads a top-level "frames" key as a list of recorded protobuf frames.
# The system status word and the indicator brightness are set per frame here,
# not in the JSON: the coordinator test reads the same fixture with its own map.
# 0x1814 has bit 11 set (control active), 0x1014 has not. Neither has bit 0
# (off-grid) or bit 1 (abnormal).
# The battery system readings and the feed limit are the values the real device
# answered on 2026-10-02 21:24 (status 0x1014): the day frame carries them as
# measured, the night frame carries other numbers so that a pair of swapped
# offsets cannot hide behind equal values.
LIVE = {
    "battery_voltage": 52.305,
    "battery_current": -16.247,
    "battery_temp": 33.0,
    "max_feed_power": 7000,
}
DAY = {
    **_FIXTURE["samples"]["day"],
    **LIVE,
    "system_status": 0x1814,
    "system_state2": 0,
    "indicator_brightness": 80,
}
NIGHT = {
    **_FIXTURE["samples"]["night"],
    "battery_voltage": 49.875,
    "battery_current": 3.5,
    "battery_temp": 28.5,
    "max_feed_power": 5000,
    "system_status": 0x1014,
    "system_state2": 0,
    "indicator_brightness": 20,
}

# Fixture field -> (register offset, wire type). Written out by hand from the
# protocol table, independent of the parser's own map.
_REGISTERS = {
    "load_w": (0x0206, "f32"),
    "grid_w": (0x0208, "f32"),
    "solar_w": (0x020A, "f32"),
    "battery_w": (0x020C, "f32"),
    "soc": (0x020E, "u16"),
    "system_status": (0x0211, "u32"),
    "system_state2": (0x0213, "u32"),
    "backup_ratio": (0x0217, "u16"),
    "indicator_brightness": (0x021C, "u16"),
    "battery_capacity_wh": (0x0227, "u32"),
    "battery_voltage": (0x023D, "f32"),
    "battery_current": (0x023F, "f32"),
    "battery_temp": (0x0241, "f32"),
    "inv_freq": (0x0251, "f32"),
    "pv1_voltage": (0x0253, "f32"),
    "pv2_voltage": (0x0255, "f32"),
    "pv3_voltage": (0x0257, "f32"),
    "pv1_current": (0x0259, "f32"),
    "pv2_current": (0x025B, "f32"),
    "pv3_current": (0x025D, "f32"),
    "max_feed_power": (0x0260, "u32"),
    "fault_count": (0x0800, "u16"),
    "batteries_online": (0x0820, "u16"),
    "grid_draw_total_kwh": (0x0870, "f32"),
    "grid_feed_total_kwh": (0x0880, "f32"),
    "bat_charge_total_kwh": (0x08B0, "f32"),
    "bat_discharge_total_kwh": (0x08C0, "f32"),
    "solar_total_kwh": (0x08D0, "f32"),
}

# Sensor key -> fixture field, for every value passed through unchanged.
KEY_TO_FIELD = {
    "home_w": "load_w",
    "grid_w": "grid_w",
    "solar_w": "solar_w",
    "batt_w": "battery_w",
    "soc_pct": "soc",
    "local_system_status": "system_status",
    "local_system_state2": "system_state2",
    "ems_backup_ratio_pct": "backup_ratio",
    "local_indicator_brightness_pct": "indicator_brightness",
    "ems_total_battery_capacity_wh": "battery_capacity_wh",
    "batt_voltage_v": "battery_voltage",
    "batt_current_a": "battery_current",
    "batt_temp_c": "battery_temp",
    "ems_feed_power_limit_w": "max_feed_power",
    "pcs_ac_freq_hz": "inv_freq",
    "mppt_pv1_voltage_v": "pv1_voltage",
    "mppt_pv2_voltage_v": "pv2_voltage",
    "mppt_pv1_current_a": "pv1_current",
    "mppt_pv2_current_a": "pv2_current",
    "fault_count": "fault_count",
    "bp_online_sum": "batteries_online",
    "batt_charge_energy_kwh": "bat_charge_total_kwh",
    "batt_discharge_energy_kwh": "bat_discharge_total_kwh",
    "solar_lifetime_energy_kwh": "solar_total_kwh",
    "grid_import_lifetime_energy_kwh": "grid_draw_total_kwh",
    "grid_export_lifetime_energy_kwh": "grid_feed_total_kwh",
}
DERIVED_KEYS = {
    "modbus_control_active",
    "local_off_grid",
    "local_system_abnormal",
    "local_bms_connected",
    "local_system_alerts",
    "grid_import_power_w",
    "grid_export_power_w",
    "batt_charge_power_w",
    "batt_discharge_power_w",
}
EXPECTED_KEYS = set(KEY_TO_FIELD) | DERIVED_KEYS


def _encode(kind, value):
    """Encode a decoded value the way the device sends it: word-swapped."""
    if kind == "u16":
        return struct.pack(">H", value)
    raw = struct.pack(">I" if kind == "u32" else ">f", value)
    return raw[2:4] + raw[0:2]


def _poll_blocks(frame):
    """Build the register bytes of every poll block from decoded fixture values."""
    blocks = {start: bytearray(2 * count) for start, count in POLL_BLOCKS}
    for name, value in frame.items():
        offset, kind = _REGISTERS[name]
        data = _encode(kind, value)
        for start, count in POLL_BLOCKS:
            if start <= offset and offset + len(data) // 2 <= start + count:
                low = (offset - start) * 2
                blocks[start][low : low + len(data)] = data
    return {start: bytes(raw) for start, raw in blocks.items()}


def _setup_blocks(device):
    """Identity registers: u16 triple, serial as ASCII, firmware as UINT32."""
    return {
        0x0000: struct.pack(
            ">HHH",
            device["protocol_version"],
            device["product_category"],
            device["product_number"],
        ),
        0x0003: device["serial"].encode("ascii"),
        0x000B: bytes(device["firmware"]),
    }


def test_day_frame_maps_the_expected_keys_and_values():
    result = parse_registers(_poll_blocks(DAY))

    assert set(result) == EXPECTED_KEYS
    for key, field in KEY_TO_FIELD.items():
        assert result[key] == pytest.approx(DAY[field], abs=1e-3), key
    assert result["grid_export_power_w"] == pytest.approx(4309.337, abs=1e-3)
    assert result["grid_import_power_w"] == 0.0
    assert result["local_indicator_brightness_pct"] == 80
    assert result["modbus_control_active"] is True
    assert result["local_off_grid"] is False
    assert result["local_system_abnormal"] is False
    assert result["local_bms_connected"] is True
    assert result["local_system_alerts"] == "none"


def test_night_frame_discharges_and_exports_with_the_night_counters():
    result = parse_registers(_poll_blocks(NIGHT))

    assert set(result) == EXPECTED_KEYS
    assert result["batt_discharge_power_w"] == pytest.approx(823.579, abs=1e-3)
    assert result["batt_charge_power_w"] == 0.0
    assert result["grid_export_power_w"] == pytest.approx(7.65, abs=1e-3)
    assert result["grid_import_power_w"] == 0.0
    assert result["solar_lifetime_energy_kwh"] == pytest.approx(21973.359, abs=1e-3)
    assert result["grid_import_lifetime_energy_kwh"] == pytest.approx(
        8677.942, abs=1e-3
    )
    assert result["grid_export_lifetime_energy_kwh"] == pytest.approx(
        10040.026, abs=1e-3
    )
    assert result["batt_discharge_energy_kwh"] == pytest.approx(6165.245, abs=1e-3)
    assert result["local_indicator_brightness_pct"] == 20
    assert result["modbus_control_active"] is False
    assert result["batt_voltage_v"] == pytest.approx(49.875, abs=1e-3)
    assert result["batt_current_a"] == pytest.approx(3.5, abs=1e-3)
    assert result["batt_temp_c"] == pytest.approx(28.5, abs=1e-3)
    assert result["ems_feed_power_limit_w"] == 5000


def test_a_non_finite_float_is_dropped_with_the_keys_derived_from_it():
    frame = dict(DAY, grid_w=float("nan"), solar_w=float("inf"))

    result = parse_registers(_poll_blocks(frame))

    for key in ("grid_w", "grid_import_power_w", "grid_export_power_w", "solar_w"):
        assert key not in result
    assert result["home_w"] == pytest.approx(DAY["load_w"], abs=1e-3)


@pytest.mark.parametrize(
    ("status", "active"),
    [
        (0x1814, True),
        (0x1014, False),
        (0x1874, True),
        # Every bit but 11 set: only bit 11 decides.
        (0xFFFF_F7FF, False),
        (0x0000_0800, True),
    ],
)
def test_control_active_is_bit_11_of_the_word_swapped_status(status, active):
    result = parse_registers(_poll_blocks(dict(DAY, system_status=status)))

    assert result["local_system_status"] == status
    assert result["modbus_control_active"] is active


@pytest.mark.parametrize(
    ("status", "off_grid", "abnormal"),
    [
        (0x1014, False, False),
        (0x1015, True, False),
        (0x1016, False, True),
        (0x1017, True, True),
        # Every bit but 0 and 1 set: neither flag moves.
        (0xFFFF_FFFC, False, False),
    ],
)
def test_off_grid_is_bit_0_and_system_abnormal_is_bit_1_of_the_status(
    status, off_grid, abnormal
):
    result = parse_registers(_poll_blocks(dict(DAY, system_status=status)))

    assert result["local_off_grid"] is off_grid
    assert result["local_system_abnormal"] is abnormal


def test_the_live_device_readings_decode_to_the_five_new_values():
    # The status word as the device answered it with the other four readings.
    result = parse_registers(_poll_blocks(dict(DAY, system_status=0x1014)))

    assert result["local_off_grid"] is False
    assert result["local_system_abnormal"] is False
    assert result["batt_voltage_v"] == pytest.approx(52.305, abs=1e-3)
    # Positive is charge: the device was discharging, and no sign is flipped.
    assert result["batt_current_a"] == pytest.approx(-16.247, abs=1e-3)
    assert result["batt_temp_c"] == pytest.approx(33.0, abs=1e-3)
    assert result["ems_feed_power_limit_w"] == 7000


def test_bits_4_to_6_of_the_status_are_not_decoded_as_a_mode():
    # The device shows an undocumented value 7 in bits 4-6 while Backup Reserve
    # is above 0. It is not an error and must not change any key.
    plain = parse_registers(_poll_blocks(dict(DAY, system_status=0x1814)))
    with_reserve = parse_registers(_poll_blocks(dict(DAY, system_status=0x1874)))

    # The word differs in bits 4-6 only (1 against 7), so the word itself is the
    # only value that may differ: a mode decoded from those bits would show up
    # as a second one.
    changed = {key for key in plain if plain[key] != with_reserve[key]}
    assert set(with_reserve) == set(plain)
    assert changed == {"local_system_status"}
    assert with_reserve["modbus_control_active"] is True


@pytest.mark.parametrize("cut", ["first_block_absent", "first_block_old_size"])
def test_a_frame_without_the_status_registers_invents_no_control_state(cut):
    # Frame, not store: a frame that did not carry the word says nothing about
    # control, so there is no False to publish.
    blocks = _poll_blocks(DAY)
    if cut == "first_block_absent":
        del blocks[0x0206]
    else:
        # The first block as it was before the status was polled: nine registers.
        blocks[0x0206] = blocks[0x0206][: 2 * 9]

    result = parse_registers(blocks)

    assert "modbus_control_active" not in result
    assert "local_off_grid" not in result
    assert "local_system_abnormal" not in result
    assert "local_bms_connected" not in result
    assert "local_system_status" not in result
    # The rest of the frame is unharmed (negative control: the keys are absent
    # for the right reason only).
    assert result["ems_backup_ratio_pct"] == DAY["backup_ratio"]
    if cut == "first_block_old_size":
        assert result["home_w"] == pytest.approx(DAY["load_w"], abs=1e-3)


def test_the_first_poll_block_reaches_the_second_register_of_the_status():
    # 0x0211 is a UINT32: its second register is 0x0212, the last one of the block.
    start, count = POLL_BLOCKS[0]

    assert start == 0x0206
    assert start + count - 1 >= 0x0212


def test_the_first_poll_block_reaches_the_second_register_of_system_state_2():
    # 0x0213 is a UINT32: its second register is 0x0214, the last one of the block.
    start, count = POLL_BLOCKS[0]

    assert start + count - 1 >= 0x0214


@pytest.mark.parametrize(
    ("status", "connected"),
    [
        # The live word of the reference device: bit 12 set.
        (0x1014, True),
        (0x0014, False),
        # Every bit but 12 set: only bit 12 decides.
        (0xFFFF_EFFF, False),
        (0x0000_1000, True),
    ],
)
def test_bms_connected_is_bit_12_of_the_status(status, connected):
    result = parse_registers(_poll_blocks(dict(DAY, system_status=status)))

    assert result["local_bms_connected"] is connected


# The alert codes by bit, written out by hand from the protocol table (System
# State 2, 0x0213), independent of the parser's own tuple.
ALERT_BITS = [
    (0, "system_shutdown"),
    (1, "upgrade_shutdown"),
    (2, "epo_triggered"),
    (3, "low_power_mode"),
    (4, "fan_failure"),
    (5, "system_failure"),
    (6, "battery_reverse_connection"),
    (7, "battery_disconnected"),
    (8, "auxiliary_power_failure"),
    (9, "pcs_timeout"),
    (10, "pcs_failure"),
    (11, "igbt_self_test_failure"),
    (12, "high_temperature_protection"),
    (13, "battery_overheating"),
    (14, "ntc_circuit_failure"),
    (15, "system_reset"),
    (16, "hardware_version_error"),
    (17, "parallel_sync_error"),
    (18, "low_temperature_protection"),
    (19, "parallel_master_slave_conflict"),
    (20, "parallel_slave_setting_error"),
    (21, "parallel_inverter_error"),
    (22, "parallel_meter_fault"),
]


def _alerts(state2):
    return parse_registers(_poll_blocks(dict(DAY, system_state2=state2)))[
        "local_system_alerts"
    ]


def test_no_bit_set_in_system_state_2_reads_none():
    result = parse_registers(_poll_blocks(dict(DAY, system_state2=0)))

    assert result["local_system_state2"] == 0
    assert result["local_system_alerts"] == "none"


@pytest.mark.parametrize(("bit", "code"), ALERT_BITS)
def test_each_bit_of_system_state_2_has_its_own_code(bit, code):
    assert _alerts(1 << bit) == code


def test_the_alert_table_has_exactly_the_documented_23_bits():
    # Bit 22 is the last documented one; bit 23 is the first unknown one.
    assert [bit for bit, _ in ALERT_BITS] == list(range(23))
    assert _alerts(1 << 22) == "parallel_meter_fault"
    assert _alerts(1 << 23) == "bit_23"


def test_several_alerts_are_listed_in_bit_order_without_spaces():
    assert _alerts((1 << 13) | (1 << 4)) == "fan_failure,battery_overheating"


@pytest.mark.parametrize("state2", [(1 << 23) - 1, 0xFFFFFFFF])
def test_many_alerts_stay_within_a_state_and_count_the_rest(state2):
    # Home Assistant shows a state over 255 characters as unknown. The codes
    # run in bit order while they fit, and a closing +N counts the others.
    active = [code for bit, code in ALERT_BITS if (state2 >> bit) & 1] + [
        f"bit_{bit}" for bit in range(23, 32) if (state2 >> bit) & 1
    ]
    result = _alerts(state2)
    assert len(result) <= 255
    shown, _, hidden = result.rpartition(",+")
    assert shown.split(",") == active[: len(shown.split(","))]
    assert len(shown.split(",")) + int(hidden) == len(active)
    # No room was wasted: one more code would not have fitted.
    assert len(result) + len(active[len(shown.split(","))]) + 1 > 255


def test_alerts_that_fit_carry_no_count():
    ten = (1 << 10) - 1
    assert "+" not in _alerts(ten)
    assert _alerts(ten).split(",") == [code for _, code in ALERT_BITS[:10]]


@pytest.mark.parametrize(
    ("state2", "expected"),
    [
        (1 << 25, "bit_25"),
        (1 << 31, "bit_31"),
        # A known and an unknown alert together: both listed, in bit order.
        ((1 << 22) | (1 << 25), "parallel_meter_fault,bit_25"),
    ],
)
def test_a_bit_past_the_table_is_reported_by_its_number(state2, expected):
    assert _alerts(state2) == expected


def test_system_state_2_is_word_swapped_like_the_status():
    # Bit 4 is 0x00000010: the wire bytes of a word-swapped UINT32 are 00 10 00 00.
    # Read without the swap it would be bit 20 (parallel_slave_setting_error).
    result = parse_registers(_poll_blocks(dict(DAY, system_state2=1 << 4)))

    assert result["local_system_state2"] == 1 << 4
    assert result["local_system_alerts"] == "fan_failure"


@pytest.mark.parametrize("cut", ["block_absent", "block_old_size"])
def test_a_frame_without_system_state_2_invents_no_alert_state(cut):
    # Frame, not store: a frame that did not carry 0x0213 says nothing about
    # alerts, so there is no "none" to publish.
    blocks = _poll_blocks(DAY)
    if cut == "block_absent":
        del blocks[0x0206]
    else:
        # The first block as it was before 0x0213 was polled: thirteen registers.
        blocks[0x0206] = blocks[0x0206][: 2 * 13]

    result = parse_registers(blocks)

    assert "local_system_alerts" not in result
    assert "local_system_state2" not in result
    if cut == "block_old_size":
        # Negative control: the status word is inside the old block, so the
        # alerts are absent for the right reason only.
        assert result["local_bms_connected"] is True
        assert result["local_system_status"] == DAY["system_status"]


@pytest.mark.parametrize(
    ("offset", "key"),
    [
        (BACKUP_RATIO_OFFSET, "ems_backup_ratio_pct"),
        (BRIGHTNESS_OFFSET, "local_indicator_brightness_pct"),
    ],
)
def test_a_one_register_block_at_a_writable_offset_decodes_under_its_key(offset, key):
    # The read-back of a write hands the parser exactly this: one register at
    # the core's offset. The parser's own offsets must be those constants.
    result = parse_registers({offset: struct.pack(">H", 37)})

    assert result == {key: 37}


def test_the_per_pack_soc_registers_are_not_mapped():
    # Modbus holds the user-facing SoC, the cloud stream's packN_soc is the BMS
    # figure (up to five points apart), so the same key must not carry both.
    blocks = {
        **_poll_blocks(DAY),
        # batteries online 2, pack 1-3 SoC 95 / 95 / 0
        0x0820: struct.pack(">HHHH", 2, 95, 95, 0),
    }

    result = parse_registers(blocks)

    assert result["bp_online_sum"] == 2
    assert not [key for key in result if key.startswith("pack")]


@pytest.mark.parametrize("key", sorted(LIFETIME_COUNTER_KEYS))
@pytest.mark.parametrize("bad_reading", [0.0, -1.5])
def test_a_lifetime_counter_at_or_below_zero_is_dropped(key, bad_reading):
    # A counter that has run for years is never 0: it is a bad read, and as the
    # first published value it would register as a meter reset.
    field = KEY_TO_FIELD[key]

    result = parse_registers(_poll_blocks(dict(DAY, **{field: bad_reading})))

    assert key not in result
    assert set(result) == EXPECTED_KEYS - {key}


def test_the_lifetime_counters_are_exactly_the_five_energy_registers():
    expected = {
        "grid_import_lifetime_energy_kwh",
        "grid_export_lifetime_energy_kwh",
        "batt_charge_energy_kwh",
        "batt_discharge_energy_kwh",
        "solar_lifetime_energy_kwh",
    }

    assert expected == LIFETIME_COUNTER_KEYS


def test_device_info_reads_identity_with_firmware_as_a_word_swapped_uint32():
    info = parse_device_info(_setup_blocks(DEVICE))

    assert info == {
        "protocol_version": 1,
        "product_category": 1,
        "product_number": 1,
        "serial": "HJ31DUMMY0000001",
        "firmware": "5.1.37.10",
    }
    assert is_supported_device(info)
    assert not is_supported_device({**info, "product_number": 3})
    assert not is_supported_device({**info, "product_category": 2})


def test_blocks_stay_within_the_device_register_limit():
    # The device refuses a read of more than 125 registers: measured 2026-10-01,
    # 154 registers at offset 0x0840 answered Modbus exception 03.
    for start, count in SETUP_BLOCKS + POLL_BLOCKS:
        assert 1 <= count <= 125, hex(start)
