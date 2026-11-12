"""Delta 3 USB output switch: which units get the entity (#685).

The switch is offered on a P231 only, through an allowlist, so a new Delta 3
prefix does not get it by default. The parser and command tests live in
tests/test_delta3_usb_switch.py, which needs no Home Assistant.
"""

from __future__ import annotations

from custom_components.ecoflow_energy.const import (
    DELTA3_SWITCHES,
    DELTA3_USB_CONTROL_PREFIXES,
    supports_delta3_usb_switch,
)
from custom_components.ecoflow_energy.ecoflow.const import DEVICE_TYPE_DELTA3
from custom_components.ecoflow_energy.switch import _get_switch_defs

P231_SN = "P231TESTDUMMY001"


class TestGate:
    def test_allowlist_is_p231_only(self) -> None:
        assert frozenset({"P231"}) == DELTA3_USB_CONTROL_PREFIXES
        assert supports_delta3_usb_switch(P231_SN)
        assert supports_delta3_usb_switch("p231testdummy001")
        assert not supports_delta3_usb_switch("")

    def test_switch_only_for_p231(self) -> None:
        keys = [d.key for d in _get_switch_defs(DEVICE_TYPE_DELTA3, P231_SN)]
        assert "usb_out_switch" in keys
        for sn in (
            "P232TESTDUMMY001",  # same first three characters as P231
            "P351TESTDUMMY001",
            "D3M1TESTDUMMY001",
            "R655TESTDUMMY001",
            "",
        ):
            keys = [d.key for d in _get_switch_defs(DEVICE_TYPE_DELTA3, sn)]
            assert "usb_out_switch" not in keys, sn

    def test_switch_is_enhanced_only_and_reads_usb_flow(self) -> None:
        (usb,) = [
            d
            for d in _get_switch_defs(DEVICE_TYPE_DELTA3, P231_SN)
            if d.key == "usb_out_switch"
        ]
        assert usb.enhanced_only is True
        assert usb.state_key == "usb_out_flow"

    def test_existing_switches_unchanged(self) -> None:
        """P231 keeps every existing Delta 3 switch, in order, under its key."""
        p231 = _get_switch_defs(DEVICE_TYPE_DELTA3, P231_SN)
        assert p231[: len(DELTA3_SWITCHES)] == DELTA3_SWITCHES
        assert [d.key for d in p231[len(DELTA3_SWITCHES) :]] == ["usb_out_switch"]
        assert _get_switch_defs(DEVICE_TYPE_DELTA3, "D3M1TESTDUMMY001") == (
            DELTA3_SWITCHES
        )
