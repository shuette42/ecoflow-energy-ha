"""Delta 3 idle shutdowns: parser fields, shared option map and wire payloads.

Four settings from one page of the vendor app - the unit itself, both AC
outlets and the 12 V group - each switching its output off after a span with no
load connected and no activity. Not timers: a load that keeps drawing keeps its
output alive, which is the app's own description.

Two things this file exists to nail down.

**Zero is "never".** Same trap as the screen timeout, with more at stake: the
device shutdown powers the whole unit down. Three of the four read zero on the
maintainer's hardware while the app showed "Nie".

**The four write fields are 13, 10, 11 and 572** and they are trivially easy to
transpose. Each gets its own assertion on the exact number rather than a shared
loop, so a swap fails on the specific setting that was swapped.

Entity behaviour lives in tests/ha/test_delta3_idle_shutdown_entity.py.
"""

from __future__ import annotations

import pytest

from ecoflow_energy.const import (
    DELTA3_IDLE_SHUTDOWN_VALUES,
    DELTA3_IDLE_SHUTDOWNS,
    DELTA3_SELECTS,
)
from ecoflow_energy.ecoflow.delta3_commands import (
    DELTA3_SELECT_FIELDS,
    build_proto_command,
    build_select_command,
)
from ecoflow_energy.ecoflow.parsers.delta3_proto import parse_delta3_display_property
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import Delta3DisplayProperty
from ecoflow_energy.ecoflow.proto.runtime import decode_proto_runtime_frame
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

DEVICE_SN = "D3M1TEST0001ABCD"

# entity key -> (protobuf field name, sensor key, ConfigWrite field)
SETTINGS: dict[str, tuple[str, str, int]] = {
    "device_idle_shutdown": ("dev_standby_time", "dev_standby_time_min", 13),
    "ac1_idle_shutdown": ("ac_standby_time", "ac_standby_time_min", 10),
    "ac2_idle_shutdown": ("ac2_standby_time", "ac2_standby_time_min", 572),
    "dc_idle_shutdown": ("dc_standby_time", "dc_standby_time_min", 11),
}


def _frame(inner: bytes) -> bytes:
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))
    header.extend(encode_field_varint(8, 254))
    header.extend(encode_field_varint(9, 21))
    header.extend(encode_field_bytes(24, DEVICE_SN.encode()))
    return encode_field_bytes(1, bytes(header))


def _decode(frame: bytes) -> dict:
    result = decode_proto_runtime_frame(frame)
    return {k: v for k, v in result.mapped.items() if not k.startswith("_")}


def _parsed(proto_field: str, minutes: int) -> dict:
    msg = Delta3DisplayProperty()
    setattr(msg, proto_field, minutes)
    return parse_delta3_display_property(_decode(_frame(msg.SerializeToString())))


class TestParser:
    @pytest.mark.parametrize("entity_key", sorted(SETTINGS))
    @pytest.mark.parametrize("minutes", sorted(DELTA3_IDLE_SHUTDOWN_VALUES))
    def test_every_step_survives_the_round_trip(
        self, entity_key: str, minutes: int
    ) -> None:
        proto_field, sensor_key, _ = SETTINGS[entity_key]
        assert _parsed(proto_field, minutes)[sensor_key] == minutes

    @pytest.mark.parametrize("entity_key", sorted(SETTINGS))
    def test_zero_is_reported_rather_than_dropped(self, entity_key: str) -> None:
        """Proto3 omits a plain zero; these fields are declared optional for it.

        Zero is "Never" and therefore a real setting. Losing it would leave the
        entity showing a span after which the device switches off, on a device
        set never to switch off.
        """
        proto_field, sensor_key, _ = SETTINGS[entity_key]
        assert _parsed(proto_field, 0)[sensor_key] == 0

    @pytest.mark.parametrize("entity_key", sorted(SETTINGS))
    def test_absent_field_produces_no_key(self, entity_key: str) -> None:
        _, sensor_key, _ = SETTINGS[entity_key]
        msg = Delta3DisplayProperty()
        msg.pow_in_sum_w = 100.0
        parsed = parse_delta3_display_property(_decode(_frame(msg.SerializeToString())))
        assert sensor_key not in parsed

    def test_each_field_lands_in_its_own_key(self) -> None:
        """One frame carrying four different values, none of them crossed."""
        msg = Delta3DisplayProperty()
        msg.dev_standby_time = 30
        msg.ac_standby_time = 60
        msg.ac2_standby_time = 240
        msg.dc_standby_time = 1440
        parsed = parse_delta3_display_property(_decode(_frame(msg.SerializeToString())))

        assert parsed["dev_standby_time_min"] == 30
        assert parsed["ac_standby_time_min"] == 60
        assert parsed["ac2_standby_time_min"] == 240
        assert parsed["dc_standby_time_min"] == 1440

    @pytest.mark.parametrize(
        ("read_field", "sensor_key", "minutes"),
        [
            (17, "dev_standby_time_min", 30),
            (19, "ac_standby_time_min", 60),
            (20, "dc_standby_time_min", 120),
            (1460, "ac2_standby_time_min", 240),
        ],
    )
    def test_read_back_field_numbers_are_pinned_independently(
        self, read_field: int, sensor_key: str, minutes: int
    ) -> None:
        """Encode the field number by hand rather than through the bindings.

        Every other parser test builds its frame with `Delta3DisplayProperty`,
        which is generated from the same `.proto` the decoder reads. If two of
        these numbers were swapped relative to the device, encode and decode
        would be wrong in the same direction and every one of those tests would
        still pass. This one writes the literal number onto the wire, so it
        fails on a swap.

        The numbers are corroborated on hardware rather than taken on trust: a
        recording showed field 20 at 120 while the app had the 12 V page on
        "2 h", and each write was acknowledged by the device with the field
        number it had applied.
        """
        pdata = encode_field_varint(read_field, minutes)
        parsed = parse_delta3_display_property(_decode(_frame(pdata)))
        assert parsed[sensor_key] == minutes

    def test_the_screen_timeout_is_a_different_unit(self) -> None:
        """Seconds there, minutes here, in the same message. Easy to conflate."""
        msg = Delta3DisplayProperty()
        msg.screen_off_time = 300
        msg.dc_standby_time = 300
        parsed = parse_delta3_display_property(_decode(_frame(msg.SerializeToString())))

        assert parsed["screen_off_time_sec"] == 300
        assert parsed["dc_standby_time_min"] == 300


class TestOptionMap:
    def test_zero_means_never(self) -> None:
        assert DELTA3_IDLE_SHUTDOWN_VALUES[0] == "never"

    def test_two_hours_is_120_minutes(self) -> None:
        """The pairing that proved the unit.

        A screenshot of the 12 V DC page with "2 h" ticked was held against a
        capture in which field 20 read 120.
        """
        assert DELTA3_IDLE_SHUTDOWN_VALUES[120] == "2_hours"

    def test_steps_match_the_app_list(self) -> None:
        assert dict(DELTA3_IDLE_SHUTDOWN_VALUES) == {
            30: "30_minutes",
            60: "1_hour",
            120: "2_hours",
            240: "4_hours",
            360: "6_hours",
            720: "12_hours",
            1440: "24_hours",
            0: "never",
        }

    def test_never_is_last_and_shortest_is_first(self) -> None:
        """The app's order. Never sits at the end, where a user expects it."""
        options = tuple(DELTA3_IDLE_SHUTDOWN_VALUES.values())
        assert options[0] == "30_minutes"
        assert options[-1] == "never"

    @pytest.mark.parametrize("entity_key", sorted(SETTINGS))
    def test_all_four_share_one_option_list(self, entity_key: str) -> None:
        defn = next(d for d in DELTA3_SELECTS if d.key == entity_key)
        assert defn.options == tuple(DELTA3_IDLE_SHUTDOWN_VALUES.values())

    @pytest.mark.parametrize("entity_key", sorted(SETTINGS))
    def test_all_four_are_push_only(self, entity_key: str) -> None:
        defn = next(d for d in DELTA3_SELECTS if d.key == entity_key)
        assert defn.enhanced_only is True

    def test_the_definition_table_and_the_selects_agree(self) -> None:
        from_table = {key for key, _, _, _ in DELTA3_IDLE_SHUTDOWNS}
        assert from_table == set(SETTINGS)

    def test_names_say_what_powers_down(self) -> None:
        """The app calls these "timeout", which names the mechanism.

        A user scanning an entity list has to be able to tell that this one
        switches an outlet off.
        """
        names = {key: name for key, name, _, _ in DELTA3_IDLE_SHUTDOWNS}
        assert all("Shutdown" in name for name in names.values())


class TestCommand:
    def test_unknown_key_returns_none(self) -> None:
        assert build_select_command("work_mode", 30) is None

    @pytest.mark.parametrize("entity_key", sorted(SETTINGS))
    @pytest.mark.parametrize("minutes", sorted(DELTA3_IDLE_SHUTDOWN_VALUES))
    def test_wire_frame_carries_the_right_field(
        self, entity_key: str, minutes: int
    ) -> None:
        _, _, config_field = SETTINGS[entity_key]
        command = build_select_command(entity_key, minutes)
        assert command is not None
        payload = build_proto_command(command, DEVICE_SN)
        assert payload is not None
        expected = encode_field_bytes(1, encode_field_varint(config_field, minutes))
        assert expected in payload

    # Written out one by one on purpose. A parametrized loop over the same table
    # the code reads would pass just as happily with two entries swapped.
    def test_device_shutdown_writes_field_13(self) -> None:
        assert DELTA3_SELECT_FIELDS["device_idle_shutdown"].config_field == 13

    def test_ac1_writes_field_10(self) -> None:
        assert DELTA3_SELECT_FIELDS["ac1_idle_shutdown"].config_field == 10

    def test_ac2_writes_field_572(self) -> None:
        assert DELTA3_SELECT_FIELDS["ac2_idle_shutdown"].config_field == 572

    def test_dc_writes_field_11(self) -> None:
        assert DELTA3_SELECT_FIELDS["dc_idle_shutdown"].config_field == 11

    def test_the_screen_timeout_still_writes_field_12(self) -> None:
        assert DELTA3_SELECT_FIELDS["screen_timeout"].config_field == 12

    def test_no_two_selects_share_a_field(self) -> None:
        fields = [e.config_field for e in DELTA3_SELECT_FIELDS.values()]
        assert len(fields) == len(set(fields))

    def test_no_select_field_collides_with_another_control(self) -> None:
        """13, 10, 11 and 12 are low numbers in a crowded namespace.

        Every other ConfigWrite field this integration writes belongs in here,
        not just the switch and number tables - the charge mode, the energy
        backup and the port priority all write fields of their own.
        """
        from ecoflow_energy.ecoflow.delta3_commands import (
            AC_CHARGE_MODE_FIELD,
            DELTA3_ENERGY_BACKUP_FIELD,
            DELTA3_NUMBER_PARAMS,
            DELTA3_SWITCH_PARAMS,
            PORT_PRIORITY_FIELD,
        )

        taken = {e.config_field for e in DELTA3_SWITCH_PARAMS.values()}
        taken |= {e.config_field for e in DELTA3_NUMBER_PARAMS.values()}
        taken |= {AC_CHARGE_MODE_FIELD, DELTA3_ENERGY_BACKUP_FIELD, PORT_PRIORITY_FIELD}
        clashing = {
            key
            for key, entry in DELTA3_SELECT_FIELDS.items()
            if entry.config_field in taken
        }
        assert not clashing, f"select fields already used by another control: {clashing}"

    def test_no_params_key_is_silently_overwritten(self) -> None:
        """The reverse lookup is built by dict splat.

        Two controls sharing a params key would not raise - the later table
        would simply win, and one of them would start writing the other's
        field. Counting is the cheapest way to notice.
        """
        from ecoflow_energy.ecoflow.delta3_commands import (
            _PARAMS_KEY_TO_FIELD,
            DELTA3_NUMBER_PARAMS,
            DELTA3_SWITCH_PARAMS,
        )

        expected = (
            len(DELTA3_SWITCH_PARAMS)
            + len(DELTA3_NUMBER_PARAMS)
            + len(DELTA3_SELECT_FIELDS)
            + 1  # the AC charge mode, which has no table of its own
        )
        assert len(_PARAMS_KEY_TO_FIELD) == expected
