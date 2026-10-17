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
from custom_components.ecoflow_energy.sensor import _get_sensor_defs
from tests.test_entity_documentation import DEFINITION_LISTS, DOCS_DIR, FAMILY_TO_FILE

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


def _family_page(device_type: str) -> Path:
    """The entity page that catalogs `device_type`, derived from the code and
    the existing family-to-file mapping, so no third mapping exists: the
    sensor list the platform serves for the device type is the very list
    object `DEFINITION_LISTS` files under a family name, and
    `FAMILY_TO_FILE` names that family's page."""
    served = _get_sensor_defs(device_type)
    families = [
        family
        for family, lists in DEFINITION_LISTS.items()
        if lists.get("sensors") is served
    ]
    assert len(families) == 1, (
        f"device type {device_type!r} must resolve to exactly one definition "
        f"family, got {families}"
    )
    return DOCS_DIR / FAMILY_TO_FILE[families[0]]


def test_every_sn_prefix_is_on_its_family_entity_page():
    """Every `_SN_PREFIX_MAP` key appears backticked on the entity page of
    its own family under documentation/entities/. "On its family page", not
    "on exactly one page": HJ31, J32D and J32E also appear on ocean-2.md."""
    pages: dict[Path, str] = {}
    missing: list[str] = []
    for prefix, device_type in sorted(_SN_PREFIX_MAP.items()):
        page = _family_page(device_type)
        text = pages.setdefault(page, page.read_text(encoding="utf-8"))
        if f"`{prefix}`" not in text:
            missing.append(f"{prefix} (expected on {page.name})")
    assert not missing, (
        f"serial prefix(es) missing, backticked, from their family entity "
        f"page: {missing}"
    )


_STARRED_PREFIX_RE = re.compile(r"`([A-Z0-9]{4})`\\\*")
_ENHANCED_ONLY_LIST_RE = re.compile(r"All except the Enhanced-only serials \(([^)]*)\)")


def _enhanced_only_prefixes_in_readme_table(readme: str) -> set[str]:
    """Derive the Enhanced-only prefixes from the Supported Devices table, the
    one place that registers them: a row whose Connection cell starts with
    "Enhanced only" contributes all its prefixes, and a `\\*` after a prefix
    in any other row marks that one prefix. Untested prefixes carry neither."""
    section = _section(readme, "## Supported Devices")
    derived: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| **"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        prefix_cell, connection_cell = cells[1], cells[2]
        if connection_cell.startswith("Enhanced only"):
            derived.update(_BACKTICK_PREFIX_RE.findall(prefix_cell))
        else:
            derived.update(_STARRED_PREFIX_RE.findall(prefix_cell))
    return derived


def test_enhanced_only_prefixes_in_the_readme_agree_with_the_table_and_code():
    """The Supported Devices table registers which prefixes are Enhanced
    only. (a) The one complete list elsewhere in README.md, the Devices cell
    "All except the Enhanced-only serials (...)", equals what the table
    yields. (b) Every prefix whose device type the code lists in
    `ENHANCED_ONLY_DEVICE_TYPES` is in the table's set: the code owns the type
    level, the table adds the prefix level."""
    readme = README_PATH.read_text(encoding="utf-8")
    derived = _enhanced_only_prefixes_in_readme_table(readme)
    assert derived, "no Enhanced-only prefix parsed from the Supported Devices table"

    listed_match = _ENHANCED_ONLY_LIST_RE.search(readme)
    assert listed_match, (
        "README.md lost its 'All except the Enhanced-only serials' list"
    )
    listed = set(_BACKTICK_PREFIX_RE.findall(listed_match.group(1)))
    assert listed == derived, (
        f"README.md's Enhanced-only list and its Supported Devices table "
        f"disagree: only in the list {sorted(listed - derived)}, only in the "
        f"table {sorted(derived - listed)}"
    )

    code_enhanced_only = {
        prefix
        for prefix, device_type in _SN_PREFIX_MAP.items()
        if device_type in const.ENHANCED_ONLY_DEVICE_TYPES
    }
    assert code_enhanced_only <= derived, (
        f"prefix(es) {sorted(code_enhanced_only - derived)} belong to a device "
        f"type in ENHANCED_ONLY_DEVICE_TYPES but the README table does not "
        f"mark them Enhanced only"
    )


def test_powerpulse2_definition_counts():
    """PowerPulse 2's entity counts (18 sensors, 1 binary sensor, 2 selects,
    2 buttons), and its six Numbers as the entry-gated set: five written
    only through a sibling PowerOcean (`ev_max_current_a`, the two stored
    setpoints `ev_solar_min_current_a` and `ev_custom_current_a`, and the two
    display brightnesses `ev_screen_brightness_pct` and
    `ev_led_brightness_pct`) and one written only on the wallbox's own channel
    with no PowerOcean (`ev_charge_current_a`), never both from the same
    route. The two selects
    are the charging mode and the phase setting, both sibling-only.
    PowerPulse 2 has no test in test_entity_documentation.py's
    `_ENTITY_REFERENCE_SIMPLE_DEVICES` because its documentation/README.md
    bullet has two routes rather than a single PowerOcean-gated footnote, so
    nothing gated it before this."""
    assert len(const.POWERPULSE2_SENSORS) == 18
    assert len(const.POWERPULSE2_BINARY_SENSORS) == 1
    assert len(const.POWERPULSE2_SELECTS) == 2
    assert len(const.POWERPULSE2_BUTTONS) == 2

    numbers = const.POWERPULSE2_NUMBERS
    assert len(numbers) == 6
    routes = sorted(n.powerpulse_route or "" for n in numbers)
    assert routes == ["own"] + ["sibling"] * 5, (
        f"POWERPULSE2_NUMBERS must be exactly one 'own'-route number (needs "
        f"no PowerOcean) and five 'sibling'-route numbers (need one), "
        f"got routes {routes}"
    )
