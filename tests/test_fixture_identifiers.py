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
    # A serial masked to `X` and then re-masked with the region's key reads
    # as a run of `X ^ key` on the wire, which for many keys is itself
    # alphanumeric (`0x36` gives `n`, `0x1e` gives `F`). The product
    # sanitizer's own output on the 2026-08-24 settings report tripped this
    # pass that way on 2026-09-11. Such a run is skipped here only when its
    # plaintext under the region's key is entirely the mask byte. A declared
    # key is not proof of a mask (a header can set the flag and send plain
    # bytes), so a run whose plaintext is anything else stays a finding, and
    # a region without a key is scanned on the wire like everything else.
    keyed_regions = [
        region for region in _encrypted_regions(raw) if region.key is not None
    ]
    for match in _RUN.finditer(text):
        run = match.group()
        if run in _PLACEHOLDERS:
            continue
        if any(
            region.start <= match.start()
            and match.end() <= region.end
            and set(_xor(raw[match.start() : match.end()], region.key)) == {ord("X")}
            for region in keyed_regions
        ):
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
        region_raw = _xor(raw[region.start : region.end], region.key)
        region_text = region_raw.decode("latin1")
        if _UUID.search(region_text):
            findings.append("unmasked UUID under the mask")
        if _MAC.search(region_text):
            findings.append("unmasked MAC under the mask")
        for run in _RUN.findall(region_text):
            if run in _PLACEHOLDERS:
                continue
            if set(run) != {"X"}:
                findings.append(f"unmasked run under the mask {run!r}")
        for start, end in _anchored_string_fields(region_raw):
            value = region_raw[start:end]
            if set(value) == {ord("X")}:
                continue
            decoded = value.decode("latin1")
            if decoded in _PLACEHOLDERS:
                continue
            findings.append(
                f"unmasked string beside a serial under the mask {decoded!r}"
            )

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
        + encode_field_bytes(2, b"A1B2C3D4")
        + encode_field_bytes(3, b"X" * 16)
        + encode_field_bytes(5, b"Ecoflow_1234")
    )
    frame = _wrap_as_record(record)

    findings = _leaks(frame)
    assert len(findings) == 2, findings
    assert any("A1B2C3D4" in finding for finding in findings), findings
    assert any("Ecoflow_1234" in finding for finding in findings), findings

    assert _leaks(sanitize_frame(frame, [])) == []


def test_the_gate_sees_a_string_beside_a_serial_under_the_mask() -> None:
    """Positive control for the region branch of the anchored check.

    The product masks a neighbour's name under the XOR mask by running the
    same passes over the unmasked region (ADR-023, ADR-025); the gate has
    to see it there too, or it would accept a fixture the product would
    have cleaned. Same record as above, carried as the XOR-ed `pdata` of a
    header that declares `enc_type = 1` and its own `seq`.
    """
    key = 0x99
    record = (
        encode_field_varint(1, 1)
        + encode_field_bytes(2, b"A1B2C3D4")
        + encode_field_bytes(3, b"X" * 16)
        + encode_field_bytes(5, b"Ecoflow_1234")
    )
    plain = encode_field_bytes(1, record)
    header = bytearray()
    header.extend(encode_field_varint(6, 1))
    header.extend(encode_field_varint(14, key))
    header.extend(encode_field_bytes(1, bytes(b ^ key for b in plain)))
    frame = encode_field_bytes(1, bytes(header))

    findings = [f for f in _leaks(frame) if "beside a serial under the mask" in f]

    assert len(findings) == 2, findings
    assert any("A1B2C3D4" in f for f in findings), findings
    assert any("Ecoflow_1234" in f for f in findings), findings
    assert _leaks(sanitize_frame(frame, [])) == []


def test_the_gate_is_quiet_without_a_serial() -> None:
    """Negative control: the same shapes without an anchor leak nothing.

    Without a whole field that fullmatches a serial, `_anchored_string_fields`
    yields no candidates at all, however identifier-shaped its neighbours are.
    """
    record = encode_field_bytes(2, b"plug_and_play") + encode_field_bytes(
        3, b"A1B2C3D4"
    )
    frame = _wrap_as_record(record)

    assert _leaks(frame) == []


_RUN_DATA_SYNC_FIXTURE = (
    FIXTURE_ROOT / "powerpulse" / "c376_run_data_sync_20260824.json"
)


def _run_data_sync_frame() -> bytes:
    """The first PowerPulse 2 settings report on file, masked with `X` under
    the XOR mask; on the wire the masked serial reads as sixteen `n`."""
    frames = json.loads(_RUN_DATA_SYNC_FIXTURE.read_text())["frames"]
    return bytes.fromhex(frames[0]["hex"])


def test_a_masked_serial_under_a_keyed_region_is_not_a_wire_leak() -> None:
    """Negative control: the product sanitizer's own output must pass.

    `X ^ 0x36` is `n`, so the wire bytes of this frame carry a sixteen-`n`
    run where the serial was. The region's plaintext is what gets checked,
    and there the run is `X` * 16.
    """
    raw = _run_data_sync_frame()
    assert b"n" * 16 in raw, "the control frame no longer shows the shape"
    region = next(r for r in _encrypted_regions(raw) if r.key is not None)
    assert b"X" * 16 in _xor(raw[region.start : region.end], region.key)
    assert _leaks(raw) == []


def test_a_plain_serial_in_a_region_that_only_declares_a_key_is_a_leak() -> None:
    """Positive control for the wire skip itself: a header that declares the
    mask but sends plain bytes carries the serial on the wire. Its plaintext
    under the key is not the mask byte, so the skip must not apply and the
    wire scan reports it. Without the skip the result is the same; with an
    unconditional skip it would be empty.
    """
    key = 0x1E
    serial = b"C376TESTPLAIN001"
    header = bytearray()
    header.extend(encode_field_varint(6, 1))  # enc_type = XOR, declared
    header.extend(encode_field_varint(14, key))  # seq
    header.extend(encode_field_bytes(1, serial))  # pdata NOT masked
    frame = encode_field_bytes(1, bytes(header))
    region = next(r for r in _encrypted_regions(frame) if r.key is not None)
    assert region.start <= frame.index(serial) < region.end
    assert any("unmasked run" in f for f in _leaks(frame)), _leaks(frame)


def test_a_real_serial_under_a_keyed_region_is_still_a_leak() -> None:
    """Positive control: skipping the wire scan inside a keyed region must
    not let an unmasked serial hide there. The same frame with a serial-shaped
    run written into the plaintext and re-masked is reported under the mask.
    """
    raw = bytearray(_run_data_sync_frame())
    region = next(r for r in _encrypted_regions(bytes(raw)) if r.key is not None)
    plain = bytearray(_xor(bytes(raw[region.start : region.end]), region.key))
    at = plain.index(b"X" * 16)
    plain[at : at + 16] = b"C376TEST00000001"
    raw[region.start : region.end] = _xor(bytes(plain), region.key)
    findings = _leaks(bytes(raw))
    assert any("under the mask" in finding for finding in findings), findings
