"""Tests for the EcoFlow WAVE 3 (AC71) protobuf parser.

Every expectation below is a number the captured unit put on the wire on
2026-07-27 (PLAN-047, issue #161). They are written out per frame rather
than recomputed from the parser, because a test that derives its
expectation from the code under test holds for any field map.

Two tests rebuild a captured frame with one field changed or removed, to
exercise a path no captured frame reaches on its own (an unmapped mode
value, a masked frame whose sequence number is missing). The rebuild always
starts from a real header's own fields - seq, enc_type, cmd_func, cmd_id,
pdata - so the untouched fields stay exactly what the device sent; only the
one field under test is edited.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from ecoflow_energy.ecoflow.parsers.stream_proto import _iter_fields, _pdata_candidates
from ecoflow_energy.ecoflow.parsers.wave3_proto import (
    _ERRCODE_LIST_FIELD,
    _MODE_INFO_ALL_KEYS,
    _MODE_INFO_FIELD,
    WAVE3_ACTIVE_MODE_INPUTS,
    parse_wave3_message,
    resolve_active_mode,
)
from ecoflow_energy.ecoflow.proto.decoder import decode_header_message
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)

FIXTURE = Path(__file__).parent / "fixtures" / "wave3" / "ac71_frames_plan046.json"

# A different device's parser fixture, reused by one test only to prove the
# WAVE 3 map does not accidentally match a Smart Meter frame's field numbers.
_METER_FIXTURE = Path(__file__).parent / "fixtures" / "smart_meter" / "bk21_frames_issue331.json"

_MASKED_TAGS = ("standby_full", "runtime_full")

# test_a_masked_frame_without_its_seq_yields_nothing (below) covers only
# _MASKED_TAGS - 10 frames. test_a_masked_frame_without_its_flag_yields_nothing
# covers every masked full-upload tag, including running_full, 11 frames.
_ALL_MASKED_FULL_TAGS = _MASKED_TAGS + ("running_full",)


def _frames() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text())["frames"]


def _payload(index: int) -> bytes:
    return bytes.fromhex(_frames()[index]["hex"])


def _first_header(payload: bytes) -> dict[str, Any]:
    headers, _ = decode_header_message(payload)
    return headers[0]


def _pdata_leaks(frames: list[dict[str, Any]]) -> bool:
    """True if any frame's first de-masked candidate carries a string that
    must never reach the public corpus: the reporter's timezone, or the
    device's own model name.
    """
    for frame in frames:
        payload = bytes.fromhex(frame["hex"])
        headers, _ = decode_header_message(payload)
        for header in headers:
            candidates = _pdata_candidates(header)
            if not candidates:
                continue
            first = candidates[0]
            if b"Europe/" in first or b"AC71" in first:
                return True
    return False


def _encode_fields(fields: list[tuple[int, int, bytes]]) -> bytes:
    """Re-emit `_iter_fields` tuples back to protobuf wire bytes.

    Wire types 0, 1 and 5 raw bytes are already the exact on-wire bytes
    (`_iter_fields` slices them without decoding); only wire type 2 needs
    its length prefix put back.
    """
    out = bytearray()
    for field_num, wire_type, raw in fields:
        out += encode_varint((field_num << 3) | wire_type)
        if wire_type == 2:
            out += encode_varint(len(raw)) + raw
        else:
            out += raw
    return bytes(out)


def _xor_mask(pdata: bytes, seq: int) -> bytes:
    key = seq & 0xFF
    return bytes(b ^ key for b in pdata)


def _rebuild_frame(
    header: dict[str, Any],
    *,
    pdata_override: bytes | None = None,
    drop_seq: bool = False,
) -> bytes:
    """Rebuild a one-header frame from a decoded header, re-emitting only
    the fields `parse_wave3_message` and `_pdata_candidates` read: pdata
    (1), enc_type (6), cmd_func (8), cmd_id (9), seq (14).
    """
    pdata_hex = header.get("pdata", "")
    pdata_bytes = bytes.fromhex(pdata_hex) if pdata_override is None else pdata_override

    hdr = bytearray()
    hdr += encode_field_bytes(1, pdata_bytes)
    enc_type = header.get("enc_type")
    if enc_type is not None:
        hdr += encode_field_varint(6, enc_type)
    hdr += encode_field_varint(8, header.get("cmd_func", 0))
    hdr += encode_field_varint(9, header.get("cmd_id", 0))
    if not drop_seq and header.get("seq") is not None:
        hdr += encode_field_varint(14, header["seq"])
    return encode_field_bytes(1, bytes(hdr))


def _rebuild_with_varint_field(index: int, field_num: int, new_value: int) -> bytes:
    """Rebuild frame `index`, replacing one masked varint field's value.

    De-masks the captured pdata with its own sequence number, rewrites the
    one field, re-masks with the same key (XOR is its own inverse), and
    rebuilds the frame around it. Every other field is untouched.
    """
    header = _first_header(_payload(index))
    seq = header["seq"]
    masked = bytes.fromhex(header["pdata"])
    unmasked = _xor_mask(masked, seq)

    new_fields = []
    for fn, wt, raw in _iter_fields(unmasked):
        if fn == field_num and wt == 0:
            new_fields.append((fn, 0, encode_varint(new_value)))
        else:
            new_fields.append((fn, wt, raw))
    remasked = _xor_mask(_encode_fields(new_fields), seq)
    return _rebuild_frame(header, pdata_override=remasked)


def _rebuild_mode_info(index: int, transform: Any) -> bytes:
    """Rebuild frame `index`'s masked pdata with the wave_mode_info (514)
    record's own entry list edited by `transform`, everything else
    byte-identical.

    `transform` receives the `_iter_fields` tuples that make up the 514
    record's raw entries and returns the new list to re-encode - the same
    de-mask/edit/re-mask shape `_rebuild_with_varint_field` uses one level
    up, applied one level deeper.
    """
    header = _first_header(_payload(index))
    seq = header["seq"]
    masked = bytes.fromhex(header["pdata"])
    unmasked = _xor_mask(masked, seq)

    new_top_fields = []
    found = False
    for fn, wt, raw in _iter_fields(unmasked):
        if fn == _MODE_INFO_FIELD and wt == 2:
            found = True
            new_entries = transform(list(_iter_fields(raw)))
            new_top_fields.append((fn, wt, _encode_fields(new_entries)))
        else:
            new_top_fields.append((fn, wt, raw))
    assert found, "frame does not carry a wave_mode_info (514) record"
    remasked = _xor_mask(_encode_fields(new_top_fields), seq)
    return _rebuild_frame(header, pdata_override=remasked)


def test_the_fixture_carries_the_capture_it_claims_to() -> None:
    """Regression pin on the capture's shape - the exact tag counts and
    frame count PLAN-047 measured - plus the check that a corpus meant for
    a public repo carries neither the reporter's timezone string nor the
    device's own model name in any de-masked payload.

    The second half is a positive control on that check itself: an
    in-memory copy of the fixture with the model name injected into one
    frame must trip it, proving the check does not just pass because it
    never looks anywhere the string could be.
    """
    frames = _frames()
    assert len(frames) == 25

    expected_tags = {
        "standby_full": 7,
        "runtime_full": 3,
        "dev_request": 1,
        "get_reply_bundle": 1,
        "incremental_sleep": 1,
        "running_full": 1,
        "incremental_power": 10,
        "incremental_both": 1,
    }
    counts: dict[str, int] = {}
    for frame in frames:
        counts[frame["tag"]] = counts.get(frame["tag"], 0) + 1
    assert counts == expected_tags

    assert not _pdata_leaks(frames)

    tampered = copy.deepcopy(frames)
    marker_pdata = b"AC71 test-marker payload bytes"
    marker_header = (
        encode_field_bytes(1, marker_pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21)
    )
    tampered[0] = dict(tampered[0])
    tampered[0]["hex"] = encode_field_bytes(1, marker_header).hex()
    assert _pdata_leaks(tampered)

    # The same positive control for the other half of the check: a
    # de-masked payload carrying the reporter's own timezone string must
    # trip it too, not only the device model marker above.
    tampered_tz = copy.deepcopy(frames)
    tz_pdata = b"Europe/Lisbon test-marker payload bytes"
    tz_header = (
        encode_field_bytes(1, tz_pdata)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, 21)
    )
    tampered_tz[0] = dict(tampered_tz[0])
    tampered_tz[0]["hex"] = encode_field_bytes(1, tz_header).hex()
    assert _pdata_leaks(tampered_tz)


def test_a_standby_full_upload_decodes_the_criteria() -> None:
    """Frame 0, a full `254/21` upload while idle: scalars, the enum
    fields, the two booleans and the five wave_mode_info entries all come
    off the wire in one decode, and no internal `_raw` key leaks out.
    """
    result = parse_wave3_message(_payload(0))
    assert result is not None

    assert result["temp_ambient_c"] == pytest.approx(21.61, abs=0.01)
    assert result["humi_ambient_pct"] == pytest.approx(63.01, abs=0.01)
    assert result["operating_mode"] == "fan"

    # All 13 wave_mode_info keys, every mode's own entry (PLAN-047 review,
    # finding A9 - most of these carried no literal assertion before).
    assert result["cooling_submode"] == "normal"
    assert result["cooling_fan_speed_pct"] == 40
    assert result["cooling_target_temp_c"] == pytest.approx(26.0, abs=0.01)
    assert result["heating_submode"] == "sleep"
    assert result["heating_fan_speed_pct"] == 20
    assert result["heating_target_temp_c"] == pytest.approx(23.0, abs=0.01)
    assert result["fan_only_fan_speed_pct"] == 40
    assert result["dehumidify_fan_speed_pct"] == 100
    assert result["dehumidify_target_humidity_pct"] == pytest.approx(50.0, abs=0.01)
    assert result["constant_temp_fan_speed_pct"] == 40
    assert result["constant_temp_target_temp_c"] == pytest.approx(19.55, abs=0.01)
    assert result["constant_temp_upper_limit_c"] == pytest.approx(21.55, abs=0.01)
    assert result["constant_temp_lower_limit_c"] == pytest.approx(17.55, abs=0.01)

    assert result["running"] is False
    assert result["ac_input_connected"] is True
    assert result["fault"] is False
    assert result["screen_brightness_pct"] == 100
    assert result["beep_enabled"] is True
    assert result["automatic_drainage"] is False
    assert result["mood_light_mode"] == "on"

    assert not any(key.startswith("_") for key in result)


def test_the_active_mode_resolves_from_accumulated_state() -> None:
    """`resolve_active_mode` reads the per-mode keys out of accumulated
    state, not out of a single message - the device sends all five modes'
    settings in every full upload, and only `operating_mode` says which one
    is live.
    """
    state = parse_wave3_message(_payload(0))
    assert state is not None
    assert state["operating_mode"] == "fan"

    resolved_fan = resolve_active_mode(state)
    assert resolved_fan == {
        "target_temp_c": None,
        "airflow_speed_pct": 40,
        "target_humidity_pct": None,
        "operating_submode": None,
    }

    cooling_state = dict(state)
    cooling_state["operating_mode"] = "cooling"
    resolved_cooling = resolve_active_mode(cooling_state)
    assert resolved_cooling == {
        "target_temp_c": pytest.approx(26.0, abs=0.01),
        "airflow_speed_pct": 40,
        "target_humidity_pct": None,
        "operating_submode": "normal",
    }

    no_mode_state = {k: v for k, v in state.items() if k != "operating_mode"}
    assert resolve_active_mode(no_mode_state) == {}


def test_resolve_active_mode_never_reads_outside_its_declared_inputs() -> None:
    """`WAVE3_ACTIVE_MODE_INPUTS` is exported so the HA layer can stop
    hand-copying the set of keys `resolve_active_mode` reads (PLAN-047
    review, finding item 5). Proved here by observation rather than by
    re-deriving the set from the same table it is built from: every one
    of the 13 wave_mode_info keys gets its own sentinel object, and every
    non-`None` value `resolve_active_mode` hands back for any mode is
    traced back to the sentinel's key and checked against the set - a
    value it never returns can never be traced, so nothing outside the
    set could pass unnoticed.
    """
    sentinels = {key: object() for key in _MODE_INFO_ALL_KEYS}
    by_id = {id(value): key for key, value in sentinels.items()}
    assert "operating_mode" in WAVE3_ACTIVE_MODE_INPUTS

    for mode in ("cooling", "heating", "fan", "dehumidify", "constant_temp"):
        state = dict(sentinels)
        state["operating_mode"] = mode
        resolved = resolve_active_mode(state)
        assert resolved
        for value in resolved.values():
            if value is None:
                continue
            source_key = by_id.get(id(value))
            assert source_key is not None, "value not traceable to a known state key"
            assert source_key in WAVE3_ACTIVE_MODE_INPUTS


def test_mode_zero_or_unmapped_reads_unknown() -> None:
    """A `486` value of 0 or 9 (unmapped) must resolve to `operating_mode:
    None`, not a synthetic label and not a missing key - the frame is
    rebuilt from the real masked capture with only that one field changed.
    """
    for new_mode in (0, 9):
        frame = _rebuild_with_varint_field(0, 486, new_mode)
        result = parse_wave3_message(frame)
        assert result is not None
        assert "operating_mode" in result
        assert result["operating_mode"] is None


def test_a_stray_field_before_the_mode_list_does_not_shift_the_modes() -> None:
    """A `514` entry's mode comes from its position among *accepted*
    entries (field 1, wire type 2), not its ordinal among every field the
    record holds: a stray field 2 varint prepended ahead of the six real
    entries must not shift cooling's setpoint onto heating's key
    (PLAN-047 review, finding A2).
    """

    def _prepend_stray(entries: list[tuple[int, int, bytes]]) -> list[tuple[int, int, bytes]]:
        return [(2, 0, encode_varint(1))] + entries

    frame = _rebuild_mode_info(0, _prepend_stray)
    result = parse_wave3_message(frame)
    assert result is not None
    assert result["cooling_target_temp_c"] == pytest.approx(26.0, abs=0.01)
    assert result["heating_target_temp_c"] == pytest.approx(23.0, abs=0.01)


def test_a_mode_list_with_the_wrong_entry_count_publishes_no_mode_data() -> None:
    """A `514` record with anything other than the six entries this map was
    built for (PLAN-047 review, finding A3) must not guess at a mapping -
    all thirteen per-mode keys come back `None`, and every other key of
    the frame is untouched.
    """
    baseline = parse_wave3_message(_payload(0))
    assert baseline is not None

    def _drop_entry_zero(entries: list[tuple[int, int, bytes]]) -> list[tuple[int, int, bytes]]:
        return entries[1:]

    frame = _rebuild_mode_info(0, _drop_entry_zero)
    result = parse_wave3_message(frame)
    assert result is not None

    for key in _MODE_INFO_ALL_KEYS:
        assert key in result
        assert result[key] is None

    for key in set(baseline) - set(_MODE_INFO_ALL_KEYS):
        assert result[key] == baseline[key]


def test_the_runtime_upload_decodes_temperatures_and_firmware() -> None:
    """Frame 1, a full `254/22` upload: the sensor temperatures the `21`
    message does not carry, plus the packed firmware revision.
    """
    result = parse_wave3_message(_payload(1))
    assert result is not None

    assert result["temp_outdoor_ambient_c"] == pytest.approx(20.90, abs=0.01)
    assert result["ac_input_voltage_v"] == pytest.approx(238.0, abs=0.01)
    assert result["bms_communication_error"] is True
    assert result["firmware_version"] == "v1.1.0.104"
    assert result["temp_compressor_discharge_c"] == pytest.approx(22.06, abs=0.01)

    assert not any(key.startswith("_") for key in result)


def test_a_zero_firmware_publishes_no_version() -> None:
    """`176` (packed firmware revision) publishes nothing when zero: a `22`
    upload that has not yet reported firmware must not overwrite a
    previously known version with an empty string (see module docstring).
    A second field (174, always-mapped) keeps the frame non-empty, since a
    zero-only firmware field decodes to nothing at all and the parser
    would otherwise return `None` for the whole frame rather than a dict
    missing the key. Built as a synthetic unmasked `254/22` frame, since no
    captured frame ever carried a zero firmware value to exercise this
    with.
    """
    pdata = encode_field_varint(176, 0) + encode_field_varint(174, 0)
    frame = _rebuild_frame({"cmd_func": 254, "cmd_id": 22}, pdata_override=pdata)
    result = parse_wave3_message(frame)
    assert result is not None
    assert "firmware_version" not in result
    assert result["bms_communication_error"] is False


def test_an_incremental_power_frame_carries_only_power() -> None:
    """Frame 14, a two-field incremental upload (power plus its unmapped
    duplicate 777): the result carries exactly the one mapped key, nothing
    inferred from a field's absence.
    """
    result = parse_wave3_message(_payload(14))
    # 14.9527 on the wire, published at two decimals like every float here.
    assert result == {"ac_input_power_w": 14.95}


def test_an_incremental_sleep_frame_flips_running() -> None:
    """Frame 12 carries only field 212 (dev_sleep_state) = 0: the result is
    exactly `{"running": True}`, the inversion of the sleep flag and
    nothing else.
    """
    result = parse_wave3_message(_payload(12))
    assert result == {"running": True}


def test_a_masked_frame_without_its_seq_yields_nothing() -> None:
    """Regression pin on measured frames, not a proof: every captured
    `standby_full`/`runtime_full` frame, rebuilt with its sequence number
    removed, must decode to nothing. `_pdata_candidates` falls back to the
    still-masked bytes when it cannot compute the XOR key, and those bytes
    do not happen to parse into anything this map recognizes.
    """
    frames = _frames()
    checked = 0
    for frame in frames:
        if frame["tag"] not in _MASKED_TAGS:
            continue
        header = _first_header(bytes.fromhex(frame["hex"]))
        rebuilt = _rebuild_frame(header, drop_seq=True)
        assert parse_wave3_message(rebuilt) is None
        checked += 1
    assert checked == 10


def test_a_get_reply_bundle_decodes_plain_and_agrees_with_the_push() -> None:
    """Frame 11, an unmasked `get_reply_bundle` bundling three headers
    (`23`/`22`/`21`) in one message with no `enc_type` at all: the merged
    result comes from the `22` and `21` headers only (`23` DevRequest is
    unmapped), and its ambient temperature agrees with the same sensor
    pushed 62 minutes earlier as a masked `standby_full` upload (frame 0) -
    two different message shapes, plain and masked, reporting one reading.
    """
    result = parse_wave3_message(_payload(11))
    assert result is not None

    assert result["temp_ambient_c"] == pytest.approx(21.64, abs=0.01)
    assert result["operating_mode"] == "fan"
    assert result["running"] is False
    assert result["firmware_version"] == "v1.1.0.104"

    push = parse_wave3_message(_payload(0))
    assert push is not None
    assert abs(result["temp_ambient_c"] - push["temp_ambient_c"]) < 0.05


def test_a_masked_frame_without_its_flag_yields_nothing() -> None:
    """Regression pin on measured frames, not a proof: every captured
    standby_full/runtime_full/running_full frame, rebuilt with its
    `enc_type` header field removed, must decode to nothing. Without the
    flag, `_pdata_candidates` treats the still-masked bytes as already
    plain and hands them straight to the decoder, which does not happen to
    recognize anything in them - this is the wider counterpart of
    `test_a_masked_frame_without_its_seq_yields_nothing` above, which only
    walks `_MASKED_TAGS` and so never reaches `running_full`.
    """
    checked = 0
    for frame in _frames():
        if frame["tag"] not in _ALL_MASKED_FULL_TAGS:
            continue
        header = _first_header(bytes.fromhex(frame["hex"]))
        header_no_flag = dict(header)
        header_no_flag["enc_type"] = None
        rebuilt = _rebuild_frame(header_no_flag)
        assert parse_wave3_message(rebuilt) is None
        checked += 1
    assert checked == 11


def test_a_dev_request_is_not_a_reading() -> None:
    """Frame 2, `254/23` DevRequest: no field in this message is mapped, so
    the frame carries no reading at all - not an empty dict, `None`.
    """
    assert parse_wave3_message(_payload(2)) is None


def test_a_meter_frame_is_not_a_wave3_frame() -> None:
    """A Smart Meter `254/21` frame carries none of the WAVE 3 field
    numbers (515, 962, 963, 772 belong to that device's own map, not this
    one): the WAVE 3 parser must not mistake another device's reading for
    its own.
    """
    meter_frames = json.loads(_METER_FIXTURE.read_text())["frames"]
    payload = bytes.fromhex(meter_frames[0]["hex"])
    assert parse_wave3_message(payload) is None


def test_a_fault_code_sets_the_fault_flag() -> None:
    """`dev_errcode_list` (field 627) collapses to a single `fault` flag:
    true when any code in its packed repeated list is non-zero, false when
    every code is zero. Built as a synthetic unmasked `254/21` frame, since
    no captured frame ever carried a non-zero code to exercise this with.
    """

    def _frame_with_codes(codes: list[int]) -> bytes:
        packed = b"".join(encode_varint(code) for code in codes)
        record = encode_field_bytes(1, packed)
        pdata = encode_field_bytes(_ERRCODE_LIST_FIELD, record)
        return _rebuild_frame({"cmd_func": 254, "cmd_id": 21}, pdata_override=pdata)

    faulted = parse_wave3_message(_frame_with_codes([0, 0, 7, 0, 0, 0]))
    assert faulted is not None
    assert faulted["fault"] is True

    clean = parse_wave3_message(_frame_with_codes([0, 0, 0, 0, 0, 0]))
    assert clean is not None
    assert clean["fault"] is False


def test_an_unpacked_error_list_is_read_too() -> None:
    """`_decode_errcode_list` never switches on wire type, so the unpacked
    repeated encoding - one varint field 1 per element, wire type 0 -
    works today by accident (PLAN-047 review, finding A7). Pin it: a
    non-zero code among unpacked entries still sets `fault`.
    """
    codes = [0, 0, 7, 0]
    record = b"".join(encode_field_varint(1, code) for code in codes)
    pdata = encode_field_bytes(_ERRCODE_LIST_FIELD, record)
    frame = _rebuild_frame({"cmd_func": 254, "cmd_id": 21}, pdata_override=pdata)

    result = parse_wave3_message(frame)
    assert result is not None
    assert result["fault"] is True


_RANGE_TEMP_KEYS = (
    "temp_ambient_c",
    "temp_indoor_supply_air_c",
    "temp_indoor_return_air_c",
    "temp_outdoor_ambient_c",
    "temp_condenser_c",
    "temp_evaporator_c",
    "temp_compressor_discharge_c",
)
_RANGE_OPERATING_MODES = frozenset({"cooling", "heating", "fan", "dehumidify", "constant_temp"})


def test_every_full_frame_stays_inside_the_measured_ranges() -> None:
    """Sanity floor over every masked full-upload frame plus the plain
    get_reply bundle: temperatures, humidity and AC input power stay
    inside the range this 24-hour capture actually measured, and a present
    `operating_mode` is always one of the five named modes. The count of
    fields actually compared is asserted with a floor - a parser that
    quietly stops producing readings would still pass an "at least one
    frame decoded" check, but not a floor on how much was measured.
    """
    frames = _frames()
    masked_full_indices = [i for i, frame in enumerate(frames) if frame["tag"] in _ALL_MASKED_FULL_TAGS]
    assert len(masked_full_indices) == 11

    results = [parse_wave3_message(_payload(i)) for i in masked_full_indices]
    bundle = parse_wave3_message(_payload(11))
    assert bundle is not None
    results.append(bundle)

    checked = 0
    for result in results:
        assert result is not None
        for key in _RANGE_TEMP_KEYS:
            if key not in result:
                continue
            assert 15 <= result[key] <= 40
            checked += 1
        if "humi_ambient_pct" in result:
            assert 0 <= result["humi_ambient_pct"] <= 100
            checked += 1
        if "ac_input_power_w" in result:
            assert 0 <= result["ac_input_power_w"] <= 3000
            checked += 1
        if result.get("operating_mode") is not None:
            assert result["operating_mode"] in _RANGE_OPERATING_MODES
            checked += 1

    # Measured 65 over these 12 records (PLAN-047 review, finding A1). The
    # floor sits below that so a legitimate fixture edit does not trip it,
    # and above the 45 that emptying the whole (254, 22) field map alone
    # would leave - a floor low enough to survive losing a message type
    # entirely is not a floor.
    assert checked >= 60


def test_float_readings_carry_at_most_two_decimals() -> None:
    """The device sends single-precision floats, so a 19.55 setpoint arrives
    as 19.549999237060547. A sensor hides that behind its display precision;
    a number entity shows it verbatim (seen in the Docker window for the
    constant temperature setpoint). Every float the parser publishes is
    rounded to two decimals, which loses nothing the app can express.
    """
    result = parse_wave3_message(_payload(0))
    assert result is not None

    floats = {key: value for key, value in result.items() if isinstance(value, float)}
    assert len(floats) >= 10  # positive control: the full frame carries floats
    assert result["constant_temp_target_temp_c"] == 19.55
    assert result["temp_ambient_c"] == 21.61
    for key, value in floats.items():
        assert value == round(value, 2), key
