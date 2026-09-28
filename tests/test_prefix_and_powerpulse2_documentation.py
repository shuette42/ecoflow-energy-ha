"""Drift gate between `_SN_PREFIX_MAP` / the PowerPulse 2 definition lists
and the two public documents that restate them by hand (F18, 2026-09-28
release audit).

`test_entity_documentation.py` already gates every device family's entity
tables against const.py, but nothing tied the serial-prefix lists in
README.md's Supported Devices table and documentation/README.md's Entity
Reference bullets to `_SN_PREFIX_MAP` itself, and nothing gated the
PowerPulse 2 entity counts at all - that family is absent from
`_ENTITY_REFERENCE_SIMPLE_DEVICES` because its README bullet carries two
routes with no shared PowerOcean count, not a primary/footnote split. Both
gaps let a missing prefix (`P351`, `C376`/`C374`) or a stale PowerPulse 2
count ship silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from custom_components.ecoflow_energy import const
from custom_components.ecoflow_energy.ecoflow.const import _SN_PREFIX_MAP

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
DOC_README_PATH = REPO_ROOT / "documentation" / "README.md"

# A serial prefix is always four uppercase letters/digits (see _SN_PREFIX_MAP
# itself: HJ31, J32D, D3M1, S02F, ...). Scoping the backtick pattern to that
# shape, and to lines that are table rows / Entity Reference bullets, keeps
# this from matching an unrelated four-character backtick token elsewhere in
# either document (e.g. a unit or a command name).
_BACKTICK_PREFIX_RE = re.compile(r"`([A-Z0-9]{4})`")


def _section(text: str, start_heading: str) -> str:
    """Text from `start_heading` (a literal '## ...' line) up to the next
    '## ' heading, or end of file."""
    start = text.index(start_heading)
    rest = text[start + len(start_heading) :]
    next_heading = rest.find("\n## ")
    return rest if next_heading == -1 else rest[:next_heading]


def _prefixes_in_lines_starting_with(text: str, line_prefix: str) -> set[str]:
    found: set[str] = set()
    for line in text.splitlines():
        if line.startswith(line_prefix):
            found.update(_BACKTICK_PREFIX_RE.findall(line))
    return found


def test_every_sn_prefix_is_in_the_readme_supported_devices_table():
    """Every `_SN_PREFIX_MAP` key must appear, backticked, in a Supported
    Devices table row of README.md - the check that would have caught F4/F7
    (PowerPulse 2's `C376`/`C374` and Delta 3's `P351` missing from the
    table)."""
    section = _section(README_PATH.read_text(encoding="utf-8"), "## Supported Devices")
    documented = _prefixes_in_lines_starting_with(section, "| **")
    missing = set(_SN_PREFIX_MAP) - documented
    assert not missing, (
        f"README.md's Supported Devices table is missing serial prefix(es) "
        f"{sorted(missing)} that _SN_PREFIX_MAP routes to a device type"
    )


def test_every_sn_prefix_is_in_the_documentation_readme_entity_reference():
    """Same check against documentation/README.md's Entity Reference bullet
    list, which restates the same prefixes in its own words."""
    section = _section(
        DOC_README_PATH.read_text(encoding="utf-8"), "## Entity Reference"
    )
    documented = _prefixes_in_lines_starting_with(section, "- [")
    missing = set(_SN_PREFIX_MAP) - documented
    assert not missing, (
        f"documentation/README.md's Entity Reference list is missing serial "
        f"prefix(es) {sorted(missing)} that _SN_PREFIX_MAP routes to a "
        f"device type"
    )


def test_powerpulse2_definition_counts():
    """PowerPulse 2's entity counts (18 sensors, 1 binary sensor, 1 select,
    2 buttons), and its two Numbers as the entry-gated pair: one written
    only through a sibling PowerOcean (`ev_max_current_a`) and one written
    only on the wallbox's own channel with no PowerOcean (`ev_charge_current_a`),
    never both from the same route. PowerPulse 2 has no test in
    test_entity_documentation.py's `_ENTITY_REFERENCE_SIMPLE_DEVICES` because
    its documentation/README.md bullet has two routes rather than a single
    PowerOcean-gated footnote, so nothing gated it before this."""
    assert len(const.POWERPULSE2_SENSORS) == 18
    assert len(const.POWERPULSE2_BINARY_SENSORS) == 1
    assert len(const.POWERPULSE2_SELECTS) == 1
    assert len(const.POWERPULSE2_BUTTONS) == 2

    numbers = const.POWERPULSE2_NUMBERS
    assert len(numbers) == 2
    routes = sorted(n.powerpulse_route for n in numbers)
    assert routes == ["own", "sibling"], (
        f"POWERPULSE2_NUMBERS must be exactly one 'sibling'-route number "
        f"(needs a PowerOcean) and one 'own'-route number (needs none), "
        f"got routes {routes}"
    )
