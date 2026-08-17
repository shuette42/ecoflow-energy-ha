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
        if "hex" in payload:
            return [payload]
    return []


def test_the_fixture_tree_is_not_empty() -> None:
    """A guard over an empty glob passes for the wrong reason.

    Without this, deleting or moving the fixture directory would turn every
    check below into a silent no-op that still reports green.
    """
    files = _fixture_files()
    assert len(files) >= 6, [str(p) for p in files]


@pytest.mark.parametrize("path", _fixture_files(), ids=lambda p: p.name)
def test_no_identifier_survived_masking(path: Path) -> None:
    """No serial, UUID, MAC or account id may reach a public fixture."""
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
        hex_payload = frame.get("hex")
        if not hex_payload:
            continue
        text = bytes.fromhex(hex_payload).decode("latin1")
        where = f"{path.name}[{index}]"
        assert not _UUID.search(text), f"{where}: unmasked UUID"
        assert not _MAC.search(text), f"{where}: unmasked MAC"
        for run in _RUN.findall(text):
            if run in _PLACEHOLDERS:
                continue
            assert set(run) == {"X"}, f"{where}: unmasked run {run!r}"

        # Only where the field is an actual MQTT topic. Several fixtures reuse
        # the same key for the message type ("property", "get_reply"), which
        # carries no identifier and must not be asked to look templated.
        topic = frame.get("topic") or ""
        if "/" in topic:
            assert "{sn}" in topic or "XXXX" in topic, f"{where}: raw topic {topic}"
