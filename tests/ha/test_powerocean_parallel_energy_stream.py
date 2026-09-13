"""A PowerOcean pair's energy stream arrives as 96/50 and reaches the sensors.

A parallel pair never sends 96/33. Its energy stream is 96/50, a list with
one row per unit and one row for the system (#347). Until this parser read
it, every entity fed from the stream - solar, home, grid and battery power,
state of charge, the derived splits and the integrated energies - stayed
`unknown` on a pair for as long as the integration ran. The fixture holds
the reporter's own frames, masked at export; see
`scripts/probe/build_powerocean_parallel_fixture.py` for what it holds and
the one byte per unit row that is restored.
"""

import json
from pathlib import Path

import pytest

from custom_components.ecoflow_energy.coordinator.mqtt_ingest import MqttIngestMixin
from custom_components.ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_POWEROCEAN,
    device_log_tag,
)
from custom_components.ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    JTS1EnergyStreamReport,
    JTS1ParallelEnergyStream,
    JTS1ParallelEnergyStreamReport,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

_FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerocean"
    / "parallel_energy_stream_masked.json"
)

_STREAM_KEYS = {"solar_w", "home_w", "grid_w", "batt_w", "soc_pct"}


class _PowerOceanParser(MqttIngestMixin):
    device_sn = "J32EMASKEDTEST00"
    device_type = DEVICE_TYPE_POWEROCEAN
    device_tag = device_log_tag(device_sn)

    def __init__(self) -> None:
        self._bp_sn_to_index: dict[str, int] = {}
        self._schedule_indices: set[int] = set()


def _build_header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    header = bytearray()
    if pdata:
        header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _frames() -> list[dict]:
    return json.loads(_FIXTURE.read_text())["frames"]


def _frame(source_prefix: str, index: int) -> bytes:
    for frame in _frames():
        if frame["source"].startswith(source_prefix) and frame["frame_index"] == index:
            return bytes.fromhex(frame["hex"])
    raise AssertionError(f"no fixture frame {source_prefix} #{index}")


# Row values of the pair's first property push (frame 1, 2026-09-05
# 12:33:27 UTC), read from the fixture by the build script's check. The two
# unit rows' battery power sums to the total row's.
_PAIR_TOTAL = {
    "solar_w": 2515.444,
    "home_w": 505.770,
    "grid_w": -33.000,
    "batt_w": 1976.674,
    "soc_pct": 97.0,
}


def test_pair_property_push_feeds_the_stream_sensors() -> None:
    """The system row of 96/50 lands on the keys 96/33 would have filled."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 1)
    )

    assert parsed is not None
    assert parsed.keys() >= _STREAM_KEYS
    for key, expected in _PAIR_TOTAL.items():
        assert parsed[key] == pytest.approx(expected, abs=0.01), key
    # The derived splits follow, exactly as they do from 96/33.
    assert parsed["batt_charge_power_w"] == pytest.approx(1976.674, abs=0.01)
    assert parsed["batt_discharge_power_w"] == 0.0
    assert parsed["grid_export_power_w"] == pytest.approx(33.0, abs=0.01)
    assert parsed["grid_import_power_w"] == 0.0


def test_pair_get_reply_bundle_carries_the_stream_beside_the_rest() -> None:
    """In the 35-header bundle the stream row merges next to the EMS keys."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 0)
    )

    assert parsed is not None
    assert parsed.keys() >= _STREAM_KEYS
    assert parsed["batt_w"] == pytest.approx(1963.378, abs=0.01)
    assert parsed["soc_pct"] == 97.0
    # The bundle's other messages are still merged, the stream displaced none.
    assert "pcs_ac_freq_hz" in parsed
    assert "bp_real_soc_pct" in parsed


def test_unit_rows_are_not_published_and_no_serial_reaches_a_key() -> None:
    """Only the system row is read; the unit rows and their serials stay out."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 90)
    )

    assert parsed is not None
    assert parsed["batt_w"] == pytest.approx(-2319.194, abs=0.01)
    assert "dev_sn" not in parsed
    assert "para_energy_stream" not in parsed
    assert "all_packs" not in parsed
    assert not any("X" * 16 in str(value) for value in parsed.values())


def test_j329_sends_the_same_list_and_its_total_row_is_taken() -> None:
    """A J329 speaks 96/50 too; after sunset its unit rows end at the serial."""
    daylight = _PowerOceanParser()._parse_powerocean_proto_frame(_frame("j329", 2))
    night = _PowerOceanParser()._parse_powerocean_proto_frame(_frame("j329", 105))

    assert daylight is not None and night is not None
    assert daylight["solar_w"] == pytest.approx(622.225, abs=0.01)
    assert daylight["batt_w"] == pytest.approx(-512.088, abs=0.01)
    assert daylight["batt_discharge_power_w"] == pytest.approx(512.088, abs=0.01)
    assert night["solar_w"] == 0.0
    assert night["batt_w"] == pytest.approx(-731.026, abs=0.01)
    assert night["soc_pct"] == 88.0


def test_every_fixture_frame_decodes_through_the_parallel_path() -> None:
    """Each frame yields exactly one 96/50 result on its own parse path."""
    for frame in _frames():
        results = [
            result
            for result in decode_proto_runtime_headers(
                bytes.fromhex(frame["hex"]), device_type=DEVICE_TYPE_POWEROCEAN
            )
            if result.parse_path == "typed_runtime:parallel_energy_stream_report"
        ]
        assert len(results) == 1, frame["note"]
        mapped = results[0].mapped
        assert mapped["_is_energy_stream"] is True
        assert mapped["_is_full_power_frame"] is True
        assert "dev_sn" not in mapped["_available_keys"]


def test_a_list_without_a_system_row_publishes_nothing() -> None:
    """Every row stamped with a serial: no row stands for the system."""
    report = JTS1ParallelEnergyStreamReport(
        para_energy_stream=[
            JTS1ParallelEnergyStream(
                sys_load_pwr=447.3, bp_pwr=982.2, bp_soc=97, dev_sn="A" * 16
            ),
            JTS1ParallelEnergyStream(
                sys_load_pwr=84.2, bp_pwr=981.2, bp_soc=96, dev_sn="B" * 16
            ),
        ]
    )
    frame = _build_header(96, 50, report.SerializeToString())

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert not (_STREAM_KEYS & (parsed or {}).keys())


def test_system_row_is_chosen_by_what_it_lacks_not_by_position() -> None:
    """The total row is found wherever the device puts it in the list."""
    report = JTS1ParallelEnergyStreamReport(
        para_energy_stream=[
            JTS1ParallelEnergyStream(
                sys_load_pwr=447.3, bp_pwr=982.2, bp_soc=97, dev_sn="A" * 16
            ),
            JTS1ParallelEnergyStream(
                sys_load_pwr=531.5,
                sys_grid_pwr=-1.7,
                mppt_pwr=2493.0,
                bp_pwr=1963.4,
                bp_soc=97,
            ),
            JTS1ParallelEnergyStream(
                sys_load_pwr=84.2, bp_pwr=981.2, bp_soc=96, dev_sn="B" * 16
            ),
        ]
    )
    frame = _build_header(96, 50, report.SerializeToString())

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert parsed is not None
    assert parsed["home_w"] == pytest.approx(531.5, abs=0.01)
    assert parsed["batt_w"] == pytest.approx(1963.4, abs=0.01)


def test_single_unit_stream_on_96_33_is_unchanged() -> None:
    """A single PowerOcean keeps its 96/33 path, byte for byte the same."""
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0,
        sys_grid_pwr=-6280.0,
        mppt_pwr=6730.0,
        bp_pwr=-1200.0,
        bp_soc=55,
    )
    frame = _build_header(96, 33, stream.SerializeToString())

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert parsed is not None
    assert parsed["solar_w"] == 6730.0
    assert parsed["batt_discharge_power_w"] == 1200.0
    assert parsed["soc_pct"] == 55.0


def test_a_second_list_in_one_bundle_is_dropped_like_every_ems_copy() -> None:
    """Two 96/50 headers in one bundle: the first wins, per the pair rule."""
    first = JTS1ParallelEnergyStreamReport(
        para_energy_stream=[
            JTS1ParallelEnergyStream(sys_load_pwr=500.0, bp_pwr=1000.0, bp_soc=90),
            JTS1ParallelEnergyStream(bp_pwr=1000.0, bp_soc=90, dev_sn="A" * 16),
        ]
    )
    second = JTS1ParallelEnergyStreamReport(
        para_energy_stream=[
            JTS1ParallelEnergyStream(sys_load_pwr=121.5, bp_pwr=-50.0, bp_soc=89),
            JTS1ParallelEnergyStream(bp_pwr=-50.0, bp_soc=89, dev_sn="A" * 16),
        ]
    )
    frame = _build_header(96, 50, first.SerializeToString()) + _build_header(
        96, 50, second.SerializeToString()
    )

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(frame)

    assert parsed is not None
    assert parsed["home_w"] == 500.0
    assert parsed["batt_w"] == 1000.0
    assert parsed["soc_pct"] == 90.0
