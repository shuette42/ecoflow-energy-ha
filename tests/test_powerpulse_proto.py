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
from datetime import datetime
from pathlib import Path
from typing import Any

from ecoflow_energy.ecoflow.parsers.powerpulse_proto import (
    _finalize,
    parse_powerpulse_message,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

FIXTURE = Path(__file__).parent / "fixtures" / "powerpulse" / "c376_frames_plan132.json"
IDLE_LIMIT_FIXTURE = FIXTURE.with_name("c376_idle_limit_steps_20260910.json")
PARAM_SET_ECHO_FIXTURE = FIXTURE.with_name("c376_param_set_echo_20260824.json")
PARAM_SET_ECHO_SWEEP_FIXTURE = FIXTURE.with_name("c376_param_set_echo_20260910.json")
CHARGE_MODE_ECHO_FIXTURE = FIXTURE.with_name("c376_charging_mode_echo_20260913.json")


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
    assert result["ev_max_current_a"] == 16.0
    assert result["ev_charge_current_a"] == 10.0
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
    assert result["ev_charge_current_a"] == 16.0
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


def test_zero_lifetime_counter_is_withheld_not_published() -> None:
    """Field 44 == 0 must not surface as `ev_total_energy_wh` == 0.0.

    No frame in this corpus ever reports the lifetime counter as zero, so
    the guard that withholds it is exercised directly against the mapping
    step's input rather than a captured frame - a `total_increasing` sensor
    reads a published 0.0 as a meter reset, and this is the only such
    sensor this parser feeds.
    """
    result = _finalize({"_total_energy_wh_raw": 0})
    assert "ev_total_energy_wh" not in result


def test_cable_lock_false_value_reported() -> None:
    """The cable lock's False path, built by hand.

    A second capture on 2026-09-09 measured field 19 as 0 before the lock
    engaged and 1 afterwards; that capture is not part of this corpus, so
    the False path is exercised against the mapping step's input directly.
    """
    result = _finalize({"_cable_lock_raw": 0})
    assert result["ev_cable_lock_enabled"] is False


def test_param_report_carries_the_configured_maximum_current() -> None:
    """`2/34` field 9 becomes `ev_max_current_a` (PLAN-146).

    The 2026-08-24 recording's seven `ParamReport` frames: the first echoes
    the 11 A write that triggered it, the remaining six echo a later 16 A
    write. The 2026-09-10 sweep gives four more values end to end.
    """
    frames = json.loads(PARAM_SET_ECHO_FIXTURE.read_text())["frames"]
    expected_by_index = {0: 11.0, 1: 16.0, 2: 16.0, 4: 16.0, 6: 16.0, 8: 16.0, 9: 16.0}
    for index, value in expected_by_index.items():
        result = parse_powerpulse_message(bytes.fromhex(frames[index]["hex"]))
        assert result is not None
        assert result["ev_max_current_a"] == value

    sweep_frames = json.loads(PARAM_SET_ECHO_SWEEP_FIXTURE.read_text())["frames"]
    for index, value in enumerate((6.0, 7.0, 14.0, 7.0)):
        result = parse_powerpulse_message(bytes.fromhex(sweep_frames[index]["hex"]))
        assert result is not None
        assert result["ev_max_current_a"] == value


def test_heartbeat_and_param_report_agree_on_the_key() -> None:
    """The three `2/33` HeartBeat frames of the same recording feed the same
    `ev_max_current_a` key as the `2/34` ParamReport frames (PLAN-146)."""
    frames = json.loads(PARAM_SET_ECHO_FIXTURE.read_text())["frames"]
    for index in (3, 5, 7):
        result = parse_powerpulse_message(bytes.fromhex(frames[index]["hex"]))
        assert result is not None
        assert result["ev_max_current_a"] == 16.0


def test_unmapped_enum_numbers_drop_the_key_instead_of_writing_none() -> None:
    """An unrecognised enum number must drop its key, not publish `None`.

    The sibling PowerOcean parser drops the same shape for the same key
    (`ev_charge_status`) because an unmapped value would crash the enum
    sensor with "not in list of options"; the same reasoning applies to
    all three enum fields this parser resolves.
    """
    assert "ev_charge_status" not in _finalize({"_plug_status_raw": 99})
    assert "ev_session_status" not in _finalize({"_session_status_raw": 99})
    assert "ev_phase_mode" not in _finalize({"_phase_mode_raw": 99})


def test_phase_mode_and_session_status_counts_over_the_fixture() -> None:
    """Exact counts over the fixture, not just "at least one".

    Six of the ten frames report a single-phase charge and two report an
    idle session; a mapping that silently swapped or dropped one of the two
    enum values would still leave the suite green without this check.
    """
    single_phase = 0
    idle = 0
    for frame in _frames():
        result = _parse(frame["ts_iso"])
        assert result is not None
        if result.get("ev_phase_mode") == "single_phase":
            single_phase += 1
        if result.get("ev_session_status") == "idle":
            idle += 1
    assert single_phase == 6
    assert idle == 2


def test_session_start_plus_duration_tracks_capture_time_on_property_frames() -> None:
    """Field 40 is a unix timestamp, field 41 is a small seconds counter,
    and their sum tracks the capture time within 0-3 s on live pushes.

    Checked on the `property`-topic frames only: the three `get_reply`
    bundles are a config read-back taken well after their own capture
    timestamp (23-83 s in this corpus, not a live push), so the identity
    does not hold for them and they are excluded rather than asserted on.
    The ordering check below (`duration < start`) is what actually tells
    the two fields apart - the sum used for the drift check is symmetric
    in both operands and would not notice them being swapped.
    """
    checked = 0
    for frame in _frames():
        if frame["topic"] != "property":
            continue
        result = _parse(frame["ts_iso"])
        assert result is not None
        if "ev_session_start_ts" not in result:
            continue
        start = result["ev_session_start_ts"]
        duration = result["ev_session_duration_s"]
        # A 2026 unix timestamp is on the order of 1.7e9; no session in this
        # corpus runs anywhere near that many seconds. If the two fields
        # were swapped, this ordering would flip.
        assert start > 1_700_000_000
        assert 0 <= duration < start
        capture_epoch = int(datetime.fromisoformat(frame["ts_iso"]).timestamp())
        drift = capture_epoch - start - duration
        assert 0 <= drift <= 3
        checked += 1
    assert checked == 5


def test_maximum_current_follows_the_configured_limit_on_an_idle_wallbox() -> None:
    """Field 18 is the limit; field 17 is the session setpoint (#7, 2026-09-10).

    The second owner stepped his limit from 16 A down to 6 A on an idle
    wallbox and the first pre-release's sensor stayed at 6 A throughout. In
    his download the field the sensor read (17) is 60 in every heartbeat
    while the field beside it (18) is 100 and then 60 at the minutes he
    chose 10 A and 6 A. Both frames pin both keys, so swapping the two
    fields back fails on the first frame and not only on the second.
    """
    frames = json.loads(IDLE_LIMIT_FIXTURE.read_text())["frames"]
    assert len(frames) == 2
    for frame in frames:
        result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))
        assert result is not None, frame["ts_iso"]
        assert result["ev_max_current_a"] == float(frame["configured_max_current_a"])
        assert result["ev_charge_current_a"] == 6.0
        assert result["ev_charge_status"] == "available"
        assert result["ev_phase_mode"] == "single_phase"
        assert "ev_session_energy_wh" not in result


# --- The second prefix, C374 (#7, recording of 2026-09-11) ---------------
#
# Six of the eight HeartBeats kept from a 30-minute recording of several
# short charging sessions (four session starts in the kept frames: one drawing
# 16 A per phase from the grid, the others solar-controlled at 9.5 to 12.4 A
# setpoints) on a wallbox that reports under `C374`. The two frames the
# builder leaves out read as an identifier run in their masked bytes; the one
# at 13:14:40, status `preparing` with every session field at 0, is covered
# by the `_finalize` test below instead.

C374_FIXTURE = FIXTURE.with_name("c374_frames_20260911.json")


def _c374_results() -> list[tuple[str, dict[str, Any]]]:
    out = []
    for frame in json.loads(C374_FIXTURE.read_text())["frames"]:
        result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))
        assert result is not None, frame["ts_iso"]
        out.append((frame["ts_iso"], result))
    return out


def test_c374_frames_decode_with_the_c376_field_map() -> None:
    """Same envelope, same fields, same scaling: every C374 HeartBeat parses
    and the lifetime identity `44 - 43 == 42` holds on all of them.

    The floor is the fixture's own frame count, so a fixture rebuilt with
    fewer frames fails here rather than passing on a smaller set.
    """
    results = _c374_results()
    assert len(results) == 6
    for ts_iso, result in results:
        assert result["ev_max_current_a"] == 20.0, ts_iso
        assert result["ev_phase_mode"] == "three_phase", ts_iso
        assert (
            result["ev_total_energy_wh"] - result["ev_session_start_energy_wh"]
            == result["ev_session_energy_wh"]
        ), ts_iso
    totals = [r["ev_total_energy_wh"] for _, r in results]
    assert totals == [688, 1651, 2273, 2882, 3432, 3629]


def test_c374_power_is_below_and_near_the_per_phase_sum() -> None:
    """The three charging frames: 11.4 kW at 15.9 A, 8.7 kW at 12.1 A and
    6.6 kW at 9.2 A, each just under the sum of the three phase products.
    """
    checked = 0
    for ts_iso, result in _c374_results():
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
        assert 0.9 * phase_sum < result["ev_charge_power_w"] < phase_sum, ts_iso
        assert result["ev_charge_status"] == "charging", ts_iso
        checked += 1
    assert checked == 3


def test_c374_reports_preparing_between_sessions() -> None:
    """Plug status 2 is `preparing`: cable attached, nothing flowing.

    Neither C376 recording ever showed it. In the C374 recording it sits
    between two sessions at 13:37:00 with power 0, currents at the noise
    floor and the session status at idle, and the next session starts six
    minutes later. Before the entry the parser dropped the value and the
    sensor kept reading `finishing` from the session before.
    """
    by_ts = {ts[11:19]: r for ts, r in _c374_results()}
    result = by_ts["13:37:00"]
    assert result["ev_charge_status"] == "preparing"
    assert result["ev_session_status"] == "idle"
    assert result["ev_charge_power_w"] == 0.0
    # The session fields still describe the session that just ended, the
    # same way they do in `finishing`; only `available` withholds them.
    assert result["ev_session_energy_wh"] == 916
    assert result["ev_session_start_ts"] == 1789133366
    statuses = [r["ev_charge_status"] for _, r in _c374_results()]
    assert statuses.count("preparing") == 1
    assert statuses.count("charging") == 3
    assert statuses.count("finishing") == 2


def test_zero_session_start_timestamp_is_withheld_not_published() -> None:
    """A start timestamp of 0 never reaches the timestamp sensor.

    The first C374 frame (13:14:40, left out of the fixture by the
    identifier-run gate) carries status `preparing` with fields 40 to 43
    all 0. Status 2 does not withhold the session fields, so without the
    guard the sensor would show 1970-01-01. The other three zeros are real
    readings and stay.
    """
    result = _finalize(
        {
            "_plug_status_raw": 2,
            "_session_start_ts_raw": 0,
            "_session_duration_s_raw": 0,
            "_session_energy_wh_raw": 0,
            "_session_start_energy_wh_raw": 0,
        }
    )
    assert result["ev_charge_status"] == "preparing"
    assert "ev_session_start_ts" not in result
    assert result["ev_session_duration_s"] == 0
    assert result["ev_session_energy_wh"] == 0
    assert result["ev_session_start_energy_wh"] == 0


# --- Charging mode, field 63 sub-field 4 (PLAN-147, #7, capture of
# 2026-09-13) ---------------------------------------------------------------


def _charge_mode_echo_frames() -> list[dict[str, Any]]:
    return json.loads(CHARGE_MODE_ECHO_FIXTURE.read_text())["frames"]


def _parse_charge_mode_echo(ts_iso: str) -> dict[str, Any] | None:
    for frame in _charge_mode_echo_frames():
        if frame["ts_iso"].startswith(ts_iso):
            return parse_powerpulse_message(bytes.fromhex(frame["hex"]))
    raise AssertionError(f"no frame captured at {ts_iso}")


def test_charge_mode_echo_reports_solar_then_smart_then_solar() -> None:
    """The three mode-write echoes actually kept in the fixture: the
    identifier gate dropped the heartbeat that would have carried the
    Custom write's echo (`3`, see the fixture note in PLAN-147), so only
    the Solar and Smart transitions are checked here."""
    result = _parse_charge_mode_echo("2026-09-13T22:49:14.763")
    assert result is not None
    assert result["ev_charge_mode"] == "solar"

    result = _parse_charge_mode_echo("2026-09-13T22:50:12.810")
    assert result is not None
    assert result["ev_charge_mode"] == "smart"

    result = _parse_charge_mode_echo("2026-09-13T22:51:13.907")
    assert result is not None
    assert result["ev_charge_mode"] == "solar"


def test_charge_mode_baseline_heartbeats_report_solar() -> None:
    """The three heartbeats captured before any write in the recording all
    report Solar - the mode the wallbox was already in."""
    for ts_iso in (
        "2026-09-13T22:37:13",
        "2026-09-13T22:39:13",
        "2026-09-13T22:42:14",
    ):
        result = _parse_charge_mode_echo(ts_iso)
        assert result is not None
        assert result["ev_charge_mode"] == "solar", ts_iso


def test_charge_mode_param_report_frames_carry_no_mode() -> None:
    """Field 63 lives on the HeartBeat (2/33) only - the three `2/34`
    ParamReport frames in the same fixture report no charging mode at all."""
    for frame in _charge_mode_echo_frames():
        if frame["cmds"][0]["cmd_id"] != 34:
            continue
        result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))
        assert result is not None
        assert "ev_charge_mode" not in result


def _synthetic_heartbeat_with_charge_mode(work_mode: int) -> bytes:
    """A synthetic, unmasked `2/33` heartbeat carrying only field 63
    (`po_linkage_param`) sub-field 4 (`work_mode`) - built by hand because
    Fast (1) and Custom (3) fell to the fixture's identifier gate and are
    not decodable from the capture (PLAN-147 evidence table)."""
    po_linkage = (
        encode_field_varint(1, 1)
        + encode_field_varint(2, 1)
        + encode_field_bytes(3, b"X" * 16)
        + encode_field_varint(4, work_mode)
        + encode_field_varint(5, 1)
    )
    pdata = encode_field_bytes(63, po_linkage)
    header = (
        encode_field_bytes(1, pdata)
        + encode_field_varint(8, 2)
        + encode_field_varint(9, 33)
    )
    return encode_field_bytes(1, header)


def test_charge_mode_synthetic_fast_and_custom() -> None:
    """Fast (1) and Custom (3), the two wire values absent from the fixture,
    decode correctly from a hand-built frame using the same field layout."""
    for work_mode, expected in ((1, "fast"), (3, "custom")):
        result = parse_powerpulse_message(
            _synthetic_heartbeat_with_charge_mode(work_mode)
        )
        assert result is not None
        assert result["ev_charge_mode"] == expected


def test_charge_mode_unmapped_number_drops_the_key() -> None:
    """Same reasoning as the other three enum fields this parser resolves:
    an unrecognised `work_mode` must drop the key, not publish `None`."""
    assert "ev_charge_mode" not in _finalize({"_charge_mode_raw": 9})


def test_charge_mode_reported_on_every_plan132_frame() -> None:
    """Field 63 is a genuine HeartBeat field, not one this recording happens
    to omit: all ten PLAN-132 frames (a different session, issue #7,
    2026-09-07) carry it, every one reporting Custom - the mode that
    session's wallbox was set to throughout."""
    checked = 0
    for frame in _frames():
        result = _parse(frame["ts_iso"])
        assert result is not None
        assert result["ev_charge_mode"] == "custom"
        checked += 1
    assert checked == 10


def test_charge_mode_on_every_heartbeat_in_the_corpus() -> None:
    """Every `2/33` in every powerpulse fixture carries the linkage record
    with a mapped mode: 46 heartbeats in seven fixtures from six recordings
    on 2026-09-14, with and without a PowerOcean on the account. The floor
    sits at that count rather than at 1, so a parser that quietly stopped
    reading the field on most frames, or a fixture that lost its heartbeats,
    is caught (`verify-the-checker`, shape 3)."""
    from ecoflow_energy.ecoflow.proto.decoder import decode_header_message

    heartbeats = 0
    modes: set[str] = set()
    for path in sorted(FIXTURE.parent.glob("*.json")):
        payload = json.loads(path.read_text())
        frames = payload.get("frames") or payload.get("pushes") or []
        for frame in frames:
            raw = bytes.fromhex(frame.get("hex") or frame.get("frame_hex") or "")
            headers, _ = decode_header_message(raw)
            if not any(
                h.get("cmd_func") == 2 and h.get("cmd_id") == 33 for h in headers
            ):
                continue
            result = parse_powerpulse_message(raw)
            assert result is not None, (path.name, frame.get("ts_iso"))
            assert "ev_charge_mode" in result, (path.name, frame.get("ts_iso"))
            heartbeats += 1
            modes.add(result["ev_charge_mode"])
    assert heartbeats >= 46
    assert modes == {"fast", "solar", "custom", "smart"}


def test_charge_mode_c374_frames_report_fast_then_solar() -> None:
    """The one real-frame source for `fast`: the `C374` recording (#7,
    2026-09-11) carries mode 1 on its first two heartbeats and mode 2 on the
    four that follow. Its owner described the sessions as "from the grid at
    the full rate" and then "solar-controlled at lower currents", which is
    the same order - an independent reading of 1 and 2 from a second owner
    and a second wallbox, without a write to line them up against."""
    modes = [(ts[11:19], result["ev_charge_mode"]) for ts, result in _c374_results()]
    assert modes == [
        ("13:19:18", "fast"),
        ("13:24:21", "fast"),
        ("13:32:28", "solar"),
        ("13:37:00", "solar"),
        ("13:42:03", "solar"),
        ("13:44:51", "solar"),
    ]


def test_charge_mode_absent_from_a_message_with_no_field_63() -> None:
    """Positive control the other way: `EDevRunDataSync` (241/44) is a
    different message entirely and never carries field 63, so every frame
    of that fixture reports no charging mode - the key is genuinely
    conditional on the field being present, not defaulted from elsewhere."""
    run_data_sync_fixture = FIXTURE.with_name("c376_run_data_sync_20260824.json")
    frames = json.loads(run_data_sync_fixture.read_text())["frames"]
    assert len(frames) > 0
    for frame in frames:
        result = parse_powerpulse_message(bytes.fromhex(frame["hex"]))
        if result is None:
            continue
        assert "ev_charge_mode" not in result


def test_charge_mode_field_does_not_disturb_existing_keys() -> None:
    """Adding field 63 to the HeartBeat field loop must not change any of
    the values already asserted for this frame elsewhere in this file."""
    result = _parse("2026-09-07T13:11:27")
    assert result is not None
    assert result["ev_charge_status"] == "charging"
    assert result["ev_max_current_a"] == 16.0
    assert result["ev_charge_current_a"] == 10.0
    assert result["ev_phase_mode"] == "three_phase"
    assert result["ev_charge_power_w"] == 6599.2
