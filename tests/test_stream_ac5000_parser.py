"""Tests for the STREAM AC 5000 (ES22) protobuf telemetry parser."""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import (
    parse_stream_ac5000_message,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stream_ac5000"
GET_REPLY = FIXTURES / "es22_get_reply_masked.json"
PUSHES = FIXTURES / "es22_push_capture_masked.json"


def _encode_fixed32_field(field_number: int, value: float) -> bytes:
    """Encode one protobuf fixed32 field."""
    tag = (field_number << 3) | 5
    return encode_varint(tag) + struct.pack("<f", value)


def _sub(field_number: int, inner: bytes) -> bytes:
    """Wrap ``inner`` as a length-delimited submessage."""
    return encode_field_bytes(field_number, inner)


def _edges(
    *,
    to_home: float = 0.0,
    to_grid: float = 0.0,
    from_grid: float = 0.0,
    from_solar: float | None = None,
) -> bytes:
    """Build an `f12` flow-matrix group. Battery power is derived from these."""
    inner = (
        _encode_fixed32_field(4, to_home)
        + _encode_fixed32_field(5, to_grid)
        + _encode_fixed32_field(6, 0.0)
        + _encode_fixed32_field(7, from_grid)
    )
    if from_solar is not None:
        inner += _encode_fixed32_field(9, from_solar)
    return _sub(12, inner)


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal EcoFlow header frame for tests."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _build_masked_frame(cmd_func: int, cmd_id: int, inner: bytes, seq: int) -> bytes:
    """Build a frame whose payload is XOR-masked with its own sequence byte."""
    masked = bytes(value ^ (seq & 0xFF) for value in inner)
    header = bytearray()
    header.extend(encode_field_bytes(1, masked))
    header.extend(encode_field_varint(6, 1))  # enc_type
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    header.extend(encode_field_varint(14, seq))
    return encode_field_bytes(1, bytes(header))


def _load(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["frames"]


class TestNestedWalker:
    """The map addresses nested paths, which the Stream walker cannot do."""

    def test_reads_a_value_two_levels_down(self) -> None:
        inner = _sub(33, _encode_fixed32_field(6, 12.5))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["soc_precise_pct"] == pytest.approx(12.5, rel=1e-5)

    def test_half_watt_node_totals_are_halved(self) -> None:
        inner = _sub(11, encode_field_varint(1, 4042))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["home_w"] == pytest.approx(2021.0, rel=1e-6)

    def test_solar_node_total_is_already_watts(self) -> None:
        """f11.9 is the one field in this block that is not half-watts."""
        inner = _sub(11, encode_field_varint(9, 1556))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["solar_w"] == pytest.approx(1556.0, rel=1e-6)

    def test_undeclared_string_field_is_not_entered(self) -> None:
        """A length-delimited field the map does not declare is skipped.

        `f23.3` holds a timezone name. Walking into it as if it were a
        submessage is how a parser invents fields that do not exist.
        """
        timezone_block = _sub(23, _sub(3, b"Europe/Amsterdam"))
        frame = _build_frame(254, 39, bytes(timezone_block + _edges(from_grid=100.0)))
        result = parse_stream_ac5000_message(frame)
        assert result is not None
        assert result["batt_w"] == pytest.approx(100.0, rel=1e-5)
        assert not any(key.startswith("23") for key in result)

    def test_group_arriving_as_a_scalar_is_ignored(self) -> None:
        """A declared group sent as a varint is a layout difference."""
        inner = encode_field_varint(11, 5) + _edges(from_grid=42.0)
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert "home_w" not in result
        assert result["batt_w"] == pytest.approx(42.0, rel=1e-5)

    def test_masked_payload_is_unmasked(self) -> None:
        """No ES22 frame has set enc_type, so this path is synthetic only."""
        inner = _sub(11, encode_field_varint(5, 18))
        frame = _build_masked_frame(254, 39, bytes(inner), seq=77)
        result = parse_stream_ac5000_message(frame)
        assert result is not None
        assert result["soc_pct"] == 18


class TestFlowEdges:
    def test_present_edge_group_zero_fills_its_missing_edges(self) -> None:
        """proto3 omits a zero, so an absent edge in a present group is 0.

        Without this an idle battery would keep reporting the last power it
        delivered, forever.
        """
        inner = _sub(12, encode_field_varint(6, 800))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["home_from_grid_w"] == 800
        assert result["home_from_batt_w"] == 0.0
        assert result["grid_export_power_w"] == 0.0
        assert result["grid_import_power_w"] == 800.0

    def test_absent_edge_group_reports_nothing(self) -> None:
        """An `f11`-only push means unchanged, not zero.

        116 of 355 observed pushes have this shape. Zero-filling them would
        blank an edge that is still delivering power.
        """
        inner = _sub(11, encode_field_varint(1, 2000))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["home_w"] == pytest.approx(1000.0)
        assert "home_from_batt_w" not in result
        assert "home_from_grid_w" not in result
        assert "grid_import_power_w" not in result
        assert "grid_export_power_w" not in result

    def test_the_solar_to_home_edge_is_not_mapped(self) -> None:
        """Field 8 appears in no captured frame, so its position is a guess.

        An inferred position reaching an accessory entity is a wrong reading
        Home Assistant keeps forever.
        """
        inner = _sub(12, encode_field_varint(4, 500) + encode_field_varint(8, 700))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert "home_from_solar_w" not in result

    def test_solar_falls_to_zero_instead_of_latching(self) -> None:
        """The node total is filled, so sunset reports 0 rather than nothing.

        Nothing deletes a key that stops arriving, so a solar reading left
        out of the fill held its last daylight value until sunrise. The
        entity is still kept off units without PV, by requiring a non-zero
        reading before it is created rather than by withholding the zero.
        """
        daylight = _sub(11, encode_field_varint(1, 2000) + encode_field_varint(9, 1556))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(daylight)))
        assert result is not None
        assert result["solar_w"] == pytest.approx(1556.0)

        night = _sub(11, encode_field_varint(1, 2000))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(night)))
        assert result is not None
        assert result["solar_w"] == 0.0

    def test_an_absent_node_group_still_reports_no_solar(self) -> None:
        """The fill needs its container: no `f11`, no statement about solar."""
        inner = _sub(12, encode_field_varint(4, 500))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert "solar_w" not in result

    def test_export_sums_the_two_outbound_edges(self) -> None:
        inner = _sub(
            12,
            encode_field_varint(5, 356) + encode_field_varint(10, 62),
        )
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["grid_export_power_w"] == pytest.approx(418.0)
        assert result["grid_import_power_w"] == pytest.approx(0.0)


class TestDerivedValues:
    @pytest.mark.parametrize(
        ("edges", "batt_w"),
        [
            ({"from_grid": 599.6}, 599.6),
            ({"to_home": 593.0}, -593.0),
            ({"to_grid": 200.0, "to_home": 393.0}, -593.0),
            ({}, 0.0),
        ],
    )
    def test_battery_power_comes_from_the_edges(
        self, edges: dict[str, float], batt_w: float
    ) -> None:
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(_edges(**edges))))
        assert result is not None
        assert result["batt_w"] == pytest.approx(batt_w, rel=1e-5)

    @pytest.mark.parametrize(
        ("edges", "batt_w"),
        [
            ({"from_solar": 400.0, "from_grid": 100.0}, 500.0),
            ({"from_solar": 400.0, "to_home": 150.0}, 250.0),
            ({"from_solar": 46.0}, 46.0),
        ],
    )
    def test_the_solar_to_battery_edge_counts_towards_battery_power(
        self, edges: dict[str, float], batt_w: float
    ) -> None:
        """`f12.9` is solar charging the pack and belongs in the sum.

        It is absent from both fixtures only because the unit they came from
        has no PV wired to the EcoFlow. Issue #177 confirms it three times on
        a unit that has, the clearest being a frame where the battery took
        47 W out of 2.86 kW of solar and field 9 read 46. Dropping it would
        report zero battery power to a PV owner while the pack fills, and the
        charge counter would miss the whole solar contribution.
        """
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(_edges(**edges))))
        assert result is not None
        assert result["batt_w"] == pytest.approx(batt_w, rel=1e-5)

    def test_battery_power_ignores_f50(self) -> None:
        """`f50.1.4` latches at its last active value once the unit idles."""
        inner = _edges(to_home=0.0) + _sub(50, _sub(1, _encode_fixed32_field(4, 444.0)))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["batt_w"] == pytest.approx(0.0)

    def test_absent_flow_matrix_leaves_battery_power_unset(self) -> None:
        """An absent `f12` means unchanged, so the coordinator keeps the last."""
        inner = _sub(38, _sub(1, encode_field_varint(1, 9)))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner))) or {}
        assert "batt_w" not in result
        assert "batt_charge_power_w" not in result

    def test_zero_fill_clears_the_battery_edges(self) -> None:
        """A present `f12` that omits an edge decodes it as zero, not stale."""
        inner = _sub(12, _encode_fixed32_field(6, 200.0))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["batt_w"] == pytest.approx(0.0)

    @pytest.mark.parametrize(
        ("edges", "charge", "discharge"),
        [
            ({"from_grid": 599.6}, 599.6, 0.0),
            ({"to_home": 593.0}, 0.0, 593.0),
            ({}, 0.0, 0.0),
        ],
    )
    def test_signed_battery_power_splits_one_way_only(
        self, edges: dict[str, float], charge: float, discharge: float
    ) -> None:
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(_edges(**edges))))
        assert result is not None
        assert result["batt_charge_power_w"] == pytest.approx(charge, rel=1e-5)
        assert result["batt_discharge_power_w"] == pytest.approx(discharge, rel=1e-5)

    def test_charge_discharge_state_is_left_to_the_coordinator(self) -> None:
        """The coordinator derives it with hysteresis over a rolling window."""
        result = parse_stream_ac5000_message(
            _build_frame(254, 39, bytes(_edges(to_home=593.0)))
        )
        assert result is not None
        assert "batt_charge_discharge_state" not in result

    def test_meter_block_carries_the_grid_sign(self) -> None:
        """A Tibber Pulse reports one signed total; negative is export."""
        inner = _sub(15, _encode_fixed32_field(3, -419.0))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["grid_w"] == pytest.approx(-419.0, rel=1e-5)

    def test_p1_meter_net_feeds_the_same_key(self) -> None:
        """An EcoFlow P1 reports per-phase values and its net in f16.16."""
        inner = _sub(
            16,
            _encode_fixed32_field(4, 1111.0)
            + _encode_fixed32_field(5, -506.0)
            + _encode_fixed32_field(6, -607.0)
            + _encode_fixed32_field(7, 231.4)
            + _encode_fixed32_field(15, 50.02)
            + _encode_fixed32_field(16, -2.0),
        )
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["grid_phase_a_active_power_w"] == pytest.approx(1111.0, rel=1e-5)
        assert result["grid_phase_b_active_power_w"] == pytest.approx(-506.0, rel=1e-5)
        assert result["grid_phase_c_active_power_w"] == pytest.approx(-607.0, rel=1e-5)
        assert result["grid_phase_a_voltage_v"] == pytest.approx(231.4, rel=1e-5)
        assert result["ac_frequency_hz"] == pytest.approx(50.02, rel=1e-5)
        assert result["grid_w"] == pytest.approx(-2.0, rel=1e-5)

    def test_account_power_limits(self) -> None:
        """f10.1 and f10.2 are the limits the app calls max grid-tied output
        and max grid input. Each was moved in the app on its own and read back
        at the value set, in both directions."""
        inner = _sub(10, encode_field_varint(1, 2400) + encode_field_varint(2, 1200))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["max_grid_output_power_w"] == 2400
        assert result["max_grid_input_power_w"] == 1200

    def test_the_ceiling_fields_are_not_a_user_limit(self) -> None:
        """Neither f10.5 nor f10.6 is a user setting, for different reasons.

        `.6` sat at the same 2500 as the input limit for as long as nobody
        touched the setting, which is what made it look like the source. It
        did not follow that limit to 1200 and back, and it has moved only
        once ever, when the ceiling was raised from 600, so it behaves like
        that ceiling. `.5` is unexplained: 600 at the old ceiling and 800
        after, matching no setting visible in the app.
        """
        inner = _sub(10, encode_field_varint(5, 800) + encode_field_varint(6, 2500))
        assert parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner))) is None

    def test_the_milliwatt_pair_is_not_mapped(self) -> None:
        """254/40 f22 reads 600000/1200000 and stayed there while the account
        limit went to 2500 W, the output limit to 2400 W and the discharge task
        to 1400 W. It is neither, so it is left alone."""
        inner = _sub(22, encode_field_varint(1, 600000) + encode_field_varint(3, 1200000))
        assert parse_stream_ac5000_message(_build_frame(254, 40, bytes(inner))) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0, "self_powered"), (1, "intelligent_plus"), (2, "custom"), (9, None)],
    )
    def test_work_mode_enum(self, raw: int, expected: str | None) -> None:
        inner = encode_field_varint(25, raw)
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["work_mode"] == expected

    def test_bms_current_and_voltage_convert_from_milli_units(self) -> None:
        inner = encode_field_varint(7, 16063) + encode_field_varint(
            8, (1 << 64) - 40735
        )
        result = parse_stream_ac5000_message(_build_frame(32, 50, bytes(inner)))
        assert result is not None
        assert result["batt_voltage_v"] == pytest.approx(16.063)
        assert result["bms_current_a"] == pytest.approx(-40.735)

    def test_bms_soc_does_not_overwrite_the_system_soc(self) -> None:
        """The pack reads about two points above what the app shows."""
        system = _sub(33, _encode_fixed32_field(6, 18.63))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(system)))
        assert result is not None
        assert result["soc_precise_pct"] == pytest.approx(18.63, rel=1e-4)

        bms = parse_stream_ac5000_message(
            _build_frame(32, 50, bytes(_encode_fixed32_field(25, 20.93)))
        )
        assert bms is not None
        assert bms["bms_soc_precise_pct"] == pytest.approx(20.93, rel=1e-4)
        assert "soc_precise_pct" not in bms


class TestSocLimits:
    def test_limits_from_the_battery_heartbeat(self) -> None:
        inner = _sub(1, encode_field_varint(7, 90) + encode_field_varint(21, 15))
        result = parse_stream_ac5000_message(_build_frame(32, 2, bytes(inner)))
        assert result is not None
        assert result["max_charge_soc_pct"] == 90
        assert result["min_discharge_soc_pct"] == 15

    def test_the_config_block_is_not_a_second_source(self) -> None:
        """`254/39 f29` holds the same limits and stays unmapped.

        Both frames reach the same entity on the same channel, so mapping
        both would be two sources writing one value, the shape that made the
        Stream LED readback flap. `f29` is the one left out because it rides
        only in the full-state frame, so a limit changed in the app would
        take minutes to arrive rather than seconds.
        """
        inner = _sub(29, encode_field_varint(1, 90) + encode_field_varint(2, 15))
        assert parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner))) is None

    def test_scheduled_discharge_setpoint_is_read_back(self) -> None:
        """f40.1.9.1 followed every setpoint written during a control test."""
        task = (
            encode_field_varint(2, 2)  # discharge
            + encode_field_varint(3, 1)  # enabled
            + _sub(7, encode_varint((1380 << 16) | 0))
            + _sub(9, encode_field_varint(1, 500))
        )
        inner = _sub(40, _sub(1, task))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["scheduled_discharge_power_w"] == 500
        assert result["scheduled_discharge_enabled"] is True
        assert result["scheduled_discharge_start_min"] == 0
        assert result["scheduled_discharge_end_min"] == 1380
        # The charge task is a separate frame, so nothing about it is implied.
        assert "scheduled_charge_power_w" not in result


class TestScheduledTasks:
    def test_charge_task_keys_are_separate_from_discharge(self) -> None:
        per_device = (
            encode_field_bytes(1, b"ES22TEST00000001")
            + encode_field_varint(2, 100)
            + encode_field_varint(3, 600)
        )
        task = (
            encode_field_varint(2, 1)  # charge
            + encode_field_varint(3, 1)
            + _sub(7, encode_varint((960 << 16) | 780))
            + _sub(8, encode_field_varint(1, 1) + _sub(3, per_device))
        )
        result = parse_stream_ac5000_message(
            _build_frame(254, 39, bytes(_sub(40, _sub(1, task))))
        )
        assert result is not None
        assert result["scheduled_charge_power_w"] == 600
        assert result["scheduled_charge_start_min"] == 780
        assert result["scheduled_charge_end_min"] == 960
        assert "scheduled_discharge_power_w" not in result

    def test_disabled_task_reports_disabled(self) -> None:
        """Disabling a task in the app dropped f40.1.3 rather than sending 0."""
        task = encode_field_varint(2, 2) + _sub(9, encode_field_varint(1, 600))
        result = parse_stream_ac5000_message(
            _build_frame(254, 39, bytes(_sub(40, _sub(1, task))))
        )
        assert result is not None
        assert result["scheduled_discharge_enabled"] is False

    def test_task_without_a_kind_is_dropped(self) -> None:
        """Which task a power belongs to cannot be guessed."""
        task = _sub(9, encode_field_varint(1, 500))
        result = parse_stream_ac5000_message(
            _build_frame(254, 39, bytes(_sub(40, _sub(1, task))))
        )
        assert result is None

    def test_empty_task_block_clears_every_readback_key(self) -> None:
        """Deleting the last task in the app sends `f40` with no bytes.

        Without this the setpoint entities keep reporting a task the device
        no longer has, since the coordinator merge only ever adds keys.
        """
        inner = _sub(11, encode_field_varint(5, 42)) + _sub(40, b"")
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["soc_pct"] == 42
        for kind in ("charge", "discharge"):
            assert result[f"scheduled_{kind}_power_w"] is None
            assert result[f"scheduled_{kind}_enabled"] is None
            assert result[f"scheduled_{kind}_start_min"] is None
            assert result[f"scheduled_{kind}_end_min"] is None
        assert result["scheduled_charge_soc_target"] is None

    def test_absent_task_block_clears_nothing(self) -> None:
        """Most 254/39 frames are deltas carrying no `f40`, meaning unchanged.

        Clearing on absence rather than on an empty block would flap the
        setpoint entities on nearly every frame.
        """
        inner = _sub(11, encode_field_varint(5, 42))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert not [key for key in result if key.startswith("scheduled_")]

    def test_a_reported_task_beats_an_empty_block(self) -> None:
        """A task in the same frame is the more specific statement."""
        task = encode_field_varint(2, 2) + _sub(9, encode_field_varint(1, 700))
        inner = _sub(40, b"") + _sub(40, _sub(1, task))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(inner)))
        assert result is not None
        assert result["scheduled_discharge_power_w"] == 700


class TestRejectsGarbage:
    def test_empty_payload(self) -> None:
        assert parse_stream_ac5000_message(b"") is None

    def test_random_bytes(self) -> None:
        assert parse_stream_ac5000_message(b"\xff\xfe\xfd\xfc") is None

    def test_unmapped_command_yields_nothing(self) -> None:
        frame = _build_frame(53, 77, bytes.fromhex("08021003"))
        assert parse_stream_ac5000_message(frame) is None


class TestCaptureReplay:
    """Replay of the real masked captures through the production entry point."""

    def test_get_reply_bundle_decodes_every_command(self) -> None:
        frame = _load(GET_REPLY)[0]
        result = parse_stream_ac5000_message(bytes.fromhex(frame["hex"]))
        assert result is not None
        # One bundle carries the whole picture: battery, flow, meter, limits.
        for key in (
            "soc_pct",
            "batt_w",
            "home_w",
            "grid_w",
            "max_charge_soc_pct",
            "min_discharge_soc_pct",
            "bms_soh_pct",
            "batt_voltage_v",
            "work_mode",
        ):
            assert key in result, key

    def test_push_frames_parse_or_carry_nothing_we_map(self) -> None:
        """A push yields nothing when every block in it is unmapped.

        254/40 always qualifies. A 254/39 does when it carries only the pack
        blocks `f38`/`f44` (duplicates of 32/50) or `f50`, which is what an
        idle unit sends once it drops the flow matrix.
        """
        frames = _load(PUSHES)
        assert len(frames) >= 20
        empty_cmds = []
        for frame in frames:
            if parse_stream_ac5000_message(bytes.fromhex(frame["hex"])):
                continue
            empty_cmds.append(
                tuple(sorted((c["cmd_func"], c["cmd_id"]) for c in frame["cmds"]))
            )
        assert len(empty_cmds) < len(frames) / 2
        for cmds in empty_cmds:
            assert cmds in (((254, 40),), ((254, 39),)), cmds

    def test_every_battery_heartbeat_frame_yields_the_soc_limits(self) -> None:
        """A 32/2 frame is the fast source, so none of them may parse empty.

        The allowance above used to cover 32/2 because nothing in it was
        mapped. Now that both SoC limits ride there, a frame carrying it has
        to produce them, and this fails if the mapping is ever dropped again.
        """
        seen = 0
        for frame in _load(PUSHES):
            if [(c["cmd_func"], c["cmd_id"]) for c in frame["cmds"]] != [(32, 2)]:
                continue
            seen += 1
            parsed = parse_stream_ac5000_message(bytes.fromhex(frame["hex"])) or {}
            assert "max_charge_soc_pct" in parsed
            assert "min_discharge_soc_pct" in parsed
        assert seen >= 1

    def test_no_solar_entity_data_on_a_unit_without_pv(self) -> None:
        """Most frames must not carry solar, or the gating never holds off."""
        parsed = [
            parse_stream_ac5000_message(bytes.fromhex(f["hex"])) or {}
            for f in _load(PUSHES)
        ]
        with_solar = [p for p in parsed if "solar_w" in p]
        assert len(with_solar) < len(parsed) / 2

    def test_a_unit_without_pv_still_reports_a_solar_figure(self) -> None:
        """Two frames of the capture read 62 W of solar on a PV-less unit.

        The device works that figure out from the house flows and the app
        shows the same number, so the reading is parsed as reported. This
        pins what the parser does with it and nothing more: it is the
        device's inference, which is why the live reading exists and the
        lifetime counter that would integrate it does not.
        """
        frames = _load(PUSHES)
        for index in (25, 26):
            parsed = parse_stream_ac5000_message(bytes.fromhex(frames[index]["hex"]))
            assert parsed is not None, index
            assert parsed["solar_w"] == pytest.approx(62.0), index

    def test_home_equals_the_edges_into_home(self) -> None:
        """The identity the flow matrix has to preserve, on real frames."""
        checked = 0
        for frame in _load(PUSHES):
            parsed = parse_stream_ac5000_message(bytes.fromhex(frame["hex"])) or {}
            home = parsed.get("home_w")
            from_batt = parsed.get("home_from_batt_w")
            from_grid = parsed.get("home_from_grid_w")
            if not all(isinstance(v, (int, float)) for v in (home, from_batt, from_grid)):
                continue
            total = from_batt + from_grid + (parsed.get("home_from_solar_w") or 0.0)
            assert abs(home - total) <= 2, frame["ts_iso"]
            checked += 1
        assert checked >= 5

    def test_import_and_export_are_never_negative(self) -> None:
        for frame in _load(PUSHES) + _load(GET_REPLY):
            parsed = parse_stream_ac5000_message(bytes.fromhex(frame["hex"])) or {}
            for key in ("grid_import_power_w", "grid_export_power_w"):
                value = parsed.get(key)
                if isinstance(value, (int, float)):
                    assert value >= 0, f"{key} = {value}"

    def test_fixtures_carry_no_identifier(self) -> None:
        """Guards the fixtures themselves, not the parser."""
        import re

        for path in (GET_REPLY, PUSHES):
            for frame in _load(path):
                raw = bytes.fromhex(frame["hex"])
                runs = [m for m in re.findall(rb"[A-Z0-9]{15,}", raw) if set(m) != {ord("X")}]
                assert not runs, f"{path.name}: {runs}"
                assert "{sn}" in frame["topic"]


TWO_UNITS = FIXTURES / "es22_two_units_masked.json"


def _node_half_watts(pdata: bytes) -> int | None:
    """Read `f11.4`, the battery node total in half-watts, straight off a payload.

    The parser deliberately leaves this field unmapped, so the test reads it
    itself rather than comparing against a value derived from other fields.
    """
    from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import _iter_fields

    for number, wire_type, raw in _iter_fields(pdata):
        if number != 11 or wire_type != 2:
            continue
        for num, wire, value in _iter_fields(raw):
            if num == 4 and wire == 0:
                total = shift = 0
                for byte in value:
                    total |= (byte & 0x7F) << shift
                    shift += 7
                return total
    return None


def _unit_entry(serial: bytes, *, soc: int, half_watts: int) -> bytes:
    """Build one `f54.1` entry: serial, state of charge, power in half-watts."""
    return _sub(
        1,
        encode_field_bytes(1, serial)
        + encode_field_varint(2, soc)
        + encode_field_varint(4, half_watts),
    )


class TestLinkedUnitBlock:
    """`f54` carries one entry per linked unit, each stamped with its serial."""

    def test_two_entries_survive_each_other(self) -> None:
        """The block repeats, so a walker that keys by path would keep one."""
        block = _sub(
            54,
            _unit_entry(b"ES22TESTUNITAAAA", soc=87, half_watts=0)
            + _unit_entry(b"ES22TESTUNITBBBB", soc=65, half_watts=1378),
        )
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(block)))
        assert result is not None
        assert result["_unit_batt_w_by_sn"] == {
            "ES22TESTUNITAAAA": 0.0,
            "ES22TESTUNITBBBB": 689.0,
        }

    def test_single_unit_installation_reports_one_entry(self) -> None:
        block = _sub(54, _unit_entry(b"ES22TESTUNITAAAA", soc=76, half_watts=1356))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(block)))
        assert result is not None
        assert result["_unit_batt_w_by_sn"] == {"ES22TESTUNITAAAA": 678.0}

    def test_entry_without_a_power_value_is_not_reported(self) -> None:
        """An absent field means unchanged here, so it must not read as zero."""
        entry = _sub(1, encode_field_bytes(1, b"ES22TESTUNITAAAA") + encode_field_varint(2, 87))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(_sub(54, entry))))
        assert result is None or "_unit_batt_w_by_sn" not in result

    def test_entry_without_a_serial_is_not_reported(self) -> None:
        entry = _sub(1, encode_field_varint(2, 87) + encode_field_varint(4, 1378))
        result = parse_stream_ac5000_message(_build_frame(254, 39, bytes(_sub(54, entry))))
        assert result is None or "_unit_batt_w_by_sn" not in result

    def test_absent_block_produces_no_key(self) -> None:
        result = parse_stream_ac5000_message(
            _build_frame(254, 39, bytes(_edges(from_grid=100.0)))
        )
        assert result is not None
        assert "_unit_batt_w_by_sn" not in result

    def test_real_two_unit_payloads_split_into_two_units(self) -> None:
        """The reporter's own frames, with the two masked serials made distinct.

        The last frame is the one his app screenshot pins down: 0 W on the unit
        reading 87 percent and 689 W on the one reading 65 percent.
        """
        from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import _read_unit_entries

        fixture = json.loads(TWO_UNITS.read_text(encoding="utf-8"))
        unit_a, unit_b = fixture["unit_a"], fixture["unit_b"]
        seen = []
        for frame in fixture["frames"]:
            entries = _read_unit_entries(bytes.fromhex(frame["pdata_hex"]))
            assert set(entries) == {unit_a, unit_b}, frame["ts"]
            seen.append(entries)
        assert len(seen) == 6
        assert seen[-1] == {unit_a: 0.0, unit_b: 689.0}

    def test_real_payload_entries_sum_to_the_system_reading(self) -> None:
        """Per-unit and system battery power come from different fields.

        They are only the same quantity if they agree, so this checks the sum
        on every frame of the capture rather than trusting the one moment the
        app screenshot covers.

        The target is the node field `f11.4`, which shares both the scale and
        the derivation with the entries. The parser's own `batt_w` is not
        comparable at this precision: it is derived from the `f12` flow edges
        instead, and on this capture the two disagree by up to 54 W within a
        single frame.
        """
        from ecoflow_energy.ecoflow.parsers.stream_ac5000_proto import _read_unit_entries

        checked = 0
        fixture = json.loads(TWO_UNITS.read_text(encoding="utf-8"))
        for frame in fixture["frames"]:
            pdata = bytes.fromhex(frame["pdata_hex"])
            system = _node_half_watts(pdata)
            if system is None:
                continue
            total = sum(_read_unit_entries(pdata).values())
            assert abs(total - system / 2) <= 35, f"{frame['ts']}: {total} vs {system / 2}"
            checked += 1
        assert checked == 6

    def test_fixture_carries_no_real_identifier(self) -> None:
        import re

        fixture = json.loads(TWO_UNITS.read_text(encoding="utf-8"))
        allowed = {fixture["unit_a"].encode(), fixture["unit_b"].encode()}
        for frame in fixture["frames"]:
            raw = bytes.fromhex(frame["pdata_hex"])
            runs = [
                run
                for run in re.findall(rb"[A-Z0-9]{15,}", raw)
                if set(run) != {ord("X")} and run not in allowed
            ]
            assert not runs, runs
