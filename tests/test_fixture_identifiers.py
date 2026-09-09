"""Every capture fixture in this repo, checked for surviving identifiers.

The fixtures are real device bytes and this repo is public, so masking is the
only thing between a contributor's meter UUID and the world. The guard used to
live next to each parser's tests, one copy per family, each with its own idea
of what an identifier looks like. That is how a lowercase dashed UUID reached a
public branch in 2026-08: the check it passed through looked for
``[A-Z0-9]{15,}``, which cannot match a string containing hyphens and lowercase
letters, and the fixture it guarded was not the one the UUID was in.

So there is one check, it walks the fixture tree rather than a list of paths,
and a fixture added tomorrow is covered without anyone remembering to add it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.frame_capture import (
    _anchored_string_fields,
    _encrypted_regions,
    _xor,
    sanitize_frame,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures"

# One pattern per shape, because a single regex is what got this wrong before.
_UUID = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")
# A serial, an account id or a certificate name, whatever its case. Masked
# bytes read as a run of X, which is what every fixture in the tree uses.
#
# Twelve characters, not six: a payload is protobuf, and six alphanumeric bytes
# in a row happen by chance in binary data (`Cstivc` in one capture is three
# floats meeting each other). An EcoFlow serial is 16, a certificate account
# longer, and the shortest identifier anyone has ever pasted into a fixture was
# a 32 character UUID, which the pattern above catches on its own.
_RUN = re.compile(r"[0-9A-Za-z]{12,}")

# Deliberate placeholders that are not identifiers and must not be masked
# further, since masking them would hide what the field is.
_PLACEHOLDERS = frozenset({"AABBCCDDEEFF"})


def _fixture_files() -> list[Path]:
    """Every fixture file, not only the JSON ones.

    `r374_get_all_masked.bin` is raw device bytes with no JSON around it, and a
    glob for `*.json` would have walked straight past it.
    """
    return sorted(p for p in FIXTURE_ROOT.rglob("*") if p.is_file())


def _frames(payload: object) -> list[dict]:
    """Return the frame dicts of a fixture, whatever shape it stores them in."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("frames", "raw_frames", "pushes"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        if "hex" in payload or "frame_hex" in payload:
            return [payload]
    return []


def test_the_fixture_tree_is_not_empty() -> None:
    """A guard over an empty glob passes for the wrong reason.

    Without this, deleting or moving the fixture directory would turn every
    check below into a silent no-op that still reports green.
    """
    files = _fixture_files()
    assert len(files) >= 6, [str(p) for p in files]


def _leaks(raw: bytes) -> list[str]:
    """Every identifier a masked frame should no longer carry, as messages.

    One entry per finding rather than the first failed assertion, so a single
    dirty frame reports everything wrong with it in one run. Covers the same
    ground the fixture gate used to check inline: a UUID or MAC anywhere in
    the frame text, an unmasked identifier run, the same three checks again on
    every region an `enc_type == 1` header XOR-hides (PLAN-128), and a
    neighbour's name or short id sitting beside its own serial
    (`_anchored_string_fields`, PLAN-133, ADR-025).
    """
    findings: list[str] = []
    text = raw.decode("latin1")
    if _UUID.search(text):
        findings.append("unmasked UUID")
    if _MAC.search(text):
        findings.append("unmasked MAC")
    for run in _RUN.findall(text):
        if run in _PLACEHOLDERS:
            continue
        if set(run) != {"X"}:
            findings.append(f"unmasked run {run!r}")

    for start, end in _anchored_string_fields(raw):
        value = raw[start:end]
        if set(value) == {ord("X")}:
            continue
        decoded = value.decode("latin1")
        if decoded in _PLACEHOLDERS:
            continue
        findings.append(f"unmasked string beside a serial {decoded!r}")

    # The mask under the mask: unmask every region the header itself declares
    # XOR-ed and run the same checks over that plaintext. A region with no
    # `seq` has no key to derive and is left alone here too - `sanitize_frame`
    # cannot mask what it cannot decrypt either.
    for region in _encrypted_regions(raw):
        if region.key is None:
            continue
        region_text = _xor(raw[region.start : region.end], region.key).decode("latin1")
        if _UUID.search(region_text):
            findings.append("unmasked UUID under the mask")
        if _MAC.search(region_text):
            findings.append("unmasked MAC under the mask")
        for run in _RUN.findall(region_text):
            if run in _PLACEHOLDERS:
                continue
            if set(run) != {"X"}:
                findings.append(f"unmasked run under the mask {run!r}")

    return findings


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_no_identifier_survived_masking(path: Path) -> None:
    """No serial, UUID, MAC, account id, or neighbour string may reach a public fixture.

    Measured on this corpus: 164 hex blobs across the fixture tree, 67
    `enc_type == 1` headers, 0 leaks from any of the checks `_leaks` runs.
    """
    text_of_file = path.read_bytes().decode("latin1")
    try:
        frames = _frames(json.loads(text_of_file))
    except ValueError:
        # A raw byte fixture. It gets the text scan below plus the run check on
        # its own bytes, which is what the frame loop would have done anyway.
        frames = [{"hex": path.read_bytes().hex()}]

    # Every fixture is scanned as text first, whatever shape it has. A fixture
    # this file cannot read as frames used to be skipped, and a skipped guard
    # reports green for the same reason a passing one does. The two patterns
    # below are safe on a file full of hex blobs: both need separators, which
    # hex strings do not contain.
    assert not _UUID.search(text_of_file), f"{path.name}: unmasked UUID"
    assert not _MAC.search(text_of_file), f"{path.name}: unmasked MAC"

    for index, frame in enumerate(frames):
        hex_payload = frame.get("hex") or frame.get("frame_hex")
        if not hex_payload:
            continue
        raw_frame = bytes.fromhex(hex_payload)
        where = f"{path.name}[{index}]"
        findings = _leaks(raw_frame)
        assert findings == [], f"{where}: {findings}"

        # Only where the field is an actual MQTT topic. Several fixtures reuse
        # the same key for the message type ("property", "get_reply"), which
        # carries no identifier and must not be asked to look templated.
        topic = frame.get("topic") or ""
        if "/" in topic:
            assert "{sn}" in topic or "XXXX" in topic, f"{where}: raw topic {topic}"


def test_the_encrypted_region_walk_still_finds_its_headers() -> None:
    """A floor on what the region check inspects, not only on what it flags.

    `test_no_identifier_survived_masking` only fails when a region it looked
    at was dirty. A `_encrypted_regions` that silently started returning `[]`
    for every frame would make every one of those assertions vacuously true,
    and the corpus would read as clean for the wrong reason - a counter that
    counts the loop instead of the comparison, which this repo has been
    burned by before.

    Measured on this corpus: 164 hex blobs across the fixture tree, 67 of
    which carry at least one `enc_type == 1` header. Both are floors rather
    than exact counts, so a new fixture may raise them without breaking this
    test.
    """
    blobs = regions = 0
    for path in _fixture_files():
        try:
            frames = _frames(
                json.loads(path.read_text(encoding="utf-8", errors="replace"))
            )
        except ValueError:
            frames = [{"hex": path.read_bytes().hex()}]
        for frame in frames:
            hex_payload = frame.get("hex") or frame.get("frame_hex")
            if not hex_payload:
                continue
            blobs += 1
            regions += len(_encrypted_regions(bytes.fromhex(hex_payload)))
    assert blobs >= 164, blobs
    assert regions >= 67, regions


def test_the_region_check_has_a_positive_control() -> None:
    """Confirm the pattern applied under the mask can actually fail.

    Every fixture on file reports clean because `sanitize_frame` already
    cleaned it - that alone does not prove the region-aware assertions in
    `test_no_identifier_survived_masking` have any way to fail. This is that
    check's positive control: it builds one XOR-masked
    header directly, the way a device would send it, without running it
    through `sanitize_frame` first, and confirms `_encrypted_regions` finds
    the region and `_UUID` finds the identifier once it is decrypted with
    the header's own key - the same two steps the fixture gate performs.
    """
    key = 0x42
    uuid_plain = b"a1b2c3d4-0000-4000-8000-abcdefabcdef"
    header = bytearray()
    header.extend(encode_field_varint(6, 1))  # enc_type = XOR
    header.extend(encode_field_varint(14, key))  # seq
    header.extend(encode_field_bytes(1, bytes(b ^ key for b in uuid_plain)))  # pdata
    frame = encode_field_bytes(1, bytes(header))

    regions = _encrypted_regions(frame)
    assert len(regions) == 1
    region = regions[0]
    plain = _xor(frame[region.start : region.end], region.key)

    assert _UUID.search(plain.decode("latin1"))


def _wrap_as_record(record: bytes) -> bytes:
    """Nest a record three levels deep, the way a real frame carries it.

    `frame -> header -> pdata -> record list` (PLAN-133, ADR-025): a payload
    handed straight to `_leaks` is depth 0, and the record only becomes a
    message of its own once the walk has descended three delimited fields.
    """
    return encode_field_bytes(1, encode_field_bytes(1, encode_field_bytes(1, record)))


def test_the_gate_sees_a_string_beside_a_serial() -> None:
    """Positive control: a name and a short id beside an anchor both leak.

    The anchor here is an already-masked serial (`X` * 16) rather than a real
    one, on purpose: that is what a real frame looks like after the serial
    pass has already run and before the anchored-string pass runs after it,
    which is the exact order `_plain_passes` uses.
    """
    record = (
        encode_field_varint(1, 1)
        + encode_field_bytes(2, b"3D1A32AC")
        + encode_field_bytes(3, b"X" * 16)
        + encode_field_bytes(5, b"Ecoflow_0379")
    )
    frame = _wrap_as_record(record)

    findings = _leaks(frame)
    assert len(findings) == 2, findings
    assert any("3D1A32AC" in finding for finding in findings), findings
    assert any("Ecoflow_0379" in finding for finding in findings), findings

    assert _leaks(sanitize_frame(frame, [])) == []


def test_the_gate_is_quiet_without_a_serial() -> None:
    """Negative control: the same shapes without an anchor leak nothing.

    Without a whole field that fullmatches a serial, `_anchored_string_fields`
    yields no candidates at all, however identifier-shaped its neighbours are.
    """
    record = encode_field_bytes(2, b"plug_and_play") + encode_field_bytes(
        3, b"3D1A32AC"
    )
    frame = _wrap_as_record(record)

    assert _leaks(frame) == []
