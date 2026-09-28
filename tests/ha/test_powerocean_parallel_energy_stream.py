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

# The pseudonyms the fixture build puts in place of the masked unit serials,
# first stamped row first.
_SERIAL_A = "PAIR-TEST-UNIT-A"
_SERIAL_B = "PAIR-TEST-UNIT-B"

_INVERTER_KEYS = {
    f"inverter_{index}_{suffix}"
    for index in (1, 2)
    for suffix in ("solar_w", "batt_w", "soc_pct")
}


class _PowerOceanParser(MqttIngestMixin):
    device_sn = "J32EMASKEDTEST00"
    device_type = DEVICE_TYPE_POWEROCEAN
    device_tag = device_log_tag(device_sn)

    def __init__(self) -> None:
        self._bp_sn_to_index: dict[str, int] = {}
        self._unit_sn_to_index: dict[str, int] = {}
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


def test_no_serial_reaches_a_key_or_a_value() -> None:
    """The unit rows are read, but their serials stay out of the result."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 90)
    )

    assert parsed is not None
    assert parsed["batt_w"] == pytest.approx(-2319.194, abs=0.01)
    assert "dev_sn" not in parsed
    assert "para_energy_stream" not in parsed
    assert "all_packs" not in parsed
    assert "_unit_rows" not in parsed
    assert not any(_SERIAL_A in str(value) for value in parsed.values())
    assert not any(_SERIAL_A in key or _SERIAL_B in key for key in parsed)


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


# --- Per-inverter readings from the unit rows (#436) ----------------------


def _unit_list(*rows: JTS1ParallelEnergyStream) -> bytes:
    """A 96/50 frame with a system row first, then the given unit rows."""
    system = JTS1ParallelEnergyStream(sys_load_pwr=500.0, bp_pwr=0.0, bp_soc=90)
    report = JTS1ParallelEnergyStreamReport(para_energy_stream=[system, *rows])
    return _build_header(96, 50, report.SerializeToString())


def test_pair_unit_rows_feed_the_per_inverter_keys() -> None:
    """Frame 1 of the #347 pair: the unit with PV and the unit without.

    The two unit rows' battery power adds up to the system's 1976.674 W, and
    the unit without PV sends no `mppt_pwr` at all, which lands as 0 W.
    """
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 1)
    )

    assert parsed is not None
    assert parsed.keys() >= _INVERTER_KEYS
    assert parsed["inverter_1_solar_w"] == pytest.approx(2515.444, abs=0.01)
    assert parsed["inverter_1_batt_w"] == pytest.approx(988.644, abs=0.01)
    assert parsed["inverter_1_soc_pct"] == 97.0
    assert parsed["inverter_2_solar_w"] == 0.0
    assert parsed["inverter_2_batt_w"] == pytest.approx(988.03, abs=0.01)
    assert parsed["inverter_2_soc_pct"] == 96.0
    assert parsed["inverter_1_batt_w"] + parsed["inverter_2_batt_w"] == (
        pytest.approx(parsed["batt_w"], abs=0.01)
    )
    # The system keys are the total row's, unchanged by the unit rows.
    assert parsed["solar_w"] == pytest.approx(2515.444, abs=0.01)
    assert parsed["soc_pct"] == 97.0


def test_the_get_reply_bundle_carries_the_unit_rows_too() -> None:
    """In the 35-header bundle the unit rows arrive beside everything else."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 0)
    )

    assert parsed is not None
    assert parsed["inverter_1_batt_w"] == pytest.approx(982.151, abs=0.01)
    assert parsed["inverter_2_batt_w"] == pytest.approx(981.228, abs=0.01)
    assert "pcs_ac_freq_hz" in parsed


def test_j329_pair_after_sunset_reads_zero_solar_on_both_inverters() -> None:
    """Both J329 units have PV; after sunset neither row carries `mppt_pwr`."""
    parser = _PowerOceanParser()
    daylight = parser._parse_powerocean_proto_frame(_frame("j329", 2))
    night = parser._parse_powerocean_proto_frame(_frame("j329", 105))

    assert daylight is not None and night is not None
    assert daylight["inverter_1_solar_w"] == pytest.approx(313.688, abs=0.01)
    assert daylight["inverter_2_solar_w"] == pytest.approx(308.537, abs=0.01)
    assert night["inverter_1_solar_w"] == 0.0
    assert night["inverter_2_solar_w"] == 0.0
    assert night["inverter_1_batt_w"] == pytest.approx(-365.561, abs=0.01)
    assert night["inverter_2_soc_pct"] == 88.0


def test_inverters_are_numbered_by_serial_not_by_position() -> None:
    """A list that names the later serial first still numbers by serial."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_pwr=-200.0, bp_soc=40, dev_sn=_SERIAL_B),
            JTS1ParallelEnergyStream(bp_pwr=-100.0, bp_soc=50, dev_sn=_SERIAL_A),
        )
    )

    assert parsed is not None
    assert parsed["inverter_1_batt_w"] == -100.0
    assert parsed["inverter_1_soc_pct"] == 50.0
    assert parsed["inverter_2_batt_w"] == -200.0
    assert parsed["inverter_2_soc_pct"] == 40.0


def test_a_unit_keeps_its_number_in_a_list_that_lacks_the_other() -> None:
    """Once numbered, a unit stays on its slot even if it arrives alone."""
    parser = _PowerOceanParser()
    parser._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_pwr=10.0, bp_soc=50, dev_sn=_SERIAL_A),
            JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B),
        )
    )

    parsed = parser._parse_powerocean_proto_frame(
        _unit_list(JTS1ParallelEnergyStream(bp_pwr=30.0, bp_soc=61, dev_sn=_SERIAL_B))
    )

    assert parsed is not None
    assert parsed["inverter_2_batt_w"] == 30.0
    assert parsed["inverter_2_soc_pct"] == 61.0
    assert "inverter_1_batt_w" not in parsed


def test_a_third_serial_gets_no_slot() -> None:
    """Two slots are defined; a third unit is not squeezed into either."""
    parser = _PowerOceanParser()
    parsed = parser._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_pwr=10.0, bp_soc=50, dev_sn=_SERIAL_A),
            JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B),
            JTS1ParallelEnergyStream(bp_pwr=30.0, bp_soc=70, dev_sn="PAIR-TEST-UNIT-C"),
        )
    )

    assert parsed is not None
    assert parsed["inverter_1_batt_w"] == 10.0
    assert parsed["inverter_2_batt_w"] == 20.0
    assert not any(key.startswith("inverter_3") for key in parsed)
    assert len(parser._unit_sn_to_index) == 2


def test_an_out_of_range_unit_charge_level_is_dropped() -> None:
    """The unsigned wire maximum is not a percentage, here as on the system."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_pwr=10.0, bp_soc=4294967295, dev_sn=_SERIAL_A),
            JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B),
        )
    )

    assert parsed is not None
    assert parsed["inverter_1_batt_w"] == 10.0
    assert "inverter_1_soc_pct" not in parsed
    assert parsed["inverter_2_soc_pct"] == 60.0


def test_a_single_unit_stream_creates_no_inverter_keys() -> None:
    """A single PowerOcean's 96/33 has no unit rows and no per-inverter keys."""
    stream = JTS1EnergyStreamReport(
        sys_load_pwr=450.0, mppt_pwr=6730.0, bp_pwr=-1200.0, bp_soc=55
    )

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _build_header(96, 33, stream.SerializeToString())
    )

    assert parsed is not None
    assert not any(key.startswith("inverter_") for key in parsed)


def test_a_lone_unit_before_numbering_is_not_numbered() -> None:
    """After a restart the first list may name one unit only. Taken as it
    came, the unit sorting second would become Inverter 1 and that entity's
    history would switch units; so nothing is numbered until a list brings
    both, and then the order is the serials'."""
    parser = _PowerOceanParser()
    lone = parser._parse_powerocean_proto_frame(
        _unit_list(JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B))
    )

    assert lone is not None
    assert not any(key.startswith("inverter_") for key in lone)
    assert parser._unit_sn_to_index == {}

    both = parser._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_pwr=21.0, bp_soc=60, dev_sn=_SERIAL_B),
            JTS1ParallelEnergyStream(bp_pwr=11.0, bp_soc=50, dev_sn=_SERIAL_A),
        )
    )

    assert both is not None
    assert both["inverter_1_batt_w"] == 11.0
    assert both["inverter_2_batt_w"] == 21.0


def test_a_unit_row_without_battery_power_reads_zero() -> None:
    """proto3 leaves 0 W off the wire; the next row must not hold 500 W."""
    parser = _PowerOceanParser()
    parser._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_pwr=500.0, bp_soc=50, dev_sn=_SERIAL_A),
            JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B),
        )
    )

    parsed = parser._parse_powerocean_proto_frame(
        _unit_list(
            JTS1ParallelEnergyStream(bp_soc=50, dev_sn=_SERIAL_A),
            JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B),
        )
    )

    assert parsed is not None
    assert parsed["inverter_1_batt_w"] == 0.0


def test_a_second_row_without_a_serial_is_skipped_not_fatal() -> None:
    """Only the first unstamped row is the system; a second one is no unit
    and must not cost the frame its unit readings."""
    report = JTS1ParallelEnergyStreamReport(
        para_energy_stream=[
            JTS1ParallelEnergyStream(sys_load_pwr=500.0, bp_pwr=30.0, bp_soc=90),
            JTS1ParallelEnergyStream(bp_pwr=999.0, bp_soc=99),
            JTS1ParallelEnergyStream(bp_pwr=10.0, bp_soc=50, dev_sn=_SERIAL_A),
            JTS1ParallelEnergyStream(bp_pwr=20.0, bp_soc=60, dev_sn=_SERIAL_B),
        ]
    )

    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _build_header(96, 50, report.SerializeToString())
    )

    assert parsed is not None
    assert parsed["inverter_1_batt_w"] == 10.0
    assert parsed["inverter_2_batt_w"] == 20.0
    assert parsed["batt_w"] == 30.0


def test_per_inverter_readings_are_floats() -> None:
    """The charge level arrives as an integer and is published as a float,
    like the system's Battery SOC."""
    parsed = _PowerOceanParser()._parse_powerocean_proto_frame(
        _frame("andy-j32e-pair", 1)
    )

    assert parsed is not None
    for key in _INVERTER_KEYS:
        assert isinstance(parsed[key], float), key
