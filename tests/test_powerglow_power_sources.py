"""Where a PowerGlow heating rod's power came from: PV, battery, grid (#588,
PLAN-217).

The fixture `tests/fixtures/powerglow/hf33_power_source_frames.json` holds three
`212/33` reports from a reporter download of 2026-10-23, the rod's serial
masked as sixteen `X`:

- `serial_only` carries field 1 alone: the rod sent no reading.
- `pv_battery_grid` carries 2=152, 4=116, 5=33, 6=3. The app showed PV 116 W,
  battery 33 W, grid 3 W and rod 152 W at that moment.
- `pv_battery_no_grid` carries 2=153, 4=112, 5=41 and no field 6: the shares
  add up to the total, so the missing grid share is 0 W.

Every value here is decoded from the frame's hex through the real runtime
decode path, never from a hand-built dict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_heating_rod_keys,
    remap_heating_rod_power_source_keys,
)
from ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "powerglow" / "hf33_power_source_frames.json"
)

PV = "heating_rod_power_from_pv_w"
BATTERY = "heating_rod_power_from_battery_w"
GRID = "heating_rod_power_from_grid_w"
SOURCE_KEYS = {PV, BATTERY, GRID}


def _frame(name: str) -> bytes:
    frames = json.loads(FIXTURE.read_text())["frames"]
    return bytes.fromhex(next(f for f in frames if f["name"] == name)["hex"])


def _decode(name: str) -> dict[str, Any]:
    """The remapped keys of the frame's single 212/33 report."""
    reports = [
        d
        for d in decode_proto_runtime_headers(
            _frame(name), device_type=DEVICE_TYPE_POWEROCEAN
        )
        if d.mapped.get("_is_heating_rod_power_source")
    ]
    assert len(reports) == 1
    raw = {k: v for k, v in reports[0].mapped.items() if not k.startswith("_")}
    return remap_heating_rod_power_source_keys(raw)


def test_a_report_with_all_three_shares_publishes_them_as_floats() -> None:
    result = _decode("pv_battery_grid")

    assert result == {PV: 116.0, BATTERY: 33.0, GRID: 3.0}
    assert all(type(v) is float for v in result.values())


def test_a_share_left_out_beside_the_total_is_zero_watts() -> None:
    result = _decode("pv_battery_no_grid")

    assert result == {PV: 112.0, BATTERY: 41.0, GRID: 0.0}
    assert type(result[GRID]) is float


def test_a_serial_only_report_publishes_no_key() -> None:
    """Not sent is not 0 W: a sensor must not drop to zero mid-run."""
    assert _decode("serial_only") == {}


def test_the_total_is_not_published_a_second_time() -> None:
    """One reading, one source: the drawn power stays on 212/8."""
    for name in ("pv_battery_grid", "pv_battery_no_grid"):
        assert set(_decode(name)) == SOURCE_KEYS


def test_a_real_zero_share_is_published_as_zero() -> None:
    """A report that carries a total and an explicit zero share is not a
    report that left the share out; both give 0.0, and the explicit one must
    not turn into a missing key."""
    result = remap_heating_rod_power_source_keys(
        {"hr_total_power": 50.0, "hr_power_from_pv": 50.0, "hr_power_from_grid": 0.0}
    )

    assert result == {PV: 50.0, BATTERY: 0.0, GRID: 0.0}


def test_shares_without_the_total_publish_nothing() -> None:
    """The total is what proves the report is a reading."""
    result = remap_heating_rod_power_source_keys(
        {"hr_power_from_pv": 116.0, "hr_power_from_battery": 33.0}
    )

    assert result == {}


def _report_212_8(*fields: tuple[int, int]) -> bytes:
    """A 212/8 envelope carrying the given (field number, varint) pairs."""
    pdata = b"".join(encode_field_varint(n, v) for n, v in fields)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 212)
        + encode_field_varint(9, 8)
    )
    return encode_field_bytes(1, header)


def test_the_tank_volume_in_liters_comes_from_the_212_8_report() -> None:
    """Field 10 is 1500 in every report of the reporter's download, and the
    app's "Anpassen" setting reads 1500 L."""
    (decoded,) = decode_proto_runtime_headers(
        _report_212_8((4, 152), (10, 1500)), device_type=DEVICE_TYPE_POWEROCEAN
    )
    assert decoded.mapped.get("_is_heating_rod_param") is True
    raw = {k: v for k, v in decoded.mapped.items() if not k.startswith("_")}

    result = remap_heating_rod_keys(raw)

    assert result["heating_rod_tank_volume_l"] == 1500.0
    assert type(result["heating_rod_tank_volume_l"]) is float
    assert result["heating_rod_power_w"] == 152.0


def test_the_still_unread_fields_stay_unread() -> None:
    """Error code, self-check and run flag are still deliberately not
    published."""
    result = remap_heating_rod_keys(
        {"hr_error_code": 7, "hr_selfcheck_percent": 100, "hr_run_flag": 1}
    )

    assert result == {}
