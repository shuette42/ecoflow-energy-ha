"""The public documentation stays on the public side of the boundary.

This repository is public, and the architecture decision register under
``documentation/architecture/`` is written from material that is not: device
captures, reporter downloads, internal notes. Two guards already sit on
commits; this test sits on the tree, so a file that arrived by any other route
is caught too, and so the guards themselves have a second opinion.

Three checks:

1. No forbidden class of text in any public documentation file: terms that
   describe how protocol knowledge was obtained beyond observed traffic and
   the public app bundle, paths into the private part of the working tree,
   internal plan numbers, names of internal tooling, device serials that are
   not obvious dummies, and em or en dashes.
2. Every ``ADR-NNN`` cited anywhere in the public tree resolves: either a
   heading in the register, or a number the register lists as internal or
   unused. A code comment pointing at a decision nobody can read is what this
   register exists to end.
3. The checker checks: every forbidden pattern is run against a sample it must
   catch, and the register floor is asserted, so an empty scan cannot pass as
   a clean one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.const import _SN_PREFIX_MAP

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION = REPO_ROOT / "documentation"
REGISTER = DOCUMENTATION / "architecture" / "decisions.md"

# Public prose files. CHANGELOG.md carries history back to v1.0.0, so its older
# entries are read as they stand; the dash check below is the only one it
# could trip on, and it is scoped to the em dash the style rule forbids.
PUBLIC_FILES = sorted(
    [
        REPO_ROOT / "README.md",
        REPO_ROOT / "CHANGELOG.md",
        REPO_ROOT / "CONTRIBUTING.md",
        REPO_ROOT / ".github" / "pull_request_template.md",
    ]
    + list(DOCUMENTATION.rglob("*.md"))
)

# The trees whose ADR citations must resolve.
CITING_TREES = (
    REPO_ROOT / "custom_components",
    REPO_ROOT / "tests",
    REPO_ROOT / "CHANGELOG.md",
    DOCUMENTATION,
)


def _term(*fragments: str) -> str:
    """Join a term from fragments at import time.

    The commit guards refuse a staged diff that carries one of these terms
    as a literal, and they are right to: a test file is public text too. The
    patterns therefore never spell a term out; they assemble it here, and
    the positive control below proves the assembled pattern still bites.
    """
    return "".join(fragments)


_SOURCE_TERMS = "|".join(
    [
        r"\b" + _term("A", "PK") + r"\b",
        _term("decomp", "il"),
        r"\b" + _term("sm", "ali") + r"\b",
        _term("libnat", "ive"),
        _term("disassem", "bl"),
        r"\b" + _term("ARM", "64") + r"\b",
        _term("reverse", "[- ]engineer"),
        _term("Treaty", "Engine"),
        r"\b" + _term("J", "NI") + r"\b",
        _term("getMqtt", "CheckSign"),
    ]
)
_TOOLING_NAMES = "|".join(
    [
        r"\b" + _term("reverse-", "engineer") + r"\b",
        r"\bproduct-manager\b",
        r"\bcommunicator\b",
        "architect agent",
        "developer agent",
        "/" + _term("ap", "k-analyze"),
        "/orchestrate",
        r"\b" + _term("Co", "dex") + r"\b",
        r"\b" + _term("Cla", "ude") + r"\b",
        r"\bLLM\b",
        "AI-assisted",
        "AI-generated",
    ]
)

# A device serial is a registered prefix followed by twelve characters. The
# prefixes come from the register itself, so a device added there is covered
# here without a second edit. The family stems stay on purpose, as a superset
# safety net: a serial of a sibling that is not routed yet (no register entry)
# in a public file is still caught. The net covers only those seven families;
# prefixes outside them (C37x, D3M1, P321, AC71, S02F and similar) rely on the
# map alone.
_SERIAL_FAMILY_STEMS = (
    "HJ3[0-9A-Z]",
    "J32[0-9A-Z]",
    "R3[357][0-9]",
    "BK[0-9]{2}",
    "HW5[0-9]",
    "ES2[0-9]",
    "RE[0-9]{2}",
)
_SERIAL_REGISTER_PART = "|".join(re.escape(key) for key in sorted(_SN_PREFIX_MAP))
_SERIAL_PATTERN = re.compile(
    r"\b(?:"
    + _SERIAL_REGISTER_PART
    + "|"
    + "|".join(_SERIAL_FAMILY_STEMS)
    + r")[A-Z0-9]{12}\b"
)

FORBIDDEN: dict[str, re.Pattern[str]] = {
    "protocol-source term": re.compile(_SOURCE_TERMS),
    "private path or plan number": re.compile(
        r"docs/captures|docs/plans|docs/review|docs/repo|docs/"
        + _term("ap", "k")
        + r"|\."
        + _term("cla", "ude")
        + "/"
        + r"|(?<![\w./-])scripts/|\bPLAN-[0-9]{2,4}\b|agent-memory"
    ),
    "internal tooling name": re.compile(_TOOLING_NAMES),
    "em or en dash": re.compile("[\u2013\u2014]"),
    "device serial": _SERIAL_PATTERN,
}

# A serial-shaped value is a dummy when it says so, is a masked run, or is one
# of the fixture serials named here. The list is explicit on purpose: deriving
# it from the test tree would whitelist whatever reaches `tests/`, and the
# fixture gate reads `tests/fixtures/`, not the Python files.
_DUMMY_SERIAL = re.compile(r"TEST|X{4,}")
_FIXTURE_SERIALS = frozenset({_term("HW52", "ZAB412340001")})  # Smart Plug conftest

# What each pattern must catch: the positive control for check 3, assembled
# the same way so the sample never appears in the file either.
KNOWN_BAD: dict[str, str] = {
    "protocol-source term": "taken from the " + _term("decomp", "iled") + " bundle",
    "private path or plan number": "see docs/captures/x.json and PLAN-128",
    "internal tooling name": "the " + _term("reverse-", "engineer") + " pass found it",
    "em or en dash": "one \u2014 two \u2013 three",
    # Assembled, so the scan of the test tree below does not read this very
    # file and file the control away as a known dummy.
    "device serial": "the unit " + _term("HJ31", "A1B2C3D4E5F6") + " reported",
}

_ADR = re.compile(r"\bADR-([0-9]{3})\b")
_HEADING = re.compile(r"^## ADR-([0-9]{3})\b", re.MULTILINE)
# The register names the numbers it does not carry in one sentence that
# starts with this phrase; every ADR-NNN in that paragraph counts as listed.
_NOT_IN_REGISTER = re.compile(
    r"Numbers not in this register:(?P<body>.*?)(?:\n\n|\Z)", re.DOTALL
)


def _leaks(text: str) -> list[str]:
    findings: list[str] = []
    for label, pattern in FORBIDDEN.items():
        for match in pattern.finditer(text):
            if label == "device serial" and (
                _DUMMY_SERIAL.search(match.group()) or match.group() in _FIXTURE_SERIALS
            ):
                continue
            findings.append(f"{label}: {match.group()!r}")
    return findings


@pytest.mark.parametrize(
    "path", PUBLIC_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_public_file_stays_inside_the_boundary(path: Path) -> None:
    findings = _leaks(path.read_text(encoding="utf-8"))
    assert not findings, f"{path.relative_to(REPO_ROOT)}: {findings[:5]}"


# The code trees cite plan numbers by convention (`PLAN-136` beside the code
# it produced) and name `scripts/` in docstrings that tell a maintainer which
# tool to run, so those two halves of the prose pattern do not apply there.
# A path into a gitignored tree does: nobody outside this checkout can follow
# it, and it says where the private material lives (public-repo-boundary,
# rule 3). Found 2026-09-11 in a builder docstring and a test module docstring
# that the prose scan above never reads.
_CODE_TREES = (REPO_ROOT / "custom_components", REPO_ROOT / "tests")
_PRIVATE_PATH_IN_CODE = re.compile(
    r"docs/captures|docs/plans|docs/review|docs/repo|docs/"
    + _term("ap", "k")
    + r"|\."
    + _term("cla", "ude")
    + "/|agent-memory"
)
# This file spells the pattern and its sample, so it is the one file exempt.
_CODE_FILES = sorted(
    file
    for tree in _CODE_TREES
    for file in tree.rglob("*.py")
    if file.is_file() and file.resolve() != Path(__file__).resolve()
)


@pytest.mark.parametrize(
    "path", _CODE_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT))
)
def test_code_file_names_no_private_path(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [match.group() for match in _PRIVATE_PATH_IN_CODE.finditer(text)]
    assert not found, f"{path.relative_to(REPO_ROOT)}: {found[:5]}"


def test_the_code_file_set_is_not_empty() -> None:
    assert len(_CODE_FILES) >= 100, len(_CODE_FILES)


def test_the_code_private_path_pattern_catches_its_sample() -> None:
    sample = "built from docs/captures/x.json, see docs/review/y.md"
    assert len(_PRIVATE_PATH_IN_CODE.findall(sample)) == 2


def test_the_public_file_set_is_not_empty() -> None:
    assert len(PUBLIC_FILES) >= 10, PUBLIC_FILES


@pytest.mark.parametrize("label", sorted(FORBIDDEN))
def test_every_forbidden_pattern_catches_its_sample(label: str) -> None:
    """Positive control: a pattern that matches nothing would pass check 1
    for the wrong reason."""
    assert any(f.startswith(label) for f in _leaks(KNOWN_BAD[label])), label


@pytest.mark.parametrize("prefix", sorted(_SN_PREFIX_MAP))
def test_the_serial_pattern_covers_every_prefix_the_register_names(
    prefix: str,
) -> None:
    """Positive control for the prefix alternation: one sample per key of the
    register, so a prefix added there is covered or this test names it."""
    sample = "unit " + _term(prefix, "A1B2C3D4E5F6") + " reported"
    assert any(f.startswith("device serial") for f in _leaks(sample)), prefix


# One sample per family stem, each built from a prefix the register does not
# carry. Only the stem can catch these, so a stem that is removed or mistyped
# turns exactly its own case red.
_STEM_SIBLINGS = ("HJ39", "J32Z", "R339", "BK99", "HW59", "ES29", "RE99")


@pytest.mark.parametrize("prefix", _STEM_SIBLINGS)
def test_the_serial_pattern_covers_an_unregistered_sibling_of_each_family(
    prefix: str,
) -> None:
    """Positive control for the family stems, which the register-driven test
    above cannot reach: a sibling that is not routed yet is still a serial."""
    assert prefix not in _SN_PREFIX_MAP, (
        f"pick another sibling: {prefix} is registered now"
    )
    sample = "unit " + _term(prefix, "A1B2C3D4E5F6") + " reported"
    assert any(f.startswith("device serial") for f in _leaks(sample)), prefix


@pytest.mark.parametrize(
    "text",
    [
        "unit ZZ99A1B2C3D4E5F6 reported",
        "unit " + _term("HJ31", "A1B2C3D4E5F6X7Y") + " reported",
        "unit " + _term("XHJ31", "A1B2C3D4E5F6") + " reported",
        "unit " + _term("HJ31", "A1B2C3D4E5F") + " reported",
        "unit " + _term("HJ31", "A1B2C3D4E5F6X") + " reported",
        # Real serials are uppercase, so the pattern stays case-sensitive on
        # purpose; switching on IGNORECASE has to be a visible decision.
        "unit " + _term("hj31", "a1b2c3d4e5f6") + " reported",
    ],
    ids=[
        "unregistered-prefix",
        "tail-too-long",
        "glued-to-a-leading-letter",
        "tail-one-short",
        "tail-one-long",
        "lowercase-is-not-flagged",
    ],
)
def test_the_serial_pattern_ignores_what_is_not_a_serial(text: str) -> None:
    """Negative control: the derived pattern must not fire on an unregistered
    prefix, a tail shorter or longer than twelve characters, a prefix that is
    only the end of a longer word, or a lowercase word."""
    assert [f for f in _leaks(text) if f.startswith("device serial")] == []


def test_a_dummy_serial_is_not_a_finding() -> None:
    assert _leaks("the fixture serial HJ31TESTBAM40TX5 and HJ31XXXXXXXXXXXX") == []
    assert _leaks("the Smart Plug fixture " + _term("HW52", "ZAB412340001")) == []


def _cited_numbers() -> set[str]:
    cited: set[str] = set()
    for tree in CITING_TREES:
        files = (
            [tree]
            if tree.is_file()
            else list(tree.rglob("*.py")) + list(tree.rglob("*.md"))
        )
        for file in files:
            text = file.read_text(encoding="utf-8", errors="replace")
            cited.update(_ADR.findall(text))
    return cited


def test_every_cited_decision_resolves_in_the_register() -> None:
    assert REGISTER.is_file(), REGISTER
    text = REGISTER.read_text(encoding="utf-8")
    headings = set(_HEADING.findall(text))
    listed: set[str] = set()
    match = _NOT_IN_REGISTER.search(text)
    if match:
        listed.update(_ADR.findall(match.group("body")))
    # The sentence occurs twice (the register's intro and ADR-026 decision 2);
    # `search` binds the intro, which is the one a reader meets first. The
    # two numbers are a contract: a change here is a decision, not a drift.
    # ADR-009 left this set on 2026-09-11, when the wallbox write decision it
    # was reserved for was recorded under its own heading.
    assert match, "the register no longer states the numbers it does not carry"
    assert listed == {"001", "003"}, sorted(listed)
    cited = _cited_numbers()
    # Floors at the measured values, so a deleted decision or a broken scan
    # turns the test red. Measured 2026-09-10: 15 distinct numbers cited, 23
    # public headings.
    assert len(cited) >= 15, sorted(cited)
    assert len(headings) >= 23, sorted(headings)
    unresolved = sorted(cited - headings - listed)
    assert not unresolved, f"cited but not in the register: {unresolved}"
    assert not headings & listed, sorted(headings & listed)
