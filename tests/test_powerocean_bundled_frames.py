"""Regression tests for bundled and incremental PowerOcean protobuf frames."""

from ecoflow_energy.ecoflow.parsers.powerocean_proto import flatten_heartbeat
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


def _build_header(
    cmd_func: int,
    cmd_id: int,
    pdata: bytes,
    *,
    seq: int | None = None,
    encrypted: bool = False,
) -> bytes:
    """Build one HeaderMessage repeated-header entry."""
    if encrypted:
        assert seq is not None
        key = seq & 0xFF
        pdata = bytes(value ^ key for value in pdata)

    header = bytearray()
    if pdata:
        header.extend(encode_field_bytes(1, pdata))
    if encrypted:
        header.extend(encode_field_varint(6, 1))
    header.extend(encode_field_varint(8, cmd_func))
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
