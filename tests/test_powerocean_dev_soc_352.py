"""PowerOcean Plus (R372) cmd 96/8: field 352 `dev_soc`, the solar surplus read-back.

The payloads in the fixture are real 96/8 reports from a reporter's diagnostics
(issue 631), decrypted, without header and without serial number. Field 352
moves in the same second as a surplus change in the app and stays put on a
backup reserve change, so it is the device's confirmation of the surplus value.
"""

import json
from pathlib import Path
from typing import Any

from ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from ecoflow_energy.ecoflow.parsers.powerocean_proto import remap_bp_keys
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import JTS1EmsChangeReport
from ecoflow_energy.ecoflow.proto.runtime import decode_proto_runtime_headers
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "powerocean_plus_r372_96_8_dev_soc.json"
_KEY = "ems_change_dev_soc_pct"


def _frame_pdata(index: int) -> bytes:
    frames = json.loads(_FIXTURE.read_text())["frames"]
    (frame,) = [f for f in frames if f["index"] == index]
    assert (frame["cmd_func"], frame["cmd_id"]) == (96, 8)
    return bytes.fromhex(frame["pdata_hex"])


def _sensor_keys(pdata: bytes) -> dict[str, Any]:
    """Run a 96/8 payload through the runtime decoder and the sensor remap."""
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 96)
        + encode_field_varint(9, 8)
    )
    frame = encode_field_bytes(1, header)
    decoded = decode_proto_runtime_headers(frame, device_type=DEVICE_TYPE_POWEROCEAN)
    (result,) = [r for r in decoded if r.parse_path == "typed_runtime:ems_change"]
    raw = {k: v for k, v in result.mapped.items() if not k.startswith("_")}
    return remap_bp_keys(raw, {}, "TEST0000000000XX")


def test_r372_mini_frame_reads_only_dev_soc() -> None:
    """The surplus-70 mini frame yields the read-back and nothing else."""
    assert _sensor_keys(_frame_pdata(55)) == {_KEY: 70.0}


def test_r372_second_mini_frame_reads_100() -> None:
    """Setting the surplus back to 100 is read back as 100."""
    assert _sensor_keys(_frame_pdata(63)) == {_KEY: 100.0}


def test_r372_get_reply_reads_dev_soc_and_keeps_existing_keys() -> None:
    """The full get_reply gives 100 and the keys that already arrived."""
    result = _sensor_keys(_frame_pdata(0))

    assert result[_KEY] == 100.0
    assert result["ems_discharge_lower_limit_pct"] == 30.0


def test_r372_reserve_change_frame_carries_no_dev_soc() -> None:
    """A backup reserve change leaves 352 out, so no surplus value appears."""
    result = _sensor_keys(_frame_pdata(17))

    assert _KEY not in result
    assert result == {"ems_discharge_lower_limit_pct": 71.0}


def test_dev_soc_zero_survives() -> None:
    """A real 0 stays a 0 and is not dropped as absent."""
    result = _sensor_keys(JTS1EmsChangeReport(ems_change_dev_soc=0).SerializeToString())

    assert _KEY in result
    assert result[_KEY] == 0.0
