"""Regression tests for bundled and incremental PowerOcean protobuf frames."""

import base64
import re
import struct
from pathlib import Path
from unittest.mock import patch

import pytest

from ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    flatten_heartbeat,
    parse_powerglow_telemetry,
    remap_bp_keys,
    remap_ems_state_keys,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1BpHeartbeatReport,
    JTS1EmsChangeReport,
    JTS1EmsHeartbeat,
    JTS1EmsPVInvEnergyStreamReport,
    JTS1EnergyStreamReport,
)
from ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_frame,
    decode_proto_runtime_headers,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

_R374_GET_ALL_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "powerocean"
    / "r374_get_all_masked.bin"
)


def _build_header(
    cmd_func: int,
    cmd_id: int | None,
    pdata: bytes,
    *,
    seq: int | None = None,
    encrypted: bool = False,
    xor_payload: bool | None = None,
) -> bytes:
    """Build one HeaderMessage repeated-header entry.

    `xor_payload` defaults to `encrypted` and can be set to False to produce a
    header that claims encryption while carrying plaintext.
    """
    if xor_payload is None:
        xor_payload = encrypted
    if xor_payload:
        assert seq is not None
        key = seq & 0xFF
        pdata = bytes(value ^ key for value in pdata)

    header = bytearray()
    if pdata:
        header.extend(encode_field_bytes(1, pdata))
    if encrypted:
        header.extend(encode_field_varint(6, 1))
    header.extend(encode_field_varint(8, cmd_func))
    if cmd_id is not None:
        header.extend(encode_field_varint(9, cmd_id))
    if seq is not None:
        header.extend(encode_field_varint(14, seq))
    return encode_field_bytes(1, bytes(header))


def _build_bundle(*headers: bytes) -> bytes:
    return b"".join(headers)


def _field_float(field_number: int, value: float) -> bytes:
    """Build one protobuf fixed32 float field for a hand-made fixture."""
    return bytes([(field_number << 3) | 5]) + struct.pack("<f", value)


def test_powerglow_enhanced_reports_map_confirmed_fields() -> None:
    """PowerGlow is an accessory report in the PowerOcean MQTT envelope."""
    rod_serial = b"HF33TEST00000001"
    parameter_report = b"".join(
        (
            encode_field_bytes(1, rod_serial),
            _field_float(6, 58.0),
        )
    )
    energy_item = b"".join(
        (
            encode_field_bytes(1, rod_serial),
            _field_float(2, 1750.0),
        )
    )
    frame = _build_bundle(
        _build_header(212, 8, parameter_report),
        _build_header(212, 33, encode_field_bytes(1, energy_item)),
    )
    headers, _ = decode_header_message(frame)

    parsed = parse_powerglow_telemetry(headers)

    assert parsed == {
        "heating_rod_power_w": 1750.0,
        "heating_rod_water_temp_c": 58.0,
    }


def test_powerglow_enhanced_idle_report_is_zero_watts() -> None:
    """A serial-only proto3 stream item means the rod is explicitly idle."""
    item = encode_field_bytes(1, b"HF33TEST00000001")
    headers, _ = decode_header_message(
        _build_header(212, 33, encode_field_bytes(1, item))
    )

    assert parse_powerglow_telemetry(headers) == {"heating_rod_power_w": 0.0}


def test_runtime_decodes_every_header_with_its_own_sequence() -> None:
    """Bundled headers are independently decrypted and decoded."""
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0,
        sys_grid_pwr=-6280.0,
        mppt_pwr=6730.0,
        bp_pwr=0.0,
        bp_soc=0,
    )
    heartbeat = JTS1EmsHeartbeat(pcs_ac_freq=50.0)
    mppt_a = heartbeat.mppt_heart_beat.add()
    pv1 = mppt_a.mppt_pv.add()
    pv1.vol = 500.0
    pv1.amp = 7.0
    pv1.pwr = 3500.0
    mppt_b = heartbeat.mppt_heart_beat.add()
    pv2 = mppt_b.mppt_pv.add()
    pv2.vol = 490.0
    pv2.amp = 3.6
    pv2.pwr = 1764.0

    bundle = _build_bundle(
        # Unknown first header proves decoding is not anchored to header 0.
        _build_header(241, 36, b"\x08\x01", seq=1, encrypted=True),
        _build_header(
            96,
            33,
            stream.SerializeToString(),
            seq=0x1222,
            encrypted=True,
        ),
        # The real R374 sends an empty (96,39) companion header.
        _build_header(96, 39, b"", seq=0x1333, encrypted=True),
        _build_header(
            96,
            1,
            heartbeat.SerializeToString(),
            seq=0x1444,
            encrypted=True,
        ),
    )

    decoded = decode_proto_runtime_headers(bundle)

    assert [item.parse_path for item in decoded] == [
        "typed_runtime:energy_stream_report",
        "typed_runtime:ems_heartbeat",
    ]
    assert decoded[0].parse_reason_code == "typed_source_header_pdata_decrypted"
    assert decoded[0].mapped["solar"] == 6730.0
    assert decoded[0].mapped["home_direct"] == 450.0
    assert decoded[0].mapped["grid_raw_f2"] == -6280.0
    assert decoded[1].parse_reason_code == "typed_source_header_pdata_decrypted"
    assert decoded[1].mapped["pcs_ac_freq"] == 50.0
    assert len(decoded[1].mapped["mppt_heart_beat"]) == 2


def test_single_header_runtime_api_remains_compatible() -> None:
    """Existing single-header PowerOcean frames keep the legacy API shape."""
    stream = JTS1EnergyStreamReport(mppt_pwr=1234.0, sys_load_pwr=200.0)
    frame = _build_header(96, 33, stream.SerializeToString())

    result = decode_proto_runtime_frame(frame)

    assert len(result.headers) == 1
    assert result.parse_path == "typed_runtime:energy_stream_report"
    assert result.mapped["solar"] == 1234.0
    assert result.mapped["home_direct"] == 200.0


def test_empty_known_companion_header_is_guarded_not_decoded() -> None:
    """A zero-length known pdata does not create a bogus protobuf result."""
    frame = _build_header(96, 39, b"", seq=77, encrypted=True)

    assert decode_proto_runtime_headers(frame) == []
    result = decode_proto_runtime_frame(frame)
    assert result.parse_path == "typed_runtime:guarded_no_inner_payload"
    assert result.parse_reason_code == "typed_inner_payload_missing"


def test_pv_inverter_stream_has_sensor_key() -> None:
    """A non-empty (96,39) remains useful for variants that send it."""
    message = JTS1EmsPVInvEnergyStreamReport(pv_inv_pwr=987.0)
    frame = _build_header(96, 39, message.SerializeToString())

    result = decode_proto_runtime_headers(frame)[0]

    assert result.mapped["_is_pv_inv_energy_stream"] is True
    assert result.mapped["pv_inverter_power_w"] == 987.0


def test_heartbeat_flattens_all_mppt_containers_and_zero_fills() -> None:
    """PowerOcean Plus exposes up to four PV inputs across several containers."""
    raw = {
        "mppt_heart_beat": [
            {
                "mppt_pv": [
                    {"pwr": 3529.0, "vol": 501.0, "amp": 7.04},
                    # Existing entry with zero pwr/vol omitted by proto3.
                    {"amp": 0.02},
                ]
            },
            {
                "mppt_pv": [
                    {"pwr": 1772.0, "vol": 492.0, "amp": 3.60},
                    {"pwr": 1436.0, "vol": 488.0, "amp": 2.94},
                ]
            },
        ]
    }

    result = flatten_heartbeat(raw)

    assert result["mppt_pv1_power_w"] == 3529.0
    assert result["mppt_pv2_power_w"] == 0.0
    assert result["mppt_pv2_voltage_v"] == 0.0
    assert result["mppt_pv2_current_a"] == 0.02
    assert result["mppt_pv3_power_w"] == 1772.0
    assert result["mppt_pv4_power_w"] == 1436.0


def test_heartbeat_phase_snapshot_zero_fills_and_keeps_extended_power() -> None:
    """Present phase messages clear omitted zero scalars and expose all powers."""
    raw = {
        "pcs_a_phase": {
            "vol": 230.1,
            "amp": 5.9,
            "act_pwr": -1350.2,
            "react_pwr": 42.0,
            "apparent_pwr": 1351.0,
        },
        # Existing phase with only voltage: all omitted values are explicit zero.
        "pcs_b_phase": {"vol": 229.8},
        "pcs_c_phase": {"vol": 230.3, "act_pwr": -1363.4},
    }

    result = flatten_heartbeat(raw)

    assert result["grid_phase_a_active_power_w"] == -1350.2
    assert result["grid_phase_a_reactive_power_var"] == 42.0
    assert result["grid_phase_a_apparent_power_va"] == 1351.0
    assert result["grid_phase_b_current_a"] == 0.0
    assert result["grid_phase_b_active_power_w"] == 0.0
    assert result["grid_phase_c_current_a"] == 0.0
    assert result["grid_status"] == "ok"


def test_load_info_without_current_does_not_zero_the_phase_readings() -> None:
    """A container that omits current and power must not overwrite one that has them.

    Values captured from a PowerOcean HJ31 heartbeat: pcs_load_info carries only
    voltage and frequency, while pcs_a/b/c_phase carries the full set. Filling
    current and active power from the first container reported 0 W while the
    device was exporting more than 200 W on phase C.
    """
    raw = {
        "pcs_a_phase": {
            "vol": 234.89375,
            "amp": 1.2506706,
            "act_pwr": -10.124375,
            "react_pwr": 277.30704,
            "apparent_pwr": 277.4918,
        },
        "pcs_b_phase": {
            "vol": 234.12477,
            "amp": 0.75025797,
            "act_pwr": -118.80214,
            "react_pwr": 123.4285,
            "apparent_pwr": 171.31416,
        },
        "pcs_c_phase": {
            "vol": 235.07158,
            "amp": 1.1480076,
            "act_pwr": -207.72481,
            "react_pwr": 158.31601,
            "apparent_pwr": 261.17725,
        },
        "pcs_load_info": [
            {"vol": 234.89375, "freq": 50.00675},
            {"vol": 234.12477, "freq": 50.006668},
            {"vol": 235.07158, "freq": 50.006954},
        ],
    }

    result = flatten_heartbeat(raw)

    assert result["grid_phase_a_current_a"] == pytest.approx(1.2506706)
    assert result["grid_phase_b_current_a"] == pytest.approx(0.75025797)
    assert result["grid_phase_c_current_a"] == pytest.approx(1.1480076)
    assert result["grid_phase_a_active_power_w"] == pytest.approx(-10.124375)
    assert result["grid_phase_b_active_power_w"] == pytest.approx(-118.80214)
    assert result["grid_phase_c_active_power_w"] == pytest.approx(-207.72481)
    assert result["grid_phase_c_apparent_power_va"] == pytest.approx(261.17725)
    assert result["grid_status"] == "ok"


def test_load_info_only_phase_reports_zero_and_no_extended_power() -> None:
    """A phase described only by pcs_load_info still clears omitted scalars.

    Reactive and apparent power exist only in pcs_a/b/c_phase, so a phase that
    container never described must not gain fabricated values for them.
    """
    raw = {"pcs_load_info": [{"vol": 231.4, "freq": 50.01}]}

    result = flatten_heartbeat(raw)

    assert result["grid_phase_a_voltage_v"] == pytest.approx(231.4)
    assert result["grid_phase_a_current_a"] == 0.0
    assert result["grid_phase_a_active_power_w"] == 0.0
    assert "grid_phase_a_reactive_power_var" not in result
    assert "grid_phase_a_apparent_power_va" not in result


def test_phase_container_wins_over_load_info_on_conflict() -> None:
    """pcs_a/b/c_phase is authoritative when both containers report a scalar.

    Only the phase container models a phase electrically, so it decides. The
    observed hardware never sends a conflict, which is why this has to be
    pinned by a test rather than left to the field order.
    """
    raw = {
        "pcs_load_info": [{"vol": 230.0, "amp": 9.9, "freq": 50.0, "pwr": 2200.0}],
        "pcs_a_phase": {"vol": 231.0, "amp": 1.1, "act_pwr": -207.0},
    }

    result = flatten_heartbeat(raw)

    assert result["grid_phase_a_voltage_v"] == pytest.approx(231.0)
    assert result["grid_phase_a_current_a"] == pytest.approx(1.1)
    assert result["grid_phase_a_active_power_w"] == pytest.approx(-207.0)


def test_container_without_any_scalar_still_clears_the_phase() -> None:
    """A container whose scalars are all at their proto3 default writes zeros.

    This is the dead-grid case. The phase entry is created before any field is
    read, so an empty container still clears instead of leaving a stale value.
    """
    from_phase = flatten_heartbeat({"pcs_a_phase": {}})
    assert from_phase["grid_phase_a_voltage_v"] == 0.0
    assert from_phase["grid_phase_a_current_a"] == 0.0
    assert from_phase["grid_phase_a_active_power_w"] == 0.0
    assert from_phase["grid_phase_a_reactive_power_var"] == 0.0
    assert from_phase["grid_phase_a_apparent_power_va"] == 0.0
    assert "grid_status" not in from_phase

    from_load = flatten_heartbeat({"pcs_load_info": [{}]})
    assert from_load["grid_phase_a_voltage_v"] == 0.0
    assert from_load["grid_phase_a_current_a"] == 0.0
    assert from_load["grid_phase_a_active_power_w"] == 0.0
    assert "grid_phase_a_reactive_power_var" not in from_load


def test_non_numeric_phase_scalar_is_rejected() -> None:
    """MessageToDict can render a float as "NaN"; it must not reach a sensor."""
    result = flatten_heartbeat({"pcs_a_phase": {"vol": "NaN", "amp": 1.2}})

    assert result["grid_phase_a_voltage_v"] == 0.0
    assert result["grid_phase_a_current_a"] == pytest.approx(1.2)
    assert "grid_status" not in result


def test_multi_header_frame_with_outer_payload_still_decodes() -> None:
    """Several headers plus one outer payload field keep the typed decode.

    Only the payload field carries data here, so the per-header pass finds
    nothing and the legacy whole-frame path has to produce the keys.
    """
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0, sys_grid_pwr=-6280.0, mppt_pwr=6730.0, bp_pwr=0.0
    )
    frame = (
        _build_header(96, 33, b"")
        + _build_header(96, 1, b"")
        + encode_field_bytes(2, stream.SerializeToString())
    )

    result = decode_proto_runtime_frame(frame)

    assert result.parse_path == "typed_runtime:energy_stream_report"
    assert result.parse_reason_code == "typed_source_payload_field"
    assert {"solar", "home_direct", "grid_raw_f2", "batt_pb"} <= set(result.mapped)
    assert result.mapped["solar"] == 6730.0
    assert result.mapped["grid_raw_f2"] == -6280.0


def test_invalid_pdata_hex_falls_back_to_full_frame() -> None:
    """Unusable pdata hex retries the whole frame instead of dropping keys."""
    stream = JTS1EnergyStreamReport(mppt_pwr=4321.0, sys_load_pwr=120.0)
    frame = stream.SerializeToString()

    with patch(
        "ecoflow_energy.ecoflow.proto.runtime.decode_header_message",
        return_value=([{"cmd_func": 96, "cmd_id": 33, "pdata": "zznothex"}], None),
    ):
        result = decode_proto_runtime_frame(frame)

    assert result.parse_reason_code == "typed_source_full_frame_invalid_pdata"
    assert result.parse_path == "typed_runtime:energy_stream_report"
    assert result.mapped["solar"] == 4321.0


def test_enc_type_flag_with_plaintext_pdata_still_decodes() -> None:
    """A set encryption flag on plaintext pdata must not cost every key."""
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0, sys_grid_pwr=-6280.0, mppt_pwr=6730.0
    )
    frame = _build_header(
        96,
        33,
        stream.SerializeToString(),
        seq=0x1222,
        encrypted=True,
        xor_payload=False,
    ) + _build_header(96, 39, b"", seq=0x1333)

    decoded = decode_proto_runtime_headers(frame)

    assert len(decoded) == 1
    assert decoded[0].parse_path == "typed_runtime:energy_stream_report"
    assert decoded[0].mapped["solar"] == 6730.0
    assert decoded[0].mapped["home_direct"] == 450.0
    assert decoded[0].mapped["grid_raw_f2"] == -6280.0


def test_cmd_func_and_cmd_id_come_from_the_same_header() -> None:
    """A command tuple is never assembled from two different headers."""
    other = JTS1EmsPVInvEnergyStreamReport(pv_inv_pwr=987.0)
    stream = JTS1EnergyStreamReport(mppt_pwr=6730.0, sys_load_pwr=450.0)

    frame = _build_header(96, None, other.SerializeToString()) + _build_header(
        96, 33, stream.SerializeToString()
    )

    decoded = decode_proto_runtime_headers(frame)

    assert len(decoded) == 1
    assert decoded[0].parse_path == "typed_runtime:energy_stream_report"
    assert decoded[0].mapped["solar"] == 6730.0
    assert decoded[0].mapped["home_direct"] == 450.0


def test_partial_phase_message_does_not_flip_grid_status() -> None:
    """A phase message without a voltage leaves grid_status alone."""
    result = flatten_heartbeat({"pcs_a_phase": {"act_pwr": -1350.0}})

    assert result["grid_phase_a_active_power_w"] == -1350.0
    assert "grid_status" not in result


def test_real_r374_get_all_fixture_decodes_all_supported_headers() -> None:
    """The masked real-device bundle keeps all 19 independent headers."""
    frame = _R374_GET_ALL_FIXTURE.read_bytes()

    assert len(frame) == 2483
    assert re.search(rb"R[0-9A-Z]{15}", frame) is None
    assert re.search(rb"(?<![0-9])[0-9]{16,24}(?![0-9])", frame) is None

    headers, payload = decode_header_message(frame)

    assert payload is None
    assert len(headers) == 19
    command_pairs = {
        (header.get("cmd_func"), header.get("cmd_id"))
        for header in headers
    }
    assert {(96, 1), (96, 8), (96, 33), (96, 39)} <= command_pairs

    decoded = decode_proto_runtime_headers(frame)
    parse_paths = {result.parse_path for result in decoded}

    assert {
        "typed_runtime:ems_heartbeat",
        "typed_runtime:ems_change",
        "typed_runtime:energy_stream_report",
    } <= parse_paths
    assert len(decode_proto_runtime_frame(frame).headers) == 19


def _ems_state_keys(frame: bytes) -> dict[str, object]:
    """Decode a bundle and return the cmd_id=17 report as sensor keys."""
    for result in decode_proto_runtime_headers(frame):
        if result.parse_path == "typed_runtime:ems_state":
            raw = {
                key: value
                for key, value in result.mapped.items()
                if not key.startswith("_")
            }
            return remap_ems_state_keys(raw)
    raise AssertionError("no cmd_id=17 header in frame")


def test_r374_fixture_carries_a_cmd_17_report() -> None:
    """The Plus bundle holds cmd_id=17 next to cmd_id=8, with no overlap.

    Both ids decode as the same message and carry disjoint fields. Before
    cmd_id=17 was registered every field in it was dropped, including the
    whole fault and arc-fault block.
    """
    frame = _R374_GET_ALL_FIXTURE.read_bytes()

    paths = [result.parse_path for result in decode_proto_runtime_headers(frame)]

    assert "typed_runtime:ems_state" in paths
    assert "typed_runtime:ems_change" in paths


def test_cmd_17_exposes_the_arc_fault_and_warning_block() -> None:
    """AFCI flags, MPPT warnings and the self-check states reach sensors."""
    sensors = _ems_state_keys(_R374_GET_ALL_FIXTURE.read_bytes())

    assert sensors["afci_fault_ch1"] == 0.0
    assert sensors["afci_fault_ch2"] == 0.0
    assert sensors["afci_self_test_result"] == 0.0
    assert sensors["mppt1_warning_code"] == 0.0
    assert sensors["mppt2_warning_code"] == 0.0
    assert sensors["battery_line_off"] == 0.0
    assert sensors["battery_relay_fault"] == 0.0
    assert sensors["ems_self_check_state"] == 7.0
    assert sensors["sys_calibration_state"] == 0.0
    assert sensors["parallel_mode"] == 0.0


def test_cmd_17_reports_run_state_and_connectivity() -> None:
    """Kept because the rest of the same bundle corroborates both."""
    sensors = _ems_state_keys(_R374_GET_ALL_FIXTURE.read_bytes())

    assert sensors["pcs_run_state"] == "running"
    assert sensors["wifi_status"] == "connected"
    assert sensors["cellular_status"] == "disconnected"


def test_cmd_17_does_not_write_the_grid_and_battery_state_sensors() -> None:
    """Its values for those contradict the rest of the same bundle.

    The phase containers in this bundle report 237 V on all three phases
    and -3725 W of export, while cmd_id=17 says `sys_grid_sta = 0` and
    `bp_chg_dsg_sta = 2`. Under the cmd_id=8 mapping that would publish
    "grid not detected" and "discharging" for a grid-exporting unit whose
    battery power is zero, so cmd_id=17 does not own these keys.
    """
    sensors = _ems_state_keys(_R374_GET_ALL_FIXTURE.read_bytes())

    for key in (
        "grid_status",
        "batt_charge_discharge_state",
        "ems_work_state",
        "bp_online_sum",
        "soc_pct",
    ):
        assert key not in sensors


def test_cmd_17_does_not_write_the_lifetime_energy_counters() -> None:
    """Both are 0 in every observed frame, on a total_increasing sensor."""
    sensors = _ems_state_keys(_R374_GET_ALL_FIXTURE.read_bytes())

    assert "batt_charge_energy_kwh" not in sensors
    assert "batt_discharge_energy_kwh" not in sensors


def test_zero_lifetime_energy_total_is_not_published() -> None:
    """A zero counter is "nothing to report", not a meter reading.

    Home Assistant reads a 0 on a total_increasing sensor as a meter reset
    and books the whole standing total a second time.
    """
    report = JTS1EmsChangeReport(bp_total_chg_energy=0, bp_total_dsg_energy=0)
    frame = _build_bundle(_build_header(96, 8, report.SerializeToString()))

    sensors = _ems_change_keys(frame)

    assert "batt_charge_energy_kwh" not in sensors
    assert "batt_discharge_energy_kwh" not in sensors


def test_non_zero_lifetime_energy_total_is_still_converted() -> None:
    """The guard drops zeros only - a real counter still becomes kWh."""
    report = JTS1EmsChangeReport(
        bp_total_chg_energy=12_500,
        bp_total_dsg_energy=9_750,
    )
    frame = _build_bundle(_build_header(96, 8, report.SerializeToString()))

    sensors = _ems_change_keys(frame)

    assert sensors["batt_charge_energy_kwh"] == 12.5
    assert sensors["batt_discharge_energy_kwh"] == 9.75


def _ems_change_keys(frame: bytes) -> dict[str, object]:
    """Decode a bundle and return the cmd_id=8 report as sensor keys."""
    for result in decode_proto_runtime_headers(frame):
        if result.parse_path == "typed_runtime:ems_change":
            raw = {
                key: value
                for key, value in result.mapped.items()
                if not key.startswith("_")
            }
            return remap_bp_keys(raw, {}, "R374TEST00000001")
    raise AssertionError("no cmd_id=8 header in frame")


def _inventory_pdata(
    ems_sn: bytes,
    pcs_sn: bytes,
    *pack_sns: bytes,
) -> bytes:
    """Build a module inventory payload from literal wire field numbers.

    Deliberately not built from `JTS1ErrorChangeReport`. A payload serialized
    by the same binding it is decoded with proves only that the binding agrees
    with itself: renumber a field in `ecocharge.proto` and both sides move
    together, so the assertions stay green while the wire meaning is gone.
    The field numbers here are literals taken from the observed frame layout -
    1 for the EMS module, 2 for the PCS module, 3 once per battery pack.
    """
    parts = [
        encode_field_bytes(1, encode_field_bytes(1, ems_sn)),
        encode_field_bytes(2, encode_field_bytes(1, pcs_sn)),
    ]
    parts.extend(
        encode_field_bytes(3, encode_field_bytes(1, sn)) for sn in pack_sns
    )
    return b"".join(parts)


def _b64(raw: bytes) -> str:
    """Return what MessageToDict renders a `bytes` field as."""
    return base64.b64encode(raw).decode()


def test_module_inventory_decodes_every_serial_in_its_role() -> None:
    """cmd_id=3 names the EMS module, the PCS module and both packs, in order.

    The observed HJ31 payload is 80 bytes: four 16-character serials, each
    wrapped in its own sub-message, with field 3 appearing twice. Field 3 is
    declared repeated for exactly that reason - the command family puts it in
    a oneof, where a second occurrence would overwrite the first.
    """
    payload = _inventory_pdata(
        b"EMSMODULEFAKE001",
        b"PCSMODULEFAKE002",
        b"BPTESTPACK000001",
        b"BPTESTPACK000002",
    )
    assert len(payload) == 80

    decoded = decode_proto_runtime_headers(_build_header(96, 3, payload))

    assert len(decoded) == 1
    assert decoded[0].parse_path == "typed_runtime:error_change"
    mapped = decoded[0].mapped
    assert mapped["_is_error_change"] is True
    assert mapped["ems_err_code"] == {"module_sn": _b64(b"EMSMODULEFAKE001")}
    assert mapped["pcs_err_code"] == {"module_sn": _b64(b"PCSMODULEFAKE002")}
    assert mapped["bp_err_code"] == [
        {"module_sn": _b64(b"BPTESTPACK000001")},
        {"module_sn": _b64(b"BPTESTPACK000002")},
    ]







def test_cmd_8_now_surfaces_sg_ready_and_the_battery_limit_reason() -> None:
    """Three fields the device always sent and the schema never decoded."""
    sensors = _ems_change_keys(_R374_GET_ALL_FIXTURE.read_bytes())

    assert sensors["ems_sg_ready_enabled"] == 0.0
    assert sensors["ems_sg_ready_state"] == 0.0
    assert sensors["battery_limit_reason"] == 0.0
