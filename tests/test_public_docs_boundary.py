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

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCUMENTATION = REPO_ROOT / "documentation"
REGISTER = DOCUMENTATION / "architecture" / "decisions.md"

# Public prose files. CHANGELOG.md carries history back to v1.0.0, so its older
# entries are read as they stand; the dash check below is the only one it
# could trip on, and it is scoped to the em dash the style rule forbids.
PUBLIC_FILES = sorted(
    [REPO_ROOT / "README.md", REPO_ROOT / "CHANGELOG.md"]
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
    "device serial": re.compile(
        r"\b(?:HJ3[0-9A-Z]|HW5[0-9]|R3[357][0-9]|BK[0-9]{2}|C376|D3M1|D3N1"
        r"|P321|P231|AC71|ES2[12]|RE1[17]|J32[0-9A-Z]|HZ31|S02F)[A-Z0-9]{12}\b"
    ),
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


def test_the_public_file_set_is_not_empty() -> None:
    assert len(PUBLIC_FILES) >= 10, PUBLIC_FILES


@pytest.mark.parametrize("label", sorted(FORBIDDEN))
def test_every_forbidden_pattern_catches_its_sample(label: str) -> None:
    """Positive control: a pattern that matches nothing would pass check 1
    for the wrong reason."""
    assert any(f.startswith(label) for f in _leaks(KNOWN_BAD[label])), label


def test_the_serial_pattern_covers_every_prefix_the_register_names() -> None:
    """Positive control for the prefix alternation, one sample per family
    that the first version missed (HJ3C, R371, R374)."""
    for prefix in ("HJ3C", "R371", "R374", "HJ31", "BK21", "J32E"):
        sample = "unit " + _term(prefix, "A1B2C3D4E5F6") + " reported"
        assert any(f.startswith("device serial") for f in _leaks(sample)), prefix


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
    # three numbers are a contract: a change here is a decision, not a drift.
    assert match, "the register no longer states the numbers it does not carry"
    assert listed == {"001", "003", "009"}, sorted(listed)
    cited = _cited_numbers()
    # Floors at the measured values, so a deleted decision or a broken scan
    # turns the test red. Measured 2026-09-10: 15 distinct numbers cited, 23
    # public headings.
    assert len(cited) >= 15, sorted(cited)
    assert len(headings) >= 23, sorted(headings)
    unresolved = sorted(cited - headings - listed)
    assert not unresolved, f"cited but not in the register: {unresolved}"
    assert not headings & listed, sorted(headings & listed)
