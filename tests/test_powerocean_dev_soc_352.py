"""PowerOcean Plus (R372) cmd 96/8: field 352 `dev_soc`, the solar surplus read-back.

The payloads in the fixture are real 96/8 reports from a reporter's diagnostics
(issue 631), decrypted, without header and without serial number. Field 352
moves in the same second as a surplus change in the app and stays put on a
backup reserve change, so it is the device's report of the surplus value.
"""

import json
from pathlib import Path
from typing import Any

from ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from ecoflow_energy.ecoflow.parsers.powerocean_proto import remap_bp_keys
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1EmsChangeReport,
    JTS1EmsParamChangeReport,
)
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


_FIXTURES = Path(__file__).parent / "fixtures"


def _envelope(pdata: bytes, cmd_func: int, cmd_id: int) -> bytes:
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, cmd_func)
        + encode_field_varint(9, cmd_id)
    )
    return encode_field_bytes(1, header)


def _surplus_sources(frames: list[bytes]) -> set[str]:
    """Which surplus fields a device's frames carry: 96/13 `dev_soc`, 96/8 352."""
    sources: set[str] = set()
    for raw in frames:
        try:
            decoded = decode_proto_runtime_headers(
                raw, device_type=DEVICE_TYPE_POWEROCEAN
            )
        except Exception:  # a frame of another device family; not ours to read
            continue
        for result in decoded:
            if (
                result.parse_path == "typed_runtime:ems_param_change"
                and "ems_app_surplus_pct" in result.mapped
            ):
                sources.add("96/13")
            if (
                result.parse_path == "typed_runtime:ems_change"
                and "ems_change_dev_soc" in result.mapped
            ):
                sources.add("96/8:352")
    return sources


def _fixture_frames() -> dict[str, list[bytes]]:
    """Every fixture file that holds frames, as bytes per file (one device each)."""
    found: dict[str, list[bytes]] = {}
    for path in sorted(_FIXTURES.rglob("*.json")):
        data = json.loads(path.read_text())
        frames = data.get("frames") if isinstance(data, dict) else None
        if not isinstance(frames, list):
            continue
        raw: list[bytes] = []
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            if "pdata_hex" in frame and "cmd_func" in frame:
                raw.append(
                    _envelope(
                        bytes.fromhex(frame["pdata_hex"]),
                        frame["cmd_func"],
                        frame["cmd_id"],
                    )
                )
            elif isinstance(frame.get("hex"), str):
                try:
                    raw.append(bytes.fromhex(frame["hex"]))
                except ValueError:
                    continue
        if raw:
            found[str(path.relative_to(_FIXTURES))] = raw
    return found


def test_surplus_walk_flags_a_device_with_both_sources() -> None:
    """Positive control: the detector fires on a device that sends both."""
    both = [
        _envelope(JTS1EmsParamChangeReport(dev_soc=40).SerializeToString(), 96, 13),
        _envelope(
            JTS1EmsChangeReport(ems_change_dev_soc=70).SerializeToString(), 96, 8
        ),
    ]

    assert _surplus_sources(both) == {"96/13", "96/8:352"}
    assert _surplus_sources(both[:1]) == {"96/13"}
    assert _surplus_sources(both[1:]) == {"96/8:352"}


def test_no_fixture_carries_both_surplus_sources() -> None:
    """No recorded device sends 96/13 `dev_soc` and 96/8 field 352 together.

    The runtime flag `surplus_sources_both` is the detector for a device in the
    field. This walk covers only what the fixtures record, and fails when a new
    recording shows both, so the precedence rule is looked at again.
    """
    by_file = {name: _surplus_sources(raw) for name, raw in _fixture_frames().items()}

    # The walk reads the R372 recording, so a quiet result is not an empty walk.
    assert "96/8:352" in by_file["powerocean_plus_r372_96_8_dev_soc.json"]
    assert [name for name, found in by_file.items() if len(found) == 2] == []
