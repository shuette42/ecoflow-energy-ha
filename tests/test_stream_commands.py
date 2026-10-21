"""Tests for the Stream (BK-series) SET command builders."""

from __future__ import annotations

from ecoflow_energy.const import (
    STREAMFEEDIN_SWITCHES,
    supports_stream_controls,
    supports_stream_feed_in_control,
)
from ecoflow_energy.ecoflow.stream_commands import build_feed_in_control_command


class TestFeedInControlCommand:
    """Body documented for the Developer API Stream "Feed-in control"."""

    def test_turn_on_body(self) -> None:
        assert build_feed_in_control_command(True) == {
            "cmdId": 17,
            "cmdFunc": 254,
            "dirDest": 1,
            "dirSrc": 1,
            "dest": 2,
            "needAck": True,
            "params": {"cfgFeedGridMode": 2},
        }

    def test_turn_off_body(self) -> None:
        assert build_feed_in_control_command(False) == {
            "cmdId": 17,
            "cmdFunc": 254,
            "dirDest": 1,
            "dirSrc": 1,
            "dest": 2,
            "needAck": True,
            "params": {"cfgFeedGridMode": 1},
        }

    def test_body_carries_no_serial(self) -> None:
        """The HTTP client adds `sn`; the builder must not."""
        assert "sn" not in build_feed_in_control_command(True)


class TestFeedInControlGate:
    def test_only_bk61_is_offered_the_switch(self) -> None:
        assert supports_stream_feed_in_control("BK61TEST00000001")
        assert supports_stream_feed_in_control("bk61test00000001")
        assert not supports_stream_feed_in_control("BK31TEST00000001")
        assert not supports_stream_feed_in_control("BK11TEST00000001")
        assert not supports_stream_feed_in_control("ES21TEST0000001")
        assert not supports_stream_feed_in_control("")

    def test_bk61_does_not_open_the_bk31_write_gate(self) -> None:
        assert not supports_stream_controls("BK61TEST00000001")

    def test_switch_definition_is_standard_only(self) -> None:
        (definition,) = STREAMFEEDIN_SWITCHES
        assert definition.key == "feed_in_control_switch"
        assert definition.state_key == "feed_in_control_enabled"
        assert definition.standard_only is True
        assert definition.enhanced_only is False
