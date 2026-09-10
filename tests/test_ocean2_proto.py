"""Tests for the EcoFlow Ocean 2 (RE11/RE17) protobuf parser.

Every numeric expectation below was read directly off
``tests/fixtures/ocean2/re11_frames_plan135.json`` (PLAN-135, issue #145),
not copied from the field-pinning document that named the fixture. Two of
that document's own conclusions turned out to be backwards once checked
against the frames it was built from - the battery power sign on both the
system field (`65.20`) and the home-precedence expectation for one fixture -
and the parser module docstring carries the proof. This file's expectations
follow the parser, which follows the fixture; where the two disagreed, the
fixture won.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy.ecoflow.parsers.ocean2_proto import parse_ocean2_message
from ecoflow_energy.ecoflow.proto.decoder import _decode_single_header, _read_varint

FIXTURE = Path(__file__).parent / "fixtures" / "ocean2" / "re11_frames_plan135.json"


def _frames() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["frames"]


def _by_capture_index() -> dict[int, dict[str, Any]]:
    return {frame["capture_index"]: frame for frame in _frames()}


def _parse(capture_index: int) -> dict[str, Any] | None:
    frame = _by_capture_index()[capture_index]
    return parse_ocean2_message(bytes.fromhex(frame["hex"]))


def _payload(capture_index: int) -> bytes:
    return bytes.fromhex(_by_capture_index()[capture_index]["hex"])


def _split_header_spans(payload: bytes) -> list[bytes]:
    """Return the raw bytes of every top-level field-1 (header) entry.

    Mirrors ``decode_header_message``'s walk but keeps the header's raw span
    instead of decoding it, so a subset of headers can be re-serialized into
    a synthetic frame for the "unhandled headers only" test below.
    """
    mv = memoryview(payload)
    pos = 0
    spans: list[bytes] = []
    while pos < len(mv):
        tag, pos = _read_varint(mv, pos)
        if tag is None:
            break
        field_num, wire_type = tag >> 3, tag & 0x07
        if wire_type == 2:
            length, pos = _read_varint(mv, pos)
            if length is None:
                break
            span = mv[pos : pos + length].tobytes()
            pos += length
            if field_num == 1:
                spans.append(span)
        elif wire_type == 0:
            _, pos = _read_varint(mv, pos)
        elif wire_type == 5:
            pos += 4
        elif wire_type == 1:
            pos += 8
        else:
            break
    return spans


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _wrap_as_headers(spans: list[bytes]) -> bytes:
    """Re-serialize raw header spans into a minimal multi-header frame."""
    out = bytearray()
    for span in spans:
        out += _encode_varint((1 << 3) | 2)
        out += _encode_varint(len(span))
        out += span
    return bytes(out)


# --- display upload: full frames ---------------------------------------


def test_full_upload_frame_46_battery_charging_and_pv_strings() -> None:
    """Capture 46: three PV strings, an inverter phase reading, a charging
    battery (65.20 raw is +4328 here; SoC and remaining-Wh both rose in the
    surrounding frames, so the negated value is the correct charge-positive
    reading - see the parser module docstring)."""
    result = _parse(46)
    assert result is not None
    assert result["solar_w"] == 6687.0
    assert result["batt_w"] == pytest.approx(-4328.0)
    assert result["pcs_ac_power_w"] == pytest.approx(10476.576, abs=0.01)
    assert result["inv_phase_a_active_power_w"] == pytest.approx(3516, abs=1)
    for label in ("a", "b", "c"):
        assert f"inv_phase_{label}_voltage_v" in result
    for string in ("1", "2", "3"):
        assert f"mppt_pv{string}_voltage_v" in result


def test_full_upload_frame_36_negation_and_block87_precedence() -> None:
    """Capture 36: proves the 65.20 negation (raw -4999 -> output +4999,
    within 9 W of the untouched 87.4 fallback reading of +4990, matching the
    document's own note of a 9 W disagreement between the two blocks at this
    frame) and that block 87 wins home_w over block 7 (10 vs 630)."""
    result = _parse(36)
    assert result is not None
    assert result["batt_w"] == pytest.approx(4999.0)
    assert result["home_w"] == 10.0
    assert result["grid_w"] == pytest.approx(-373.80, abs=0.01)
    grid_voltage_keys = [
        key
        for key in result
        if key.startswith("grid_phase_") and key.endswith("_voltage_v")
    ]
    assert len(grid_voltage_keys) == 3


def test_full_upload_frame_28_mppt_and_pcs_ac_precision() -> None:
    """Capture 28: the unrounded pcs_ac_power_w (block 4) differs from the
    10 W-quantised home_w (block 87/7) because they are different readings,
    not a rounding of the same one - and string 3 reads 0 A / 0 W but is
    still published."""
    result = _parse(28)
    assert result is not None
    assert result["soc_pct"] == 99
    assert result["bp_remain_watth"] == 10047
    assert result["solar_w"] == 1635.0
    assert result["mppt_pv1_voltage_v"] == pytest.approx(511.647, abs=0.001)
    assert result["mppt_pv1_current_a"] == pytest.approx(1.96595, abs=0.00001)
    assert result["mppt_pv1_power_w"] == pytest.approx(1006.736, abs=0.001)
    assert result["mppt_pv3_current_a"] == 0.0
    assert result["mppt_pv3_power_w"] == 0.0
    assert result["pcs_ac_power_w"] == pytest.approx(1542.664, abs=0.001)
    # home_w takes block 87 (1470.0) over block 7 (1440.0) per the stated
    # precedence ("87.1, then 7.1") - both blocks are present in this frame.
    assert result["home_w"] == 1470.0


def test_incremental_frame_0_carries_only_block7() -> None:
    """The smallest capture (12 bytes) carries only 7.1 and 7.4: no solar_w,
    no soc_pct, and batt_w is the raw 7.4 value as-is (7.4 is already
    charge-positive on the wire - see module docstring)."""
    result = _parse(0)
    assert result is not None
    assert result["home_w"] == 220.0
    assert result["batt_w"] == 1030.0
    assert "solar_w" not in result
    assert "soc_pct" not in result
    assert "bp_remain_watth" not in result
    assert "pcs_ac_power_w" not in result


def test_soc_and_remaining_energy_extremes() -> None:
    """Fixture indices 22 and 21 in the field-pinning document's own
    numbering are captures 54 and 50 - addressed here by capture_index,
    since that is what the fixture and this file's ``_parse`` key on."""
    assert _parse(54)["bp_remain_watth"] == 5659  # type: ignore[index]
    assert _parse(50)["soc_pct"] == 56  # type: ignore[index]


# --- battery module report (254/46, field 5) ----------------------------


def test_module_bundle_dedups_heartbeat_backlog_by_highest_timestamp() -> None:
    """Capture 1: 14 headers, 13 field-5 records, two modules. The result
    must carry exactly pack1_* and pack2_* - never pack3_* - and the values
    must be those of the newest (highest field-37 timestamp) record per
    module, decoded independently here rather than copied from the parser.
    """
    payload = _payload(1)
    headers = []
    for span in _split_header_spans(payload):
        header = _decode_single_header(span)
        if (header.get("cmd_func"), header.get("cmd_id")) == (254, 46):
            headers.append(header)
    assert len(headers) == 14

    from ecoflow_energy.ecoflow.parsers.stream_proto import _decode_scalar, _iter_fields

    best: dict[int, tuple[int, float]] = {}
    record_count = 0
    for header in headers:
        pdata = bytes.fromhex(header["pdata"])
        for field_num, wire_type, raw in _iter_fields(pdata):
            if field_num != 5 or wire_type != 2:
                continue
            record_count += 1
            sub = _iter_fields(raw)
            index = None
            timestamp = None
            remain = None
            for sfn, swt, sraw in sub:
                if sfn == 15:
                    index = int(_decode_scalar(swt, sraw, "int"))
                elif sfn == 37:
                    timestamp = int(_decode_scalar(swt, sraw, "int"))
                elif sfn == 54:
                    remain = _decode_scalar(swt, sraw, "float")
            assert index is not None and timestamp is not None and remain is not None
            current = best.get(index)
            if current is None or timestamp >= current[0]:
                best[index] = (timestamp, remain)
    assert record_count == 13
    assert sorted(best) == [1, 2]

    result = parse_ocean2_message(payload)
    assert result is not None
    assert "pack1_remain_watth" in result
    assert "pack2_remain_watth" in result
    assert "pack3_remain_watth" not in result
    assert result["pack1_remain_watth"] == pytest.approx(best[1][1], abs=0.5)
    assert result["pack2_remain_watth"] == pytest.approx(best[2][1], abs=0.5)


def test_module_bundle_pack_soc_and_remaining_energy() -> None:
    result20 = _parse(20)
    assert result20 is not None
    assert result20["pack1_soc"] == pytest.approx(99.88, abs=0.01)
    assert result20["pack1_remain_watth"] == 5024.0
    assert result20["pack2_remain_watth"] == 5024.0

    result49 = _parse(49)
    assert result49 is not None
    assert result49["pack1_soc"] == pytest.approx(59.93, abs=0.01)
    assert result49["pack2_soc"] == pytest.approx(60.35, abs=0.01)
    assert result49["pack1_remain_watth"] == 2983.0


def test_module_power_matches_batt_w_charge_positive_convention_no_negation() -> None:
    """Capture 32 (module 1's own remain_watth falls across the bundle's six
    consecutive heartbeats -> discharging) has pack1_power_w negative;
    capture 65 (module 1's own remain_watth rises -> charging) has it
    positive. 5.1 needs no sign flip to already be charge-positive, unlike
    the system-level 65.20 field it otherwise resembles."""
    result32 = _parse(32)
    assert result32 is not None
    assert result32["pack1_power_w"] < 0

    result65 = _parse(65)
    assert result65 is not None
    assert result65["pack1_power_w"] > 0


def test_pack_totals_close_to_system_remaining_energy_at_the_days_peak() -> None:
    """Global maxima across the whole fixture: pack1 + pack2 remaining
    energy (both 5024 Wh, capture 20) is within 2 Wh of the system's own
    peak (10047 Wh, capture 28) - the same cross-check the field-pinning
    document made."""
    max_pack1 = 0.0
    max_pack2 = 0.0
    max_system = 0
    for frame in _frames():
        result = parse_ocean2_message(bytes.fromhex(frame["hex"]))
        if result is None:
            continue
        max_pack1 = max(max_pack1, result.get("pack1_remain_watth", 0.0))
        max_pack2 = max(max_pack2, result.get("pack2_remain_watth", 0.0))
        max_system = max(max_system, result.get("bp_remain_watth", 0))
    assert max_pack1 == 5024.0
    assert max_pack2 == 5024.0
    assert max_system == 10047
    assert abs((max_pack1 + max_pack2) - max_system) <= 2


def test_system_and_snapshot_battery_blocks_agree_in_magnitude() -> None:
    """Over every full-upload frame carrying both 65.20 and 87.4, the two
    raw readings must be opposite in sign, and their sum must stay small
    relative to their own magnitude - the two blocks are independent
    snapshots taken moments apart, so the
    document's own "within 10 W" claim does not hold on every one of the 15
    frames actually carrying both (measured range 0-173 W here); a 200 W
    bound still catches a sign error, which would push the sum to roughly
    twice either reading's magnitude (thousands of W) rather than leave a
    residual of a few hundred."""
    from ecoflow_energy.ecoflow.parsers.stream_proto import _decode_scalar, _iter_fields

    checked = 0
    for frame in _frames():
        payload = bytes.fromhex(frame["hex"])
        from ecoflow_energy.ecoflow.proto.decoder import decode_header_message

        headers, _ = decode_header_message(payload)
        for header in headers:
            if (header.get("cmd_func"), header.get("cmd_id")) != (254, 39):
                continue
            pdata = bytes.fromhex(header["pdata"]) if header.get("pdata") else b""
            top = _iter_fields(pdata)
            block65 = next((r for fn, wt, r in top if fn == 65 and wt == 2), None)
            block87 = next((r for fn, wt, r in top if fn == 87 and wt == 2), None)
            if block65 is None or block87 is None:
                continue
            raw65 = next(
                (
                    _decode_scalar(wt, r, "float")
                    for fn, wt, r in _iter_fields(block65)
                    if fn == 20
                ),
                None,
            )
            raw87 = next(
                (
                    _decode_scalar(wt, r, "float")
                    for fn, wt, r in _iter_fields(block87)
                    if fn == 4
                ),
                None,
            )
            if raw65 is None or raw87 is None:
                continue
            checked += 1
            assert (raw65 > 0) != (raw87 > 0) or (raw65 == 0 and raw87 == 0)
            assert abs(raw65 + raw87) <= 200.0
    assert checked >= 8


def test_block87_home_precedence_over_block7_at_frame_35() -> None:
    """Capture 35: 87.1 - 7.1 = +800, the largest divergence in the capture
    - a frame the precedence rule must get right."""
    result = _parse(35)
    assert result is not None
    payload = _payload(35)
    from ecoflow_energy.ecoflow.parsers.stream_proto import _decode_scalar, _iter_fields
    from ecoflow_energy.ecoflow.proto.decoder import decode_header_message

    headers, _ = decode_header_message(payload)
    display_cmd = (254, 39)
    header = next(
        h for h in headers if (h.get("cmd_func"), h.get("cmd_id")) == display_cmd
    )
    top = _iter_fields(bytes.fromhex(header["pdata"]))
    block7 = next(r for fn, wt, r in top if fn == 7 and wt == 2)
    block87 = next(r for fn, wt, r in top if fn == 87 and wt == 2)
    raw7 = next(
        _decode_scalar(wt, r, "float") for fn, wt, r in _iter_fields(block7) if fn == 1
    )
    raw87 = next(
        _decode_scalar(wt, r, "float") for fn, wt, r in _iter_fields(block87) if fn == 1
    )
    assert raw87 - raw7 == pytest.approx(800.0)
    assert result["home_w"] == raw87


# --- unhandled cmd_ids ---------------------------------------------------


def test_control_254_40_returns_none() -> None:
    assert _parse(3) is None


def test_control_254_42_returns_none() -> None:
    assert _parse(5) is None


def test_bundle_without_display_or_module_headers_returns_none() -> None:
    """Strip the 254/39 and 254/46 headers out of a get_reply bundle
    (capture 40, fixture's own 254/44 control frame) and confirm the
    remaining 254/40 / 53/113 / 254/44 headers alone yield nothing."""
    payload = _payload(40)
    spans = _split_header_spans(payload)
    kept = []
    for span in spans:
        header = _decode_single_header(span)
        if (header.get("cmd_func"), header.get("cmd_id")) not in ((254, 39), (254, 46)):
            kept.append(span)
    assert len(kept) == 3
    synthetic = _wrap_as_headers(kept)
    assert parse_ocean2_message(synthetic) is None
