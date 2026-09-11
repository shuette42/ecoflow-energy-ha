"""Entity definition reachability tests.

Guards the contract between the definition blocks in const.py and the
platform dispatchers that hand them to Home Assistant:

1. No orphaned definition block. Every module-level list in const.py named
   ``<FAMILY>_<PLATFORM>`` is returned by at least one dispatcher for at
   least one (device type, serial, platform) combination, and every single
   definition inside it is reachable for at least one serial.
2. No device type without any entity. Every ``DEVICE_TYPE_*`` constant
   yields at least one sensor. Sensors are the invariant rather than every
   platform: the Smart Meter measures and reports, it stores nothing and
   switches nothing, so it legitimately has no switches and no numbers
   (see the device type block in ``ecoflow/const.py``).

Not covered here, because it is covered elsewhere and duplicating it would
give one failure two owners: the translation-key contract lives in
``tests/test_entity_translations.py``, strings.json / en.json drift in
``tests/test_translations.py``.

A block counts as dispatched when a dispatcher returned the block object
itself, or returned a non-empty selection whose definitions are all members
of that block. The comparison is by definition-object identity rather than
by key, because several device families name the same reading with the same
key: a new block copied from an existing one would otherwise inherit that
key's reachability and read as covered while nothing dispatches it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy import const as C
from ecoflow_energy.binary_sensor import _get_binary_sensor_defs
from ecoflow_energy.ecoflow import const as EC
from ecoflow_energy.number import _get_number_defs
from ecoflow_energy.select import _get_select_defs
from ecoflow_energy.sensor import _get_sensor_defs
from ecoflow_energy.switch import _get_switch_defs

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components"
CONST_PY = COMPONENT_DIR / "ecoflow_energy" / "const.py"
BUTTON_PY = COMPONENT_DIR / "ecoflow_energy" / "button.py"

PLATFORM_SUFFIXES = (
    "SENSORS",
    "BINARY_SENSORS",
    "SWITCHES",
    "NUMBERS",
    "SELECTS",
    "BUTTONS",
)
DEF_LIST_NAME = re.compile(rf"[A-Z0-9]+_({'|'.join(PLATFORM_SUFFIXES)})")
# The same names as DEF_LIST_NAME would match, plus the ones that carry an
# extra underscore and are therefore invisible to it.
DEF_LIST_NAME_LOOSE = re.compile(
    rf"^([A-Z0-9_]+_(?:{'|'.join(PLATFORM_SUFFIXES)}))\s*[:=]", re.MULTILINE
)

# The device type that stands for "not classified". It reaches no parser and
# therefore no definition list; `_get_sensor_defs` returns [] for it.
UNCLASSIFIED_DEVICE_TYPE = EC.DEVICE_TYPE_UNKNOWN


def _button_defs(device_type: str, device_sn: str) -> list[Any]:
    """Mirror the button platform's device-type branch.

    ``button.py`` has no ``_get_button_defs`` helper - it selects the list
    inline in ``async_setup_entry`` (the PowerPulse 2 branch). This shim
    reproduces that one branch, and `test_button_platform_branch_is_the_only_one`
    below fails if the platform ever grows a second one, so the shim cannot
    drift away from it unnoticed.
    """
    if device_type == EC.DEVICE_TYPE_POWERPULSE2:
        return C.POWERPULSE2_BUTTONS
    return []


# Every dispatcher is called as (device_type, device_sn); the two that ignore
# the serial are wrapped so the call site stays uniform.
DISPATCHERS: dict[str, Callable[[str, str], list[Any]]] = {
    "SENSORS": lambda device_type, device_sn: _get_sensor_defs(device_type),
    "BINARY_SENSORS": lambda device_type, device_sn: _get_binary_sensor_defs(
        device_type
    ),
    "SWITCHES": _get_switch_defs,
    "NUMBERS": _get_number_defs,
    "SELECTS": _get_select_defs,
    "BUTTONS": _button_defs,
}

# Dummy serials: a four-character prefix and filler, never a real serial.
_FILLER = "0000000000000000"
# The serial classes the dispatchers actually consult, plus the two cases in
# no table at all: no serial (both control gates close on it) and a prefix on
# no allowlist.
SERIALS: tuple[str, ...] = ("", "ZZZZ" + _FILLER) + tuple(
    prefix + _FILLER
    for prefix in sorted(C.STREAM_AC5000_CONTROL_PREFIXES | C.STREAM_CONTROL_PREFIXES)
)

DEVICE_TYPES: dict[str, str] = {
    name: getattr(EC, name)
    for name in dir(EC)
    if name.startswith("DEVICE_TYPE_") and isinstance(getattr(EC, name), str)
}


def _definition_blocks() -> dict[str, list[Any]]:
    """Return every module-level definition list in const.py, by name."""
    return {
        name: getattr(C, name)
        for name in dir(C)
        if DEF_LIST_NAME.fullmatch(name) and isinstance(getattr(C, name), list)
    }


def _dispatcher_outputs() -> list[list[Any]]:
    """Every list any dispatcher returns, over the full combination space."""
    outputs: list[list[Any]] = []
    for device_type in sorted(set(DEVICE_TYPES.values())):
        for device_sn in SERIALS:
            for dispatch in DISPATCHERS.values():
                outputs.append(dispatch(device_type, device_sn))
    return outputs


BLOCKS = _definition_blocks()
OUTPUTS = _dispatcher_outputs()
REACHED_DEF_IDS = {id(definition) for output in OUTPUTS for definition in output}


def _is_dispatched(block: list[Any]) -> bool:
    """Whether any dispatcher hands this block, or a selection of it, to HA."""
    member_ids = {id(definition) for definition in block}
    for output in OUTPUTS:
        if output is block:
            return True
        # `if output` first: the empty list every dispatcher returns for an
        # unknown device type is a subset of everything, and would otherwise
        # mark every block reachable.
        if output and member_ids.issuperset({id(item) for item in output}):
            return True
    return False


def test_definition_block_names_follow_the_convention() -> None:
    """A definition block carries one family token, then the platform.

    Discovery here and in `tests/test_entity_translations.py` walks
    `dir(const)` with `[A-Z0-9]+_<PLATFORM>`, which matches one family token
    and no more: `STREAMAC5000_SENSORS` is spelled without the underscore for
    exactly this reason. A block named `STREAM_AC5000_SENSORS` would be
    skipped by every one of those tests without failing any of them, so the
    naming itself is checked before anything is built on it.
    """
    offenders = sorted(
        name
        for name in DEF_LIST_NAME_LOOSE.findall(CONST_PY.read_text())
        if not DEF_LIST_NAME.fullmatch(name)
    )
    assert not offenders, (
        "Entity definition blocks whose name carries more than one family "
        f"token: {offenders}. Convention-based discovery cannot see them, so "
        "they are checked by no reachability and no translation test. Join "
        "the family into one token, as STREAMAC5000_SENSORS does."
    )


def test_source_declarations_match_runtime_discovery() -> None:
    """The reflection above sees every block const.py declares.

    Positive control for the two tests below: they walk `dir(const)`, and a
    block the walk cannot see is a block they silently never check.
    """
    declared = {
        name
        for name in DEF_LIST_NAME_LOOSE.findall(CONST_PY.read_text())
        if DEF_LIST_NAME.fullmatch(name)
    }
    assert declared == set(BLOCKS), (
        "const.py declares definition blocks the runtime walk does not see, or "
        f"the other way round. Only in source: {sorted(declared - set(BLOCKS))}. "
        f"Only in runtime: {sorted(set(BLOCKS) - declared)}"
    )


def test_every_platform_suffix_has_a_dispatcher() -> None:
    """No block family without a dispatcher to check it against."""
    suffixes = set()
    for name in BLOCKS:
        match = DEF_LIST_NAME.fullmatch(name)
        assert match is not None
        suffixes.add(match.group(1))
    missing = sorted(suffixes - set(DISPATCHERS))
    assert not missing, f"Definition blocks with no dispatcher in this test: {missing}"


def test_button_platform_branch_is_the_only_one() -> None:
    """`_button_defs` mirrors button.py, so button.py may name only that list."""
    named = set(re.findall(r"\b[A-Z0-9]+_BUTTONS\b", BUTTON_PY.read_text()))
    assert named == {"POWERPULSE2_BUTTONS"}, (
        "button.py selects button definitions inline and this test file mirrors "
        f"that branch in `_button_defs`. button.py now names {sorted(named)} - "
        "update the shim, or the new list is never checked for reachability."
    )


def test_no_orphaned_definition_block() -> None:
    """Every definition block reaches Home Assistant through some dispatcher."""
    orphans = [
        f"{name}: no dispatcher returns it for any of {len(DEVICE_TYPES)} "
        f"device types x {len(SERIALS)} serials"
        for name, block in sorted(BLOCKS.items())
        if not _is_dispatched(block)
    ]
    assert not orphans, "Orphaned entity definition blocks in const.py:\n" + "\n".join(
        orphans
    )


def test_no_unreachable_definition_inside_a_block() -> None:
    """Every single definition is reachable for at least one serial.

    A definition can be dispatched-but-unreachable: the Stream number gate
    returns a key subset for a serial outside STREAM_CONTROL_PREFIXES (see
    the Stream branch of `_get_number_defs`), so the block is compared
    definition by definition rather than as a whole.
    """
    unreachable: list[str] = []
    for name, block in sorted(BLOCKS.items()):
        missing = [
            definition.key
            for definition in block
            if id(definition) not in REACHED_DEF_IDS
        ]
        if missing:
            unreachable.append(f"{name}: {sorted(missing)}")
    assert not unreachable, "Entity definitions no dispatcher ever returns:\n" + (
        "\n".join(unreachable)
    )


@pytest.mark.parametrize(
    "constant",
    sorted(
        name
        for name, value in DEVICE_TYPES.items()
        if value != UNCLASSIFIED_DEVICE_TYPE
    ),
)
def test_every_device_type_has_sensors(constant: str) -> None:
    """Every classified device type produces at least one sensor."""
    device_type = DEVICE_TYPES[constant]
    defs = _get_sensor_defs(device_type)
    assert defs, (
        f"{constant} ({device_type!r}) yields no sensor definitions. A device "
        "type with no sensor produces a device entry with nothing in it - "
        "either wire it into _get_sensor_defs or remove the type."
    )
