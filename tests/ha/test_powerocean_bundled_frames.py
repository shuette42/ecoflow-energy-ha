"""Coordinator-level regression test for bundled PowerOcean frames."""

from custom_components.ecoflow_energy.coordinator.mqtt_ingest import MqttIngestMixin
from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1EmsHeartbeat,
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
