"""Delta 3 LCD screen timeout: parser field, option map and wire payload.

The setting is a timeout in seconds, not an on/off switch - there is no field
anywhere in this device family that powers the panel down, and the vendor app
offers no such control either. What it does offer is six fixed steps, and the
value behind each one was read off a D3M1 rather than derived from a name.

The one thing worth guarding above all others: zero means "never", so the
screen stays lit. Three other rows of the same app settings page read "Never"
and carry 0 on the wire. An off-by-one in that direction would hand a user
reaching for a dark panel the exact opposite.

Entity behaviour is covered in tests/ha/test_delta3_screen_timeout_entity.py.
"""

from __future__ import annotations

import pytest

from ecoflow_energy.const import (
    DELTA3_SCREEN_TIMEOUT_KEY,
    DELTA3_SCREEN_TIMEOUT_STATE_KEY,
    DELTA3_SCREEN_TIMEOUT_VALUES,
    DELTA3_SELECTS,
)
from ecoflow_energy.ecoflow.delta3_commands import (
    SCREEN_TIMEOUT_FIELD,
    SCREEN_TIMEOUT_PARAMS_KEY,
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


def _frame(inner: bytes) -> bytes:
    """Wrap a status payload in the HeaderMessage envelope the device sends."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))
    header.extend(encode_field_varint(8, 254))
    header.extend(encode_field_varint(9, 21))
    header.extend(encode_field_bytes(24, DEVICE_SN.encode()))
    return encode_field_bytes(1, bytes(header))


def _decode(frame: bytes) -> dict:
    result = decode_proto_runtime_frame(frame)
    return {k: v for k, v in result.mapped.items() if not k.startswith("_")}


def _decoded(seconds: int) -> dict:
    """Encode a status frame carrying only the timeout, then decode it back."""
    msg = Delta3DisplayProperty()
    msg.screen_off_time = seconds
    return _decode(_frame(msg.SerializeToString()))


class TestParser:
    @pytest.mark.parametrize("seconds", sorted(DELTA3_SCREEN_TIMEOUT_VALUES))
    def test_every_app_step_survives_the_round_trip(self, seconds: int) -> None:
        parsed = parse_delta3_display_property(_decoded(seconds))
        assert parsed[DELTA3_SCREEN_TIMEOUT_STATE_KEY] == seconds

    def test_zero_is_reported_rather_than_dropped(self) -> None:
        """Proto3 omits a plain zero; the field is declared optional for this.

        Zero is the app's "Never" and therefore a real setting, not a missing
        one. Losing it would leave the entity showing the previous step while
        the device is set never to switch off.
        """
        parsed = parse_delta3_display_property(_decoded(0))
        assert parsed[DELTA3_SCREEN_TIMEOUT_STATE_KEY] == 0

    def test_absent_field_produces_no_key(self) -> None:
        """A frame that does not mention the timeout must not invent one."""
        msg = Delta3DisplayProperty()
        msg.pow_in_sum_w = 100.0
        parsed = parse_delta3_display_property(_decode(_frame(msg.SerializeToString())))
        assert DELTA3_SCREEN_TIMEOUT_STATE_KEY not in parsed

    def test_raw_seconds_not_a_label(self) -> None:
        """The parser stays in the device's vocabulary.

        A diagnostics download has to show what the device said, including a
        value outside the app's six steps. Labelling belongs to the entity.
        """
        parsed = parse_delta3_display_property(_decoded(47))
        assert parsed[DELTA3_SCREEN_TIMEOUT_STATE_KEY] == 47


class TestOptionMap:
    def test_zero_means_never_not_off(self) -> None:
        assert DELTA3_SCREEN_TIMEOUT_VALUES[0] == "never"

    def test_steps_match_the_app_list(self) -> None:
        assert dict(DELTA3_SCREEN_TIMEOUT_VALUES) == {
            10: "10_seconds",
            30: "30_seconds",
            60: "1_minute",
            300: "5_minutes",
            1800: "30_minutes",
            0: "never",
        }

    def test_shortest_timeout_is_offered_first(self) -> None:
        """The option closest to a dark screen must not be buried.

        The app lists ascending with "never" last, and the request behind this
        feature was a screen that goes dark. Ordering is the only protection
        against picking the opposite end by mistake.
        """
        defn = next(d for d in DELTA3_SELECTS if d.key == DELTA3_SCREEN_TIMEOUT_KEY)
        assert defn.options[0] == "10_seconds"
        assert defn.options[-1] == "never"

    def test_labels_are_unique(self) -> None:
        labels = list(DELTA3_SCREEN_TIMEOUT_VALUES.values())
        assert len(labels) == len(set(labels))

    def test_definition_is_push_only(self) -> None:
        """The polled quota carries no screen field, so developer keys get none."""
        defn = next(d for d in DELTA3_SELECTS if d.key == DELTA3_SCREEN_TIMEOUT_KEY)
        assert defn.enhanced_only is True


class TestCommand:
    def test_unknown_key_returns_none(self) -> None:
        assert build_select_command("work_mode", 10) is None

    def test_command_carries_the_params_key(self) -> None:
        command = build_select_command(DELTA3_SCREEN_TIMEOUT_KEY, 30)
        assert command is not None
        assert command["params"] == {SCREEN_TIMEOUT_PARAMS_KEY: 30}

    @pytest.mark.parametrize("seconds", sorted(DELTA3_SCREEN_TIMEOUT_VALUES))
    def test_wire_frame_carries_config_field_12(self, seconds: int) -> None:
        """The write field, verified against hardware on 2026-08-05.

        A write of 30 was acknowledged with config_ok=1 and read back on status
        field 18 within about two seconds.
        """
        command = build_select_command(DELTA3_SCREEN_TIMEOUT_KEY, seconds)
        assert command is not None
        payload = build_proto_command(command, DEVICE_SN)
        assert payload is not None
        expected = encode_field_bytes(
            1, encode_field_varint(SCREEN_TIMEOUT_FIELD, seconds)
        )
        assert expected in payload

    def test_write_field_is_not_the_readback_field(self) -> None:
        """Twelve writes it, eighteen reports it. They are different numbers."""
        assert SCREEN_TIMEOUT_FIELD == 12
