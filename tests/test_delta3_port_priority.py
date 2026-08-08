"""Tests for Delta 3 port priority.

Every value pinned here was read off a D3M1 on 2026-08-04, with the EcoFlow
app open on the port priority screen at the same moment, so the wire meaning
is anchored to a state a human could see rather than to a guess. Two states
were recorded: the screen as it opened, and the screen after each cutoff
slider had been dragged to both of its ends.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.const import (
    DELTA3_NUMBERS,
    DELTA3_SWITCHES,
    DEVICE_TYPE_DELTA3,
    excluded_keys_for_serial,
    filter_defs_for_serial,
)
from custom_components.ecoflow_energy.ecoflow.const import _SN_PREFIX_MAP
from custom_components.ecoflow_energy.ecoflow.delta3_commands import (
    PORT_PRIORITY_FIELD,
    build_port_priority_command,
    build_proto_command,
    encode_port_priority_item,
    port_priority_soc_bounds,
)
from custom_components.ecoflow_energy.ecoflow.parsers.delta3_proto import (
    parse_delta3_display_property,
    port_priority_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto import ecocharge_pb2 as pb
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)

# The opening state of the first capture. The app showed AC 1 essential, AC 2
# non-essential at 30 %, DC essential - and exactly one item carries the
# enable flag, which is what proves the flag means "non-essential".
OPENING_LIST_HEX = "0a04100118230a04100218050a0608011003181e"

# The last frame of the second capture, after the cutoff slider was dragged to
# each end: AC 1 stopped at 5, AC 2 at 95, while the scale still read 0-100.
# This is the only evidence tying `port_priority_soc_bounds` to the device
# rather than to the formula, so it is asserted rather than just recorded.
BOUNDS_LIST_HEX = "0a04100118230a060801100218050a0608011003185f"


def _display_fields(list_hex: str, active_flag: int | None = None) -> dict:
    """Decode a port priority payload the way the runtime decoder would."""
    from google.protobuf.json_format import MessageToDict

    msg = pb.Delta3DisplayProperty()
    msg.power_outages_list.ParseFromString(bytes.fromhex(list_hex))
    if active_flag is not None:
        msg.power_outages_active_flag = active_flag
    return MessageToDict(msg, preserving_proto_field_name=True)


class TestParser:
    """The read-back carries all three ports on every push."""

    def test_opening_state_matches_what_the_app_showed(self) -> None:
        result = parse_delta3_display_property(_display_fields(OPENING_LIST_HEX))

        assert result["port_priority_ac1_limited"] is False
        assert result["port_priority_ac2_limited"] is True
        assert result["port_priority_dc_limited"] is False
        assert result["port_priority_ac2_cutoff_soc"] == 30

    def test_absent_enable_flag_reads_as_essential(self) -> None:
        """proto3 omits the false case, so a missing flag must not be sticky."""
        result = parse_delta3_display_property(_display_fields(OPENING_LIST_HEX))

        # DC and AC 1 carry no enable field at all in this payload.
        assert result["port_priority_dc_limited"] is False
        assert result["port_priority_ac1_limited"] is False

    def test_cutoff_survives_while_a_port_is_essential(self) -> None:
        """DC held 35 % with the flag off - the value is a stored preference."""
        result = parse_delta3_display_property(_display_fields(OPENING_LIST_HEX))

        assert result["port_priority_dc_cutoff_soc"] == 35

    def test_active_flag_is_true_only_for_one(self) -> None:
        assert (
            parse_delta3_display_property(
                _display_fields(OPENING_LIST_HEX, active_flag=1)
            )["port_priority_active"]
            is True
        )
        # Both values were observed on a D3M1 by cutting mains ahead of the
        # unit: 1 arrived in the same frame that reported the AC input at 0 W,
        # 2 came back when mains returned.
        assert (
            parse_delta3_display_property(
                _display_fields(OPENING_LIST_HEX, active_flag=2)
            )["port_priority_active"]
            is False
        )

    def test_frame_without_port_priority_yields_no_keys(self) -> None:
        result = parse_delta3_display_property({"cms_batt_soc": 99.0})

        assert not [k for k in result if k.startswith("port_priority_")]

    def test_unknown_port_type_is_skipped(self) -> None:
        """Type 0 is the enum null member; the app skips those items too."""
        fields = {
            "power_outages_list": {
                "power_outage_item": [
                    {"power_outage_port_type": 0, "power_outage_min_soc": 20},
                    {"power_outage_port_type": 2, "power_outage_min_soc": 20},
                ]
            }
        }
        result = parse_delta3_display_property(fields)

        assert result["port_priority_ac1_cutoff_soc"] == 20
        assert len([k for k in result if k.endswith("_cutoff_soc")]) == 1

    def test_key_helper_matches_the_parser_output(self) -> None:
        limited, cutoff = port_priority_keys("ac2")

        assert limited == "port_priority_ac2_limited"
        assert cutoff == "port_priority_ac2_cutoff_soc"


class TestBounds:
    """The cutoff range is derived from the battery's own limits."""

    def test_measured_case(self) -> None:
        """With limits at 100/0 the app slider stopped at 5 and 95."""
        assert port_priority_soc_bounds(100, 0) == (5, 95)

    def test_limits_inside_the_anchors_narrow_the_range(self) -> None:
        assert port_priority_soc_bounds(80, 20) == (25, 75)

    def test_anchors_cap_the_range(self) -> None:
        """A discharge limit above 30 or a charge limit below 50 is clamped."""
        assert port_priority_soc_bounds(40, 60) == (35, 45)

    def test_missing_limits_fall_back_to_the_widest_range(self) -> None:
        assert port_priority_soc_bounds(None, None) == (5, 95)

    def test_the_formula_reproduces_the_slider_ends_the_device_reported(
        self,
    ) -> None:
        """The bounds capture, not the formula, is what pins these numbers.

        Both sliders were dragged to their extremes with the battery limits at
        their defaults; whatever the device stored is what the app allowed.
        """
        result = parse_delta3_display_property(_display_fields(BOUNDS_LIST_HEX))
        lower, upper = port_priority_soc_bounds(100, 0)

        assert result["port_priority_ac1_cutoff_soc"] == lower == 5
        assert result["port_priority_ac2_cutoff_soc"] == upper == 95

    @pytest.mark.parametrize(
        ("max_charge", "min_discharge"),
        [(100, 0), (80, 20), (40, 60), (50, 30), (None, None), (0, 100)],
    )
    def test_the_anchors_keep_the_range_from_inverting(
        self, max_charge: int | None, min_discharge: int | None
    ) -> None:
        """No clamp guards this, so the anchors have to carry it themselves.

        Lower can never exceed 30 + 5 and upper can never fall below 50 - 5.
        Moving either anchor past the other would silently produce a slider
        with no usable range, so the invariant is asserted rather than fixed
        up at runtime.
        """
        lower, upper = port_priority_soc_bounds(max_charge, min_discharge)

        assert lower <= 35
        assert upper >= 45
        assert upper > lower


class TestWriter:
    """One item per write, matching what the app sends."""

    def test_item_encoding_matches_a_device_reported_item(self) -> None:
        """Byte-for-byte the AC 2 entry the device sent back at 30 %."""
        assert encode_port_priority_item(3, True, 30).hex() == "0a0608011003181e"

    def test_frame_carries_field_376_as_a_submessage(self) -> None:
        command = build_port_priority_command("ac2", True, 40)
        frame = build_proto_command(command, "D3M1TESTSERIAL01", seq=1234)
        headers, _ = decode_header_message(frame)
        header = headers[0]
        pdata = bytes.fromhex(header["pdata"])

        assert header["cmd_func"] == 254
        assert header["cmd_id"] == 17
        # 376 << 3 | wire type 2
        assert pdata.hex().startswith("c217")
        assert pdata.hex() == "c217080a06080110031828"

    def test_frame_round_trips_through_the_read_back_message(self) -> None:
        """What we write must decode as what the device sends us."""
        command = build_port_priority_command("dc", False, 35)
        frame = build_proto_command(command, "D3M1TESTSERIAL01", seq=7)
        pdata = bytes.fromhex(decode_header_message(frame)[0][0]["pdata"])

        # Strip the field 376 header (tag + length) and parse the payload.
        body = pdata[3:]
        parsed = pb.Delta3PowerOutagesList()
        parsed.ParseFromString(body)

        assert len(parsed.power_outage_item) == 1
        item = parsed.power_outage_item[0]
        assert item.power_outage_port_type == 1
        assert item.power_outage_port_enable is False
        assert item.power_outage_min_soc == 35

    @pytest.mark.parametrize(
        ("stem", "port_type"), [("dc", 1), ("ac1", 2), ("ac2", 3)]
    )
    def test_port_enum_follows_the_app(self, stem: str, port_type: int) -> None:
        command = build_port_priority_command(stem, True, 40)

        assert command["params"]["cfgPowerOutagesList"]["portType"] == port_type

    def test_unknown_port_returns_none(self) -> None:
        assert build_port_priority_command("ac3", True, 40) is None

    def test_field_number_is_pinned(self) -> None:
        assert PORT_PRIORITY_FIELD == 376


class TestVariantGating:
    """Only the serials whose app shows the screen get the entities."""

    PORT_PRIORITY_SWITCHES = {
        "port_priority_ac1_switch",
        "port_priority_ac2_switch",
        "port_priority_dc_switch",
    }
    PORT_PRIORITY_NUMBERS = {
        "port_priority_ac1_soc",
        "port_priority_ac2_soc",
        "port_priority_dc_soc",
    }

    def test_max_plus_keeps_them(self) -> None:
        keys = {d.key for d in filter_defs_for_serial(DELTA3_SWITCHES, "D3M1TEST00000001")}

        assert self.PORT_PRIORITY_SWITCHES <= keys

    @pytest.mark.parametrize(
        "serial",
        ["P231TEST00000001", "P321TEST00000001", "D3N1TEST00000001"],
    )
    def test_other_delta3_variants_do_not(self, serial: str) -> None:
        """The DELTA 3 Max is here because the app gates the menu on a
        serial starting D3M or D51, and D3N1 is neither."""
        switch_keys = {d.key for d in filter_defs_for_serial(DELTA3_SWITCHES, serial)}
        number_keys = {d.key for d in filter_defs_for_serial(DELTA3_NUMBERS, serial)}

        assert not (self.PORT_PRIORITY_SWITCHES & switch_keys)
        assert not (self.PORT_PRIORITY_NUMBERS & number_keys)

    def test_other_delta3_variants_keep_everything_else(self) -> None:
        keys = {d.key for d in filter_defs_for_serial(DELTA3_SWITCHES, "P231TEST00000001")}

        assert "ac_out_switch" in keys
        assert "bypass_out_disable_switch" in keys

    def test_state_keys_are_excluded_too(self) -> None:
        """The number platform matches on state_key, not just key."""
        excluded = excluded_keys_for_serial("P231TEST00000001")

        assert "port_priority_ac1_cutoff_soc" in excluded
        assert "port_priority_active" in excluded

    def test_every_non_d3m_delta3_prefix_is_denied(self) -> None:
        """The gate is a denylist, so a new prefix defaults to *included*.

        That is the wrong default for a feature the app only offers on part
        of the family: a Delta 3 prefix added to the device-type map without
        a decision here would create seven entities that can never fill. This
        pins the decision to CI instead of to whoever remembers.
        """
        delta3_prefixes = {
            prefix
            for prefix, device_type in _SN_PREFIX_MAP.items()
            if device_type == DEVICE_TYPE_DELTA3
        }
        undecided = {
            prefix
            for prefix in delta3_prefixes
            if not prefix.startswith("D3M")
            and not excluded_keys_for_serial(prefix + "X" * 12)
        }

        assert not undecided, (
            f"Delta 3 prefixes {sorted(undecided)} would get port priority "
            "entities by default - add them to _SN_PREFIX_EXCLUDED_KEYS or "
            "confirm the app offers the setting on them."
        )


class TestEntityReach:
    """These values travel on the push path only."""

    def test_every_port_priority_control_is_enhanced_only(self) -> None:
        controls = [
            d
            for d in (*DELTA3_SWITCHES, *DELTA3_NUMBERS)
            if d.key.startswith("port_priority_")
        ]

        assert len(controls) == 6
        assert all(d.enhanced_only for d in controls)
