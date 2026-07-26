"""Regression tests for bundled and incremental PowerOcean protobuf frames."""

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ecoflow_energy.ecoflow.parsers.powerocean_proto import flatten_heartbeat
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
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
