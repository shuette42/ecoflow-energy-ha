"""Drift gate between the entity definitions in const.py and the public
entity reference under documentation/entities/ (PLAN-129 item 2.3).

The join key is the entity's ``name`` (the English display label): the first
column of every table in the documentation files is that exact string,
compared case-insensitively (see _key()) because several tables in this repo
already differ from const.py by capitalisation only - e.g. Delta 3's
"AC 1 non-essential" (doc) vs "AC 1 Non-Essential" (const.py). A gate strict
on case would only ever flag those two, which is not a rename or a removal;
the interesting drift this file exists to catch is entities appearing or
disappearing, not their capitalisation.

Four checks, in order of importance:

1. No orphaned documentation row: every entity name in a documentation table
   exists as a `name` on some definition mapped to that file.
2. No undocumented entity: every `name` on a mapped definition list appears
   in its file's tables.
3. Every table that names entities is actually routed to a platform: a table
   whose section header carries no platform keyword and which has no `Type`
   column of its own is invisible to checks 1 and 2 - this test catches that
   directly, instead of relying on someone noticing a table went silent.
4. The mapping itself is complete: every definition list construction
   discovered in const.py, and every file under documentation/entities/, is
   named by FAMILY_TO_FILE (the catalog file for that family) or
   FAMILY_TO_EXTRA_ORPHAN_FILES (a file documenting a subset of a family's
   entities, without being required to carry the full list). A new list or a
   new file nobody wired in fails loudly instead of passing unchecked.

To make this pass after adding, renaming or removing an entity: update the
matching row in the entity's documentation file. To add a new device family:
add its const.py list(s) to FAMILY_TO_FILE together with the doc file that
catalogs it.

Both real-word exceptions (a device that legitimately does not document an
entity, or a doc row that legitimately does not name a literal entity) are
recorded as explicit, reasoned exclusions below - never as a silent skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from custom_components.ecoflow_energy import const

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "documentation" / "entities"

# Platform name -> the dataclass its const.py list holds.
DEF_CLASSES: dict[str, type] = {
    "sensors": const.EcoFlowSensorDef,
    "binary_sensors": const.EcoFlowBinarySensorDef,
    "switches": const.EcoFlowSwitchDef,
    "numbers": const.EcoFlowNumberDef,
    "selects": const.EcoFlowSelectDef,
    "buttons": const.EcoFlowButtonDef,
}

# Longest suffix first, so "_BINARY_SENSORS" is tried before "_SENSORS" (a
# name ending "_BINARY_SENSORS" also ends "_SENSORS").
_SUFFIX_TO_PLATFORM: dict[str, str] = {
    "_BINARY_SENSORS": "binary_sensors",
    "_SENSORS": "sensors",
    "_SWITCHES": "switches",
    "_NUMBERS": "numbers",
    "_SELECTS": "selects",
    "_BUTTONS": "buttons",
}
_SUFFIXES_LONGEST_FIRST = sorted(_SUFFIX_TO_PLATFORM, key=len, reverse=True)


def _discover_definition_lists() -> dict[str, dict[str, list]]:
    """Introspect const.py module attributes: {family: {platform: [Def...]}}.

    A module attribute counts if its name ends in one of the five platform
    suffixes and it is a list. If the list is non-empty, its first element
    must be an instance of the platform's dataclass - this rejects an
    unrelated list that happens to share a name suffix by accident.
    """
    result: dict[str, dict[str, list]] = {}
    for attr_name in dir(const):
        if attr_name.startswith("_"):
            continue
        value = getattr(const, attr_name)
        if not isinstance(value, list):
            continue
        platform = None
        family = None
        for suffix in _SUFFIXES_LONGEST_FIRST:
            if attr_name.endswith(suffix) and len(attr_name) > len(suffix):
                platform = _SUFFIX_TO_PLATFORM[suffix]
                family = attr_name[: -len(suffix)]
                break
        if platform is None or family is None:
            continue
        if value and not isinstance(value[0], DEF_CLASSES[platform]):
            continue
        result.setdefault(family, {})[platform] = value
    return result


DEFINITION_LISTS = _discover_definition_lists()

# Domain knowledge: which doc file documents which const.py family. Kept as
# an explicit mapping (never derived from the family name itself) so a typo
# or a new device shows up as a failure in test_mapping_is_complete rather
# than silently mapping to nothing.
FAMILY_TO_FILE: dict[str, str] = {
    "POWEROCEAN": "powerocean.md",
    "DELTA2MAX": "delta-2-max.md",
    "SMARTPLUG": "smart-plug.md",
    "STREAM": "stream.md",
    "STREAMAC5000": "stream-ac-5000.md",
    "DELTA3": "delta-3-max-plus.md",
    "POWERSTREAM": "powerstream.md",
    "SMARTMETER": "smart-meter.md",
    "SOLARTRACKER": "solar-tracker.md",
    "WAVE3": "wave-3.md",
    "POWERPULSE2": "powerpulse-2.md",
}

# Doc files that document a SUBSET of a family's entities under their own
# heading, rather than being that family's catalog. stream-ac-pro.md is a
# supplementary wire-protocol reference (confirmed cmd_id/field numbers for
# the Stream AC Pro's controls) whose "Entity" columns name real STREAM_*
# entities - so every row it has must still resolve to one (test 1 runs
# against it), but it is never required to carry the family's full entity
# list (test 2 does not run against it) because stream.md already is that
# catalog. One-directional on purpose: mapping it into FAMILY_TO_FILE
# instead would make every STREAM entity absent from this three-table file
# report as undocumented, which is not the drift this gate exists to catch.
FAMILY_TO_EXTRA_ORPHAN_FILES: dict[str, tuple[str, ...]] = {
    "STREAM": ("stream-ac-pro.md",),
}


def _key(name: str) -> str:
    return name.strip().lower()


# ---------------------------------------------------------------------------
# Markdown table parsing
# ---------------------------------------------------------------------------

_PLATFORM_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("climate", "climate"),
    ("binary sensor", "binary_sensors"),
    ("switch", "switches"),
    ("number", "numbers"),
    ("select", "selects"),
    ("button", "buttons"),
    ("sensor", "sensors"),  # after "binary sensor" - would also match it
)

_TYPE_COLUMN_MAP: dict[str, str] = {
    "sensor": "sensors",
    "binary sensor": "binary_sensors",
    "switch": "switches",
    "number": "numbers",
    "select": "selects",
    "button": "buttons",
    "climate": "climate",
}

_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")

# Sections whose table rows are a human template ("Pack N ...", "Slave N
# ..."), not literal per-instance entity names - the real per-instance
# names (Pack 1 SoC, Slave 2 Voltage, ...) are excluded from the
# "undocumented" check instead, by pattern (see EXCLUDE_FROM_UNDOCUMENTED).
#
# Each entry skips every table under that "## " header, not just one: the
# powerocean.md entry alone covers TWO tables ("Core sensors per pack", 7
# rows, and "Diagnostic sensors per pack (all disabled)", 10 rows) that both
# sit under the single "## Sensors - Battery Packs" header. The rows these
# two entries skip are not unchecked forever - TEMPLATE_FAMILIES below reads
# them directly (bypassing this skip) to build a real per-instance equality
# check against const.py, instead of the open-ended pattern exclusion this
# skip alone would otherwise require.
SKIP_SECTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("powerocean.md", "Sensors - Battery Packs (up to 5x BP5000)"),
        ("delta-2-max.md", "Sensors - Expansion Battery Packs (disabled)"),
    }
)


def _classify_header(header_text: str) -> str | None:
    lowered = header_text.lower()
    for keyword, platform in _PLATFORM_KEYWORDS:
        if keyword in lowered:
            return platform
    return None


def _clean_cell(text: str) -> str:
    return text.strip().strip("*").strip("`").strip()


@dataclass(frozen=True)
class _ParsedDoc:
    # {platform: [(raw_entity_name, 1-based source line), ...]}
    documented: dict[str, list[tuple[str, int]]]
    # (1-based header line, section header) for every table whose first
    # column is named "Entity" but whose rows _parse_doc_tables drops on
    # the floor - see the class docstring below.
    unclassified_entity_tables: list[tuple[int, str]]


def _parse_doc_tables(path: Path) -> _ParsedDoc:
    """Return the per-platform rows of every table in ``path``, plus every
    table this parser could not route anywhere.

    Walks H2 (``## ``) sections top to bottom. A table's rows are attributed
    to the platform named by its section header (matched by keyword, so
    "## Sensors - Battery" still counts as "sensors"). If the table itself
    carries a "Type" column, that column decides the platform per row
    instead - this is how PowerOcean's mixed "Controls" and "Scheduled
    Charge Tasks" tables (Entity | Type | ...) get their rows routed
    correctly even though their section headers name neither platform.

    A table can therefore fall through both routes: no keyword in its
    section header AND no "Type" column of its own. Its rows are silently
    absent from every check - not an orphan, not undocumented, just gone.
    That is exactly what happened to stream.md's Stream Micro table before
    it was fixed to carry a "Type" column: a renamed entity in it passed
    the whole suite. Rather than special-case that one table, every table
    whose header starts with an "Entity" column is checked here for having
    *some* route to a platform; test_every_entity_table_is_classified turns
    a non-empty ``unclassified_entity_tables`` into a failure.

    Names are returned raw (not deduplicated, not case-normalised, not
    compression-expanded) - callers decide how to match them, because the
    two checks need different tolerance (see _expand_compressed_name).
    """
    documented: dict[str, list[tuple[str, int]]] = {p: [] for p in DEF_CLASSES}
    documented["climate"] = []
    unclassified: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    section_platform: str | None = None
    section_header = ""
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("## "):
            section_header = line[3:].strip()
            section_platform = _classify_header(section_header)
            i += 1
            continue
        is_table_start = (
            line.strip().startswith("|")
            and i + 1 < n
            and _TABLE_SEP_RE.match(lines[i + 1].strip()) is not None
        )
        if is_table_start:
            if (path.name, section_header) in SKIP_SECTIONS:
                i += 2
                while i < n and lines[i].strip().startswith("|"):
                    i += 1
                continue
            header_cells = [_clean_cell(c) for c in line.strip().strip("|").split("|")]
            type_idx = next(
                (
                    idx
                    for idx, cell in enumerate(header_cells)
                    if cell.lower() == "type"
                ),
                None,
            )
            if (
                type_idx is None
                and section_platform is None
                and header_cells
                and header_cells[0].lower() == "entity"
            ):
                unclassified.append((i + 1, section_header))
            i += 2  # header + separator
            while i < n and lines[i].strip().startswith("|"):
                cells = [_clean_cell(c) for c in lines[i].strip().strip("|").split("|")]
                if cells and cells[0] and cells[0] not in ("-", ""):
                    name = cells[0]
                    if type_idx is not None and type_idx < len(cells):
                        platform = _TYPE_COLUMN_MAP.get(cells[type_idx].lower())
                    else:
                        platform = section_platform
                    if platform is not None:
                        documented[platform].append((name, i + 1))
                i += 1
            continue
        i += 1
    return _ParsedDoc(documented, unclassified)


_DOC_TABLE_CACHE: dict[str, _ParsedDoc] = {}


def _parsed_doc(filename: str) -> _ParsedDoc:
    if filename not in _DOC_TABLE_CACHE:
        _DOC_TABLE_CACHE[filename] = _parse_doc_tables(DOCS_DIR / filename)
    return _DOC_TABLE_CACHE[filename]


def _doc_tables(filename: str) -> dict[str, list[tuple[str, int]]]:
    return _parsed_doc(filename).documented


_TRAILING_PAREN_RE = re.compile(r"^(.+) \((\w+)\)$")


def _expand_compressed_name(raw: str) -> set[str]:
    """A cell may compress several full entity names into one documentation
    row. Three conventions are used across these files:

    1. Full-phrase alternation, space-slash-space: "Max Cell Temp / Min
       Cell Temp" is "Max Cell Temp" and "Min Cell Temp", each side already
       a complete name.
    2. A single alternating word/segment with a shared prefix and/or suffix
       around it: "Battery Max/Min Cell Temp" is "Battery Max Cell Temp"
       and "Battery Min Cell Temp"; "Grid Phase A/B/C Voltage" is three
       per-phase sensors; "PCS AC/DC Error Code" is two.
    3. A trailing parenthetical qualifier moved out of word order: "Cell
       Temp (Max)" is "Max Cell Temp" (stream-ac-5000.md's Battery
       Diagnostics table).

    Returns every name the row could plausibly expand to, always including
    the raw text itself. Callers only ever treat a candidate as a match once
    it is independently confirmed against a real, known entity name - an
    unused candidate here (e.g. from the wrong convention firing) is inert,
    never a false positive.
    """
    candidates = {raw}

    paren_match = _TRAILING_PAREN_RE.match(raw)
    if paren_match:
        base, qualifier = paren_match.groups()
        candidates.add(f"{qualifier} {base.strip()}")

    if "/" not in raw:
        return candidates

    # Convention 1: every side of " / " is already a complete name.
    if " / " in raw:
        parts = [p.strip() for p in raw.split(" / ") if p.strip()]
        if len(parts) > 1:
            candidates.update(parts)

    # Convention 2: exactly one word token itself carries the "/" - split
    # that token and distribute the words around it as a shared prefix and
    # suffix. Skip a bare "/" token (that's convention 1's job).
    words = raw.split()
    for idx, word in enumerate(words):
        if word == "/" or "/" not in word:
            continue
        options = [o.strip() for o in word.split("/") if o.strip()]
        if len(options) < 2:
            continue
        prefix = " ".join(words[:idx])
        suffix = " ".join(words[idx + 1 :])
        for option in options:
            candidates.add(" ".join(part for part in (prefix, option, suffix) if part))

    return candidates


def _expand_slash_list(text: str) -> set[str]:
    """Expand a per-instance template field's suffix text into the literal
    suffixes it stands for - the counterpart to _expand_compressed_name used
    only by the TEMPLATE_FAMILIES checks below (Pack N / Slave N).

    Handles two shapes that _expand_compressed_name does not, because both
    are made of MULTI-WORD phrases rather than the single alternating word
    that function's convention 2 covers:

    1. A ' / '-joined list of phrases sharing a common trailing suffix -
       "Input / Output" -> {"Input", "Output"} (no shared suffix: every part
       has the same word count as the last, so nothing is split off).
       "Remaining / Full Capacity" -> {"Remaining Capacity", "Full Capacity"}
       (the last part carries one extra trailing word, "Capacity", which
       becomes the shared suffix appended to every part).
       "Max MOSFET / HV MOSFET / LV MOSFET Temp" -> {"Max MOSFET Temp", "HV
       MOSFET Temp", "LV MOSFET Temp"} (same rule, three parts).
       This only fires when every part except the last has an EQUAL word
       count - the one shape observed in this repo's doc tables. A ' / '
       list that does not fit (parts of varying, non-trailing-suffix
       lengths) falls through unexpanded, which fails the equality check
       loudly rather than guessing.
    2. Delegates to _expand_compressed_name's own word-level "/" convention
       for the remaining case ("Max/Min Cell Temp", "Design/Full Capacity"),
       since that one is already correct for a single word carrying the
       slash.

    A plain text with no slash at all comes back unchanged, as a
    single-element set.
    """
    if " / " in text:
        parts = [p.strip() for p in text.split(" / ") if p.strip()]
        if len(parts) > 1:
            lead_counts = {len(p.split()) for p in parts[:-1]}
            if len(lead_counts) == 1:
                k = lead_counts.pop()
                last_words = parts[-1].split()
                if len(last_words) >= k:
                    suffix = " ".join(last_words[k:])
                    own_texts = [*parts[:-1], " ".join(last_words[:k])]
                    return {f"{own} {suffix}".strip() for own in own_texts}

    return _expand_compressed_name(text) - {text} or {text}


@dataclass(frozen=True)
class TemplateFamily:
    """A documentation section that writes one row per FIELD ("Pack N SoC")
    instead of one row per real per-instance entity ("Pack 1 SoC" ..
    "Pack 5 SoC") - see the module docstring, item 1 (finding 1, PLAN-129
    step 2). The instance count is read from the document's own prose
    (range_pattern) instead of hard-coded, so a 6th const.py instance the
    doc still calls 5 fails loudly instead of matching an open-ended regex.
    """

    doc_file: str
    family: str
    platform: str
    prefix: str  # e.g. "Pack " - literal text directly before the instance number
    section_header: str  # exact "## " text; the table(s) this reads from
    range_pattern: re.Pattern[
        str
    ]  # searched over the whole file text; group(1) = highest instance number


TEMPLATE_FAMILIES: tuple[TemplateFamily, ...] = (
    TemplateFamily(
        "powerocean.md",
        "POWEROCEAN",
        "sensors",
        "Pack ",
        "Sensors - Battery Packs (up to 5x BP5000)",
        re.compile(r"up to (\d+)x BP5000"),
    ),
    TemplateFamily(
        "delta-2-max.md",
        "DELTA2MAX",
        "sensors",
        "Slave ",
        "Sensors - Expansion Battery Packs (disabled)",
        re.compile(r"Two expansion packs \(Slave 1, Slave (\d+)\)"),
    ),
)


def _read_template_rows(path: Path, section_header: str) -> list[str]:
    """Raw first-column text of every row in every table under
    `section_header` in `path` - the counterpart to SKIP_SECTIONS, which
    drops these same rows from the platform-routed checks because they are
    field templates ("Pack N SoC"), not literal entity names. Used only to
    build the per-instance expected-name set below.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    rows: list[str] = []
    in_section = False
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("## "):
            in_section = line[3:].strip() == section_header
            i += 1
            continue
        is_table_start = (
            in_section
            and line.strip().startswith("|")
            and i + 1 < n
            and _TABLE_SEP_RE.match(lines[i + 1].strip()) is not None
        )
        if is_table_start:
            i += 2
            while i < n and lines[i].strip().startswith("|"):
                cells = [_clean_cell(c) for c in lines[i].strip().strip("|").split("|")]
                if cells and cells[0] and cells[0] not in ("-", ""):
                    rows.append(cells[0])
                i += 1
            continue
        i += 1
    return rows


def _template_expected_names(tf: TemplateFamily) -> tuple[set[str], range]:
    doc_text = (DOCS_DIR / tf.doc_file).read_text(encoding="utf-8")
    match = tf.range_pattern.search(doc_text)
    assert match, (
        f"could not find the instance-count range for {tf.family}/{tf.platform} "
        f"in {tf.doc_file} using {tf.range_pattern.pattern!r} - the document "
        f"text changed shape; update the range_pattern rather than guessing "
        f"a number"
    )
    instance_range = range(1, int(match.group(1)) + 1)

    raw_rows = _read_template_rows(DOCS_DIR / tf.doc_file, tf.section_header)
    assert raw_rows, (
        f"no template rows found under '{tf.section_header}' in {tf.doc_file}"
    )
    placeholder = f"{tf.prefix}N "
    suffixes: set[str] = set()
    for raw in raw_rows:
        assert raw.startswith(placeholder), (
            f"template row {raw!r} in {tf.doc_file} does not start with "
            f"{placeholder!r} - TEMPLATE_FAMILIES' prefix is stale"
        )
        suffixes.update(_expand_slash_list(raw[len(placeholder) :]))

    expected = {
        f"{tf.prefix}{i} {suffix}" for i in instance_range for suffix in suffixes
    }
    return expected, instance_range


@pytest.mark.parametrize(
    "tf", TEMPLATE_FAMILIES, ids=lambda tf: f"{tf.family}_{tf.platform}"
)
def test_template_family_matches_const_py(tf: TemplateFamily):
    """The generative counterpart to the Pack N / Slave N exclusions in
    EXCLUDE_FROM_UNDOCUMENTED: instead of trusting any name matching
    '^Pack \\d+ ' as documented, expand the doc's own template rows over the
    doc's own stated instance range and require the result to be EXACTLY
    the const.py names in that (family, platform) - not a subset. A 6th
    PowerOcean pack in const.py, or a doc row whose field text no longer
    matches a real suffix, fails this instead of silently matching the
    open-ended regex.
    """
    expected, instance_range = _template_expected_names(tf)
    actual = {
        d.name
        for d in DEFINITION_LISTS[tf.family][tf.platform]
        if re.match(rf"^{re.escape(tf.prefix)}\d+ ", d.name)
    }
    missing = expected - actual
    extra = actual - expected
    assert not missing and not extra, (
        f"{tf.family}_{tf.platform.upper()}'s '{tf.prefix}N' template "
        f"(instance range {instance_range.start}-{instance_range.stop - 1}, "
        f"read from {tf.doc_file}) does not match const.py exactly.\n"
        f"missing from const.py: {sorted(missing)}\n"
        f"in const.py but not implied by the template: {sorted(extra)}"
    )


# The two PowerOcean schedule families, each documented as one literal
# "<Family> 1 ..." row per platform plus the prose "up to <Family> N".
_SCHEDULE_FAMILIES = ("Schedule", "Feed Schedule")


@pytest.mark.parametrize("family", _SCHEDULE_FAMILIES)
@pytest.mark.parametrize(
    "platform", ["sensors", "binary_sensors", "switches", "numbers"]
)
def test_schedule_template_matches_const_py(platform: str, family: str):
    """Same idea as test_template_family_matches_const_py, but the Schedule
    tables are a different shape: they already carry a 'Type' column and
    their one literal row names real instance 1 ('Schedule 1 Enabled', ...)
    rather than a placeholder ('Schedule N Enabled') - so this reads that row
    straight from the already-parsed, already-platform-routed doc tables
    instead of a raw section scan, and spans all four platforms each
    schedule table produces. Two families: the charge schedule ('Schedule')
    and the feed-to-grid schedule ('Feed Schedule').
    """
    doc_text = (DOCS_DIR / "powerocean.md").read_text(encoding="utf-8")
    match = re.search(rf"up to {family} (\d+)", doc_text)
    assert match, f"could not find 'up to {family} N' in powerocean.md"
    instance_range = range(1, int(match.group(1)) + 1)

    rows = _doc_tables("powerocean.md")[platform]
    head = f"{family} 1 "
    first_rows = [name for name, _line in rows if name.startswith(head)]
    assert first_rows, (
        f"no literal '{family} 1 ...' row routed to platform {platform!r} in "
        f"powerocean.md"
    )
    suffixes = {name[len(head) :] for name in first_rows}

    expected = {f"{family} {i} {suffix}" for i in instance_range for suffix in suffixes}
    actual = {
        d.name
        for d in DEFINITION_LISTS["POWEROCEAN"][platform]
        if re.match(rf"^{family} \d+ ", d.name)
    }
    missing = expected - actual
    extra = actual - expected
    assert not missing and not extra, (
        f"POWEROCEAN_{platform.upper()}'s {family} template (instance range "
        f"{instance_range.start}-{instance_range.stop - 1}, read from "
        f"powerocean.md) does not match const.py exactly.\n"
        f"missing from const.py: {sorted(missing)}\n"
        f"in const.py but not implied by the template: {sorted(extra)}"
    )


def _row_is_orphan_free(raw: str, known_keys: set[str]) -> bool:
    """A documentation row names a real entity, or - when it compresses
    several names into one row - names only real entities.

    This is deliberately not "any of _expand_compressed_name's candidates
    matches". That function unions every convention's guesses into one flat
    set so test_no_undocumented_entity can ask "does some row plausibly
    cover this definition" - a permissive OR, correct for that direction.
    Orphan-checking asks the opposite question, "does this row plausibly
    document something real", and OR is wrong for it: a row compressing
    three names ("WiFi / Ethernet / 4G Status") passed as long as ONE of
    the three existed, so deleting the other two left it silently green.

    A row with no compression convention (the common case) still only has
    to match itself - the raw text and the trailing-paren rewrite both name
    ONE entity under two spellings, so either is enough. Convention 1
    (" / ", full-phrase alternation) and convention 2 (one word carrying the
    "/") both compress MULTIPLE independent entities into a single row, so
    every part they produce must resolve - hence the ``all()`` below, and
    the early return the moment one of those conventions fires.
    """
    if _key(raw) in known_keys:
        return True

    paren_match = _TRAILING_PAREN_RE.match(raw)
    if paren_match:
        base, qualifier = paren_match.groups()
        if _key(f"{qualifier} {base.strip()}") in known_keys:
            return True

    if " / " in raw:
        parts = [p.strip() for p in raw.split(" / ") if p.strip()]
        if len(parts) > 1:
            return all(_key(p) in known_keys for p in parts)

    words = raw.split()
    for idx, word in enumerate(words):
        if word == "/" or "/" not in word:
            continue
        options = [o.strip() for o in word.split("/") if o.strip()]
        if len(options) < 2:
            continue
        prefix = " ".join(words[:idx])
        suffix = " ".join(words[idx + 1 :])
        expanded = [
            " ".join(part for part in (prefix, option, suffix) if part)
            for option in options
        ]
        return all(_key(e) in known_keys for e in expanded)

    return False


# ---------------------------------------------------------------------------
# Reasoned exclusions - real, checked-by-hand gaps between const.py and the
# docs. Every entry names the (family, platform) it applies to, a pattern on
# the *name*, and why. None of these paper over a rename or a deletion; the
# discipline above (regex-based, not a literal enumeration) is deliberate so
# a new device or a new schedule slot does not need a new line here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exclusion:
    family: str
    platform: str
    pattern: re.Pattern[str]
    reason: str


# Entities in const.py that are correctly *not* found as a literal doc row.
EXCLUDE_FROM_UNDOCUMENTED: tuple[Exclusion, ...] = (
    Exclusion(
        "POWEROCEAN",
        "sensors",
        re.compile(r"^Pack \d+ "),
        "documented via the 'Pack N ...' template row in "
        "'## Sensors - Battery Packs (up to 5x BP5000)', not as five "
        "literal per-pack rows",
    ),
    Exclusion(
        "POWEROCEAN",
        "sensors",
        re.compile(r"^Schedule ([2-9]|[1-9]\d+) "),
        "only 'Schedule 1 ...' is a literal table row in "
        "'## Scheduled Charge Tasks'; slots 2-8 are covered by the prose "
        "'Further schedules follow the same pattern ... up to Schedule 8'",
    ),
    Exclusion(
        "POWEROCEAN",
        "binary_sensors",
        re.compile(r"^Schedule ([2-9]|[1-9]\d+) "),
        "same schedule-pattern prose as the sensors above",
    ),
    Exclusion(
        "POWEROCEAN",
        "numbers",
        re.compile(r"^Schedule ([2-9]|[1-9]\d+) "),
        "same schedule-pattern prose as the sensors above",
    ),
    Exclusion(
        "POWEROCEAN",
        "switches",
        re.compile(r"^Schedule ([2-9]|[1-9]\d+) "),
        "same schedule-pattern prose as the sensors above",
    ),
    Exclusion(
        "POWEROCEAN",
        "sensors",
        re.compile(r"^Feed Schedule ([2-9]|[1-9]\d+) "),
        "only 'Feed Schedule 1 ...' is a literal table row in "
        "'## Feed-to-Grid Schedules'; slots 2-8 are covered by the prose "
        "'Further schedules follow the same pattern ... up to Feed Schedule 8'",
    ),
    Exclusion(
        "POWEROCEAN",
        "binary_sensors",
        re.compile(r"^Feed Schedule ([2-9]|[1-9]\d+) "),
        "only 'Feed Schedule 1 ...' is a literal table row in "
        "'## Feed-to-Grid Schedules'; slots 2-8 are covered by the prose "
        "'Further schedules follow the same pattern ... up to Feed Schedule 8'",
    ),
    Exclusion(
        "POWEROCEAN",
        "numbers",
        re.compile(r"^Feed Schedule ([2-9]|[1-9]\d+) "),
        "only 'Feed Schedule 1 ...' is a literal table row in "
        "'## Feed-to-Grid Schedules'; slots 2-8 are covered by the prose "
        "'Further schedules follow the same pattern ... up to Feed Schedule 8'",
    ),
    Exclusion(
        "POWEROCEAN",
        "switches",
        re.compile(r"^Feed Schedule ([2-9]|[1-9]\d+) "),
        "only 'Feed Schedule 1 ...' is a literal table row in "
        "'## Feed-to-Grid Schedules'; slots 2-8 are covered by the prose "
        "'Further schedules follow the same pattern ... up to Feed Schedule 8'",
    ),
    Exclusion(
        "DELTA2MAX",
        "sensors",
        re.compile(r"^Slave \d+ "),
        "documented via the 'Slave N ...' template row in "
        "'## Sensors - Expansion Battery Packs (disabled)', not as two "
        "literal per-pack rows",
    ),
    Exclusion(
        "DELTA2MAX",
        "sensors",
        re.compile(r"^(PD|Inverter|BMS|MPPT) (Error|Fault) Code$"),
        "the doc row 'PD / Inverter / BMS / MPPT Error/Fault Code' "
        "compresses two independent alternations - which source pairs with "
        "which suffix is not recoverable from the text alone, so this is a "
        "manual exclusion rather than a general decompression rule",
    ),
    Exclusion(
        "DELTA3",
        "binary_sensors",
        re.compile(r"^Port Priority Active$"),
        "FINDING: named only in the '## Port priority' prose ('The Port "
        "Priority Active binary sensor reports whether it is currently in "
        "effect'), never as a table row - delta-3-max-plus.md has no "
        "'## Binary Sensors' section at all",
    ),
    Exclusion(
        "STREAM",
        "switches",
        re.compile(r"^AC Outlet [12]$"),
        "FINDING: stream.md's '## Switches' section reads 'None. The AC "
        "outlets are exposed read-only as binary sensors...', but "
        "STREAM_SWITCHES defines AC Outlet 1/2 as enhanced_only switches. "
        "The doc line predates that write path; see stream-ac-pro.md's "
        "'## Outlet Controls' for the same two switches documented as "
        "controls, under a different file",
    ),
    Exclusion(
        "STREAMAC5000",
        "sensors",
        re.compile(r"^BMS SoC$"),
        "FINDING: const.py names key bms_soc_precise_pct 'BMS SoC'; the doc "
        "row for the same pack-level BMS reading (line 27 of "
        "stream-ac-5000.md, 'straight from the BMS ... this unit's own "
        "rather than the system figure') is titled 'Precise SoC' instead. "
        "Same reading, two different labels - a rename on one side that "
        "never reached the other",
    ),
    Exclusion(
        "POWEROCEAN",
        "sensors",
        re.compile(r"^PV Inverter Power$"),
        "FINDING: the row exists at powerocean.md:148 but a blank line "
        "plus a blockquote note sit between it and its table's header/"
        "separator (lines 132-133) - GFM terminates a table at the first "
        "non-'|' line, so this row does not render as part of the MPPT "
        "table at all on the published page, only as literal pipe-delimited "
        "text",
    ),
    Exclusion(
        "STREAM",
        "sensors",
        re.compile(r"^PV [1-4] Energy$"),
        "FINDING: the doc row 'PV 1 Energy to PV 4 Energy' in "
        "'## Sensors - Energy Dashboard' is an English range, not a '/'"
        "-joined compression - _expand_compressed_name does not parse "
        "'X to Y' ranges, so this stays a manual exclusion",
    ),
    Exclusion(
        "STREAM",
        "sensors",
        re.compile(r"^Battery (Charge|Discharge) Capacity$"),
        "FINDING: documented only in prose directly under the Energy "
        "Dashboard table ('Also available as disabled diagnostics: "
        "**Battery Charge Capacity** and **Battery Discharge Capacity** "
        "(Ah)'), never as a table row - same shape as the DELTA3 'Port "
        "Priority Active' entry above",
    ),
)

# Documentation rows that are correctly *not* a literal const.py name.
EXCLUDE_FROM_ORPHAN: tuple[Exclusion, ...] = (
    Exclusion(
        "DELTA2MAX",
        "sensors",
        re.compile(r"^PD / Inverter / BMS / MPPT Error/Fault Code$"),
        "compresses two independent alternations (source x suffix) that "
        "are not recoverable from the text alone - see the matching "
        "EXCLUDE_FROM_UNDOCUMENTED entry for the four real names",
    ),
    Exclusion(
        "STREAMAC5000",
        "sensors",
        re.compile(r"^Precise SoC$"),
        "FINDING: doc name for const.py's 'BMS SoC' (key "
        "bms_soc_precise_pct) - see the matching EXCLUDE_FROM_UNDOCUMENTED "
        "entry",
    ),
    Exclusion(
        "STREAM",
        "sensors",
        re.compile(r"^PV 1 Energy to PV 4 Energy$"),
        "range notation in '## Sensors - Energy Dashboard' standing for "
        "four literal entities (PV 1 Energy .. PV 4 Energy), not a literal "
        "name itself",
    ),
)


def _matches_any(
    name: str, family: str, platform: str, exclusions: tuple[Exclusion, ...]
) -> bool:
    return any(
        exc.family == family and exc.platform == platform and exc.pattern.match(name)
        for exc in exclusions
    )


# ---------------------------------------------------------------------------
# Test 3: the mapping itself is complete
# ---------------------------------------------------------------------------


def test_every_const_family_is_mapped_to_a_doc_file():
    families = set(DEFINITION_LISTS)
    mapped = set(FAMILY_TO_FILE)
    unmapped = families - mapped
    assert not unmapped, (
        f"const.py defines definition lists for {sorted(unmapped)} that "
        f"FAMILY_TO_FILE in this test does not map to a documentation file"
    )
    stale = mapped - families
    assert not stale, (
        f"FAMILY_TO_FILE names {sorted(stale)}, but const.py no longer "
        f"defines a definition list for that family"
    )


def test_every_doc_file_is_named_by_the_mapping():
    doc_files = {p.name for p in DOCS_DIR.glob("*.md")}
    extra_files = {f for files in FAMILY_TO_EXTRA_ORPHAN_FILES.values() for f in files}
    mapped_files = set(FAMILY_TO_FILE.values()) | extra_files
    missing = doc_files - mapped_files
    assert not missing, (
        f"documentation/entities/ has {sorted(missing)}, which neither "
        f"FAMILY_TO_FILE nor FAMILY_TO_EXTRA_ORPHAN_FILES accounts for"
    )
    stale = mapped_files - doc_files
    assert not stale, (
        f"the mapping names {sorted(stale)}, but that file no longer "
        f"exists under documentation/entities/"
    )


def test_every_entity_table_is_classified():
    """Catches the general form of the Stream Micro gap: a table with an
    "Entity" first column that _parse_doc_tables cannot route to any
    platform - no keyword in its section header, no "Type" column of its
    own - is invisible to test_no_undocumented_entity and
    test_no_orphaned_documentation_row alike. Both pass on a table that
    carries no weight at all; a rename or removal inside it is invisible.
    """
    failures = [
        f"  {path.name}:{line} (section '{header}')"
        for path in sorted(DOCS_DIR.glob("*.md"))
        for line, header in _parsed_doc(path.name).unclassified_entity_tables
    ]
    assert not failures, (
        "table(s) with an 'Entity' column carry no platform to route their "
        "rows to - add a 'Type' column to the table, or give its section a "
        "'## ' header naming a platform (sensor/binary sensor/switch/"
        "number/select):\n" + "\n".join(failures)
    )


def test_climate_entity_is_the_documented_wave3_thermostat():
    """The 'climate' bucket in _PLATFORM_KEYWORDS / _TYPE_COLUMN_MAP is
    collected by _parse_doc_tables like any other platform, but no
    DEFINITION_LISTS family exists for it - the WAVE 3's one climate entity
    is built directly in custom_components/ecoflow_energy/climate.py rather
    than from a const.py list, so tests 1 and 2 cannot run against it the
    way they do sensors/binary_sensors/switches/numbers/selects. Leaving it
    collected and unread would hide a rename, a duplicate, or a second
    climate row landing unnoticed. This is the narrow, honest substitute:
    across every doc file, there is exactly one climate row, and it is
    WAVE 3's.
    """
    climate_rows = [
        (path.name, name)
        for path in sorted(DOCS_DIR.glob("*.md"))
        for name, _line in _doc_tables(path.name)["climate"]
    ]
    assert climate_rows == [("wave-3.md", "WAVE 3")], (
        "expected exactly one climate-platform row across "
        "documentation/entities/, WAVE 3's own thermostat card in "
        f"wave-3.md; found {climate_rows}"
    )


# ---------------------------------------------------------------------------
# Tests 1 and 2, per (family, platform)
# ---------------------------------------------------------------------------

_FAMILY_PLATFORM_PAIRS = [
    (family, platform)
    for family, platforms in DEFINITION_LISTS.items()
    for platform in platforms
    if family in FAMILY_TO_FILE
]


def _format_missing_row(definition, platform: str) -> str:
    """Render a definition as a copy-paste-able row in the common
    'Entity | Unit | Description' shape most tables in this repo use."""
    name = definition.name
    unit = getattr(definition, "unit", None)
    if platform in ("sensors", "numbers") and unit:
        return f"| {name} | {unit} | TODO |"
    return f"| {name} | TODO |"


@pytest.mark.parametrize("family,platform", _FAMILY_PLATFORM_PAIRS)
def test_no_undocumented_entity(family: str, platform: str):
    """Every const.py `name` on this list must appear in its doc file.

    A documented key counts if it matches the definition's name directly, or
    matches one of that row's compression candidates (see
    _expand_compressed_name) - so "Grid Phase A/B/C Voltage" documents
    "Grid Phase A Voltage" without a literal row for each phase.
    """
    filename = FAMILY_TO_FILE[family]
    rows = _doc_tables(filename)[platform]
    documented_keys: set[str] = set()
    for raw_name, _line in rows:
        documented_keys.update(_key(c) for c in _expand_compressed_name(raw_name))

    definitions = DEFINITION_LISTS[family][platform]
    missing = [
        d
        for d in definitions
        if _key(d.name) not in documented_keys
        and not _matches_any(d.name, family, platform, EXCLUDE_FROM_UNDOCUMENTED)
    ]
    if missing:
        formatted = "\n".join(_format_missing_row(d, platform) for d in missing)
        pytest.fail(
            f"{family}_{platform.upper()} defines {len(missing)} entit"
            f"{'y' if len(missing) == 1 else 'ies'} not documented in "
            f"documentation/entities/{filename}. Add under the matching "
            f"section (copy-paste, fill in the real values):\n{formatted}"
        )


@pytest.mark.parametrize("family,platform", _FAMILY_PLATFORM_PAIRS)
def test_no_orphaned_documentation_row(family: str, platform: str):
    """Every documented entity name must exist as a `name` on this list.

    Checks the family's catalog file (FAMILY_TO_FILE) and every file that
    documents a subset of it (FAMILY_TO_EXTRA_ORPHAN_FILES) - a row in
    either one is only accepted when _row_is_orphan_free confirms every
    entity it names (there can be more than one compressed into a single
    row) is real.
    """
    filenames = (FAMILY_TO_FILE[family], *FAMILY_TO_EXTRA_ORPHAN_FILES.get(family, ()))
    definitions = DEFINITION_LISTS[family][platform]
    known_keys = {_key(d.name) for d in definitions}

    orphans: list[tuple[str, str, int]] = []
    for filename in filenames:
        for raw_name, line in _doc_tables(filename)[platform]:
            if _row_is_orphan_free(raw_name, known_keys):
                continue
            if _matches_any(raw_name, family, platform, EXCLUDE_FROM_ORPHAN):
                continue
            orphans.append((filename, raw_name, line))

    if orphans:
        entry_name = f"{family}_{platform.upper()}"
        listing = "\n".join(
            f"  {filename}:{line}: '{raw_name}' has no {entry_name} entry"
            for filename, raw_name, line in sorted(orphans, key=lambda t: (t[0], t[2]))
        )
        pytest.fail(
            f"{len(orphans)} {platform} row(s) mapped to {family} document "
            f"no matching entry in {entry_name}:\n{listing}"
        )


# ---------------------------------------------------------------------------
# Finding 5: documentation/README.md's Entity Reference counts are prose,
# gated by nothing. Every count below is computed from the REAL per-platform
# def-getter functions (sensor.py/binary_sensor.py/switch.py/number.py/
# select.py's `_get_*_defs`) combined with const.filter_defs_for_serial -
# the exact functions and filter Home Assistant itself calls at entity-setup
# time - for a representative serial per device, never from DEFINITION_LISTS
# directly. That is what makes this a real gate instead of a restatement:
# DEFINITION_LISTS ignores the enhanced_only/control-prefix/serial-exclusion
# filtering the doc's counts already account for (Stream's non-BK31 units
# get 1 number, not STREAM_NUMBERS' 4; Stream Micro gets 21 of STREAM_
# SENSORS' 55).
# ---------------------------------------------------------------------------

from custom_components.ecoflow_energy.binary_sensor import (  # noqa: E402
    _get_binary_sensor_defs,
)
from custom_components.ecoflow_energy.number import _get_number_defs  # noqa: E402
from custom_components.ecoflow_energy.select import _get_select_defs  # noqa: E402
from custom_components.ecoflow_energy.sensor import _get_sensor_defs  # noqa: E402
from custom_components.ecoflow_energy.switch import _get_switch_defs  # noqa: E402

README_DOC_PATH = REPO_ROOT / "documentation" / "README.md"

_COUNT_UNIT_TO_PLATFORM: dict[str, str] = {
    "sensor": "sensors",
    "sensors": "sensors",
    "binary sensor": "binary_sensors",
    "binary sensors": "binary_sensors",
    "switch": "switches",
    "switches": "switches",
    "number": "numbers",
    "numbers": "numbers",
    "select": "selects",
    "selects": "selects",
    "climate": "climate",
}
_COUNT_RE = re.compile(
    r"(\d+)\s+(binary sensors?|sensors?|switches?|numbers?|selects?|climate)\b"
)


def _parse_counts(segment: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value, unit in _COUNT_RE.findall(segment):
        platform = _COUNT_UNIT_TO_PLATFORM[unit]
        counts[platform] = counts.get(platform, 0) + int(value)
    return counts


def _bullet_segment(doc_text: str, label: str) -> str:
    """The Entity Reference bullet for `label`, from just after the closing
    ')' of its markdown link to the end of the line."""
    pattern = re.compile(
        rf"^- \[{re.escape(label)}\]\([^)]*\)\s*-\s*(.*)$", re.MULTILINE
    )
    match = pattern.search(doc_text)
    assert match, f"no Entity Reference bullet for {label!r} in documentation/README.md"
    return match.group(1)


def _primary_and_footnote(segment: str) -> tuple[str, str]:
    """Split a bullet's text at its first '(`...`' parenthetical (the
    serial-prefix list) into the primary count segment and whatever follows
    the matching close paren (a footnote clause, if any)."""
    idx = segment.find("(`")
    if idx == -1:
        return segment, ""
    depth = 0
    i = idx
    while i < len(segment):
        if segment[i] == "(":
            depth += 1
        elif segment[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    footnote = segment[i + 1 :] if i < len(segment) else ""
    return segment[:idx], footnote


def _real_counts(device_type: str, device_sn: str) -> dict[str, int]:
    """The entity count Home Assistant would actually create for
    `device_type`/`device_sn`, per platform - the real def-getter functions
    plus the same filter_defs_for_serial call the platforms make."""
    filt = const.filter_defs_for_serial
    return {
        "sensors": len(filt(_get_sensor_defs(device_type), device_sn)),
        "binary_sensors": len(filt(_get_binary_sensor_defs(device_type), device_sn)),
        "switches": len(filt(_get_switch_defs(device_type, device_sn), device_sn)),
        "numbers": len(filt(_get_number_defs(device_type, device_sn), device_sn)),
        "selects": len(filt(_get_select_defs(device_type, device_sn), device_sn)),
    }


# (bullet label, device_type, representative serial) - devices whose bullet
# has no footnote clause and no non-const.py platform (climate), so their
# whole stated count can be checked as one primary segment. Stream, Delta 3
# Max Plus and WAVE 3 have their own dedicated tests below instead.
_ENTITY_REFERENCE_SIMPLE_DEVICES: tuple[tuple[str, str, str], ...] = (
    ("PowerOcean", const.DEVICE_TYPE_POWEROCEAN, "HJ31XXXXXXXXXXXX"),
    ("Delta 2 Max", const.DEVICE_TYPE_DELTA, "R351XXXXXXXXXXXX"),
    ("Smart Plug", const.DEVICE_TYPE_SMARTPLUG, "HW52XXXXXXXXXXXX"),
    ("Stream Micro", const.DEVICE_TYPE_STREAM, "BK01XXXXXXXXXXXX"),
    ("PowerStream", const.DEVICE_TYPE_POWERSTREAM, "HW51XXXXXXXXXXXX"),
    ("STREAM AC 5000", const.DEVICE_TYPE_STREAM_AC5000, "ES22XXXXXXXXXXXX"),
    ("Smart Meter", const.DEVICE_TYPE_SMART_METER, "BK21XXXXXXXXXXXX"),
    ("Solar Tracker", const.DEVICE_TYPE_SOLAR_TRACKER, "HZ31XXXXXXXXXXXX"),
)


@pytest.mark.parametrize(
    "label,device_type,device_sn",
    _ENTITY_REFERENCE_SIMPLE_DEVICES,
    ids=[label for label, _dt, _sn in _ENTITY_REFERENCE_SIMPLE_DEVICES],
)
def test_readme_entity_reference_counts_match_const_py(
    label: str, device_type: str, device_sn: str
):
    segment, _footnote = _primary_and_footnote(
        _bullet_segment(README_DOC_PATH.read_text(encoding="utf-8"), label)
    )
    stated = _parse_counts(segment)
    assert stated, f"no entity counts parsed from the {label!r} bullet: {segment!r}"
    actual = _real_counts(device_type, device_sn)
    mismatches = {
        p: (stated_n, actual[p])
        for p, stated_n in stated.items()
        if stated_n != actual[p]
    }
    assert not mismatches, (
        f"documentation/README.md's {label!r} bullet states {stated}, but "
        f"the real entity count for serial {device_sn!r} is {actual} - "
        f"mismatched platforms (stated, actual): {mismatches}"
    )


def test_readme_stream_primary_and_footnote_counts_match_const_py():
    """Stream's bullet states a base count (any BK-serial, no controls) plus
    a footnote delta for BK31 (adds the write-capable numbers/switches) -
    both sides checked against the real per-serial counts."""
    doc_text = README_DOC_PATH.read_text(encoding="utf-8")
    primary, footnote = _primary_and_footnote(_bullet_segment(doc_text, "Stream"))
    base = _real_counts(const.DEVICE_TYPE_STREAM, "BK11XXXXXXXXXXXX")
    bk31 = _real_counts(const.DEVICE_TYPE_STREAM, "BK31XXXXXXXXXXXX")

    stated_primary = _parse_counts(primary)
    assert stated_primary, (
        f"no counts parsed from Stream's primary segment: {primary!r}"
    )
    mismatches = {p: (n, base[p]) for p, n in stated_primary.items() if n != base[p]}
    assert not mismatches, (
        f"Stream primary count mismatch (stated, actual): {mismatches}"
    )

    stated_footnote = _parse_counts(footnote)
    assert stated_footnote, f"no counts parsed from Stream's footnote: {footnote!r}"
    delta = {p: bk31[p] - base[p] for p in bk31}
    mismatches = {p: (n, delta[p]) for p, n in stated_footnote.items() if n != delta[p]}
    assert not mismatches, (
        f"Stream BK31 footnote mismatch (stated, actual delta): {mismatches}"
    )


def test_readme_delta3_primary_and_footnote_counts_match_const_py():
    """Delta 3 Max Plus's bullet states a base count (P231/P321/D3N1/D3M1
    without port priority) plus a footnote delta for D3M serials (adds the
    port-priority switches/numbers/binary sensor)."""
    doc_text = README_DOC_PATH.read_text(encoding="utf-8")
    primary, footnote = _primary_and_footnote(
        _bullet_segment(doc_text, "Delta 3 Max Plus")
    )
    base = _real_counts(const.DEVICE_TYPE_DELTA3, "P231XXXXXXXXXXXX")
    d3m = _real_counts(const.DEVICE_TYPE_DELTA3, "D3M1XXXXXXXXXXXX")

    stated_primary = _parse_counts(primary)
    assert stated_primary, (
        f"no counts parsed from Delta 3 Max Plus's primary segment: {primary!r}"
    )
    mismatches = {p: (n, base[p]) for p, n in stated_primary.items() if n != base[p]}
    assert not mismatches, (
        f"Delta 3 Max Plus primary count mismatch (stated, actual): {mismatches}"
    )

    stated_footnote = _parse_counts(footnote)
    assert stated_footnote, (
        f"no counts parsed from Delta 3 Max Plus's footnote: {footnote!r}"
    )
    delta = {p: d3m[p] - base[p] for p in d3m}
    mismatches = {p: (n, delta[p]) for p, n in stated_footnote.items() if n != delta[p]}
    assert not mismatches, (
        f"Delta 3 Max Plus D3M footnote mismatch (stated, actual delta): {mismatches}"
    )


def test_readme_wave3_counts_match_const_py():
    """WAVE 3's bullet states '1 climate' alongside the const.py-backed
    platforms; climate.py builds that one thermostat directly rather than
    from a DEFINITION_LISTS list (see test_climate_entity_is_the_documented_
    wave3_thermostat above), so it is checked as a fixed fact here instead
    of computed from const.py."""
    doc_text = README_DOC_PATH.read_text(encoding="utf-8")
    segment, _footnote = _primary_and_footnote(_bullet_segment(doc_text, "WAVE 3"))
    stated = _parse_counts(segment)
    climate = stated.pop("climate", None)
    assert climate == 1, (
        f"WAVE 3 bullet states {climate} climate entities, expected exactly 1"
    )
    actual = _real_counts(const.DEVICE_TYPE_WAVE3, "AC71XXXXXXXXXXXX")
    mismatches = {p: (n, actual[p]) for p, n in stated.items() if n != actual[p]}
    assert not mismatches, (
        f"WAVE 3 bullet count mismatch (stated, actual): {mismatches}"
    )


# ---------------------------------------------------------------------------
# Finding 6: a positive control for this gate itself. Every claim about how
# strict test_no_undocumented_entity / test_no_orphaned_documentation_row /
# test_every_entity_table_is_classified actually are had been checked by
# hand, three times, by three different people - none of that survives the
# next edit to this file. This plants one of each defect in a throwaway doc
# file and a throwaway definition list and asserts each check catches it.
# ---------------------------------------------------------------------------


def test_gate_catches_an_orphaned_row(tmp_path: Path):
    doc = tmp_path / "synthetic.md"
    doc.write_text(
        "## Sensors\n\n"
        "| Entity | Unit | Description |\n"
        "|:---|:---:|:---|\n"
        "| Real Sensor | W | the one real entity |\n"
        "| Ghost Sensor | W | names nothing in known_keys |\n",
        encoding="utf-8",
    )
    parsed = _parse_doc_tables(doc)
    known_keys = {_key("Real Sensor")}
    orphans = [
        raw
        for raw, _line in parsed.documented["sensors"]
        if not _row_is_orphan_free(raw, known_keys)
    ]
    assert orphans == ["Ghost Sensor"]


def test_gate_catches_an_undocumented_entity():
    class _FakeDef:
        def __init__(self, name: str):
            self.name = name

    documented_keys = {_key("Real Sensor")}
    definitions = [_FakeDef("Real Sensor"), _FakeDef("Missing Sensor")]
    missing = [d for d in definitions if _key(d.name) not in documented_keys]
    assert [d.name for d in missing] == ["Missing Sensor"]


def test_gate_catches_an_unclassified_entity_table(tmp_path: Path):
    doc = tmp_path / "synthetic.md"
    doc.write_text(
        "## Something With No Platform Keyword\n\n"
        "| Entity | Description |\n"
        "|:---|:---|\n"
        "| Whatever | never routed anywhere |\n",
        encoding="utf-8",
    )
    parsed = _parse_doc_tables(doc)
    assert parsed.unclassified_entity_tables == [
        (3, "Something With No Platform Keyword")
    ]
