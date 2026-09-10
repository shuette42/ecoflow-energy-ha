"""Tests for the EcoFlow PowerPulse 2 (C376) protobuf parser.

Every expectation below is a number the captured wallbox put on the wire
during a 10-frame, 7-hour charging session (PLAN-132, issue #7): an idle
plug, a single-phase charge, a three-phase charge and a finished session.
Values are written out per frame rather than recomputed from the parser,
because a test that derives its expectation from the code under test holds
for any field map.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecoflow_energy.ecoflow.parsers.powerpulse_proto import parse_powerpulse_message

FIXTURE = Path(__file__).parent / "fixtures" / "powerpulse" / "c376_frames_plan132.json"


def _frames() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["frames"]


def _parse(ts_iso: str) -> dict[str, Any] | None:
    """Parse the frame captured at this instant.

    Frames are addressed by their timestamp, never by their position. The
    fixture builder drops a frame whose masked bytes read as an identifier
    run, so a position is not stable across a rebuild - and an index-addressed
    test then silently asserts one frame's numbers against another's.
    """
    for frame in _frames():
        if frame["ts_iso"].startswith(ts_iso):
            return parse_powerpulse_message(bytes.fromhex(frame["hex"]))
    raise AssertionError(f"no frame captured at {ts_iso}")


def test_masked_property_frame_reports_charging_session() -> None:
    """The 13:11 push (property topic, XOR-masked) is a three-phase charge."""
    result = _parse("2026-09-07T13:11:27")
    assert result is not None
    assert result["ev_charge_status"] == "charging"
    assert result["ev_session_status"] == "charging"
    assert result["ev_charge_power_w"] == 6599.2
    assert result["ev_voltage_l1_v"] == 233.7
    assert result["ev_voltage_l2_v"] == 232.69
    assert result["ev_voltage_l3_v"] == 231.65
    assert result["ev_current_l1_a"] == 9.66
    assert result["ev_current_l2_a"] == 9.66
    assert result["ev_current_l3_a"] == 9.71
    assert result["ev_max_current_a"] == 10.0
    assert result["ev_phase_mode"] == "three_phase"
    assert result["ev_session_start_ts"] == 1788786565
    assert result["ev_session_duration_s"] == 120
    assert result["ev_session_energy_wh"] == 199
    assert result["ev_session_start_energy_wh"] == 102067
    assert result["ev_total_energy_wh"] == 102266


def test_unmasked_get_reply_bundle_reports_finished_session() -> None:
    """The 12:23 bundle (get_reply, unmasked) is the session that just ended."""
    result = _parse("2026-09-07T12:23:58")
    assert result is not None
    assert result["ev_charge_status"] == "finishing"
    assert result["ev_session_status"] == "finished"
    assert result["ev_charge_power_w"] == 0.0
    assert result["ev_voltage_l1_v"] == 233.12
    assert result["ev_voltage_l2_v"] == 231.84
    assert result["ev_voltage_l3_v"] == 232.0
    assert result["ev_current_l1_a"] == 0.0
    assert result["ev_current_l2_a"] == 0.0
    assert result["ev_current_l3_a"] == 0.0
    assert result["ev_max_current_a"] == 16.0
    assert result["ev_phase_mode"] == "three_phase"
    assert result["ev_session_start_ts"] == 1788782107
    assert result["ev_session_duration_s"] == 1648
    assert result["ev_session_energy_wh"] == 4911
    assert result["ev_session_start_energy_wh"] == 90626
    assert result["ev_total_energy_wh"] == 95537


def test_lifetime_counter_minus_session_start_equals_session_energy() -> None:
    """`44 - 43 == 42` holds in every frame that reports all three keys.

    Two of the ten captured frames withhold the session-scoped keys
    entirely (the plug is idle, see
    ``test_idle_frame_omits_session_energy_key`` below), so ``checked``
    only counts the eight frames where the identity is actually exercised -
    and the floor is that exact count, not 1, so a parser that only gets
    the identity right on a single lucky frame cannot pass.
    """
    checked = 0
    for frame in _frames():
        result = _parse(frame["ts_iso"])
        assert result is not None
        if (
            "ev_total_energy_wh" in result
            and "ev_session_start_energy_wh" in result
            and "ev_session_energy_wh" in result
        ):
            assert (
                result["ev_total_energy_wh"] - result["ev_session_start_energy_wh"]
                == result["ev_session_energy_wh"]
            )
            checked += 1
    assert checked == 8


def test_reported_power_is_below_and_near_the_per_phase_sum() -> None:
    """Reported power sits just under ``sum(V * I)`` per phase.

    Idle frames and the finished-session frame report currents at the
    device's own noise floor (~0.004 A); the 0.5 A cutoff below excludes
    those so the comparison only runs on frames with a real reading.
    ``checked`` counts the frames that clear that floor, and the assertion
    floor is that exact count - not 1 - so a parser that only gets the
    ratio right on one frame cannot pass.
    """
    checked = 0
    for frame in _frames():
        result = _parse(frame["ts_iso"])
        assert result is not None
        currents = (
            result["ev_current_l1_a"],
            result["ev_current_l2_a"],
            result["ev_current_l3_a"],
        )
        if max(currents) <= 0.5:
            continue
        voltages = (
            result["ev_voltage_l1_v"],
            result["ev_voltage_l2_v"],
            result["ev_voltage_l3_v"],
        )
        phase_sum = sum(v * i for v, i in zip(voltages, currents, strict=True))
        power = result["ev_charge_power_w"]
        assert power < phase_sum
        assert power > 0.9 * phase_sum
        checked += 1
    assert checked == 7


def test_idle_frame_omits_session_energy_key() -> None:
    """A plug with no session in progress (status 1) reports no session
    total at all - not even 0.0 (PLAN-132 contract: absence, not zero).
    """
    result = _parse("2026-09-07T06:46:45")
    assert result is not None
    assert result["ev_charge_status"] == "available"
    assert "ev_session_energy_wh" not in result
    assert "ev_session_start_energy_wh" not in result
    assert "ev_session_start_ts" not in result
    assert "ev_session_duration_s" not in result
    # The lifetime counter is not session-scoped and stays reported.
    assert result["ev_total_energy_wh"] == 84944


def test_cable_lock_reported_from_every_get_reply_bundle() -> None:
    """gun_lock_keep_locking_en (2/34 field 19) is 1 in all three bundles."""
    for ts_iso in ("2026-09-07T11:54:46", "2026-09-07T12:23:58", "2026-09-07T13:07:48"):
        result = _parse(ts_iso)
        assert result is not None
        assert result["ev_cable_lock_enabled"] is True
