"""One import name per test file, and the right one for its folder.

The integration can be imported under two names, and each name loads its own
module objects. A patch on one name misses code that reads the other, and a
Home Assistant platform module loaded under the wrong name registers a second
copy of itself, which broke the config flow tests when it happened (a
top-level translation test imported ``ecoflow_energy.config_flow_setup``).

The rules, over every import and every dotted string (patch targets included):

1. No file uses both names.
2. Under ``tests/ha/`` the name is ``custom_components.ecoflow_energy``.
3. At the top level the bare name ``ecoflow_energy`` may only reach the core
   library (``ecoflow_energy.ecoflow.*``) and the constants module
   (``ecoflow_energy.const``), never a Home Assistant module.
4. A top-level file that needs a Home Assistant module and therefore uses the
   long name is listed in ``LONG_NAME_ALLOWLIST``. The list can only shrink: a
   stale entry fails, and a new one is a decision, not a drift. The proper
   home of such a test is ``tests/ha/``.

The checker checks: every rule has a sample it must flag and a sample it must
leave alone, and the scan has a floor, so an empty glob cannot pass as clean.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
HA_DIR = TESTS_DIR / "ha"

LONG = "custom_components.ecoflow_energy"
BARE = "ecoflow_energy"
BARE_CORE = f"{BARE}.ecoflow"
BARE_CONST = f"{BARE}.const"
DOTTED = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$")

# Files that keep samples of both names on purpose, to test a checker.
SAMPLE_FILES = frozenset(
    {"test_layer_boundary.py", "test_import_names.py", "test_ha_independence.py"}
)

# Top-level files that need a Home Assistant module and import it under the
# long name. Moving one under tests/ha/ (or making it core-only) removes its
# entry.
LONG_NAME_ALLOWLIST = frozenset(
    {
        "test_coordinator_mro.py",
        "test_delta2max_mppt_replay.py",
        "test_delta3_commands.py",
        "test_delta3_port_priority.py",
        "test_entity_documentation.py",
        "test_powerglow_commands.py",
        "test_powerpulse_c371.py",
        "test_prefix_and_powerpulse2_documentation.py",
        "test_river2max_r611_routing.py",
        "test_river3_r655_routing.py",
        "test_stream_5000.py",
        "test_translations.py",
    }
)

FILE_FLOOR = 100


def _dotted_names(source: str) -> list[str]:
    """Every module name the source imports or names in a dotted string."""
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
            if node.module in (LONG, BARE):
                names.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and DOTTED.match(node.value)
            and "." in node.value
        ):
            names.append(node.value)
    return names


def _is(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}.")


def _uses(names: list[str]) -> tuple[bool, bool]:
    return (
        any(_is(n, LONG) for n in names),
        any(_is(n, BARE) for n in names),
    )


def _problems(source: str, in_ha_dir: bool) -> list[str]:
    names = _dotted_names(source)
    long_used, bare_used = _uses(names)
    found: list[str] = []
    if long_used and bare_used:
        found.append("uses both import names")
    if in_ha_dir and bare_used:
        found.append("tests/ha/ must import custom_components.ecoflow_energy")
    if not in_ha_dir and bare_used:
        found.extend(
            f"{n}: the bare name may only reach the core library"
            for n in sorted(set(names))
            if _is(n, BARE)
            and n != BARE
            and not _is(n, BARE_CORE)
            and not _is(n, BARE_CONST)
        )
    return found


def _test_files() -> list[Path]:
    return sorted(
        path
        for path in [*TESTS_DIR.glob("*.py"), *HA_DIR.glob("*.py")]
        if path.name not in SAMPLE_FILES and path.name != "conftest.py"
    )


@pytest.mark.parametrize(
    ("source", "in_ha_dir"),
    [
        ("from ecoflow_energy.ecoflow import const\n", True),
        (
            "import custom_components.ecoflow_energy.x\n"
            "import ecoflow_energy.ecoflow.y\n",
            False,
        ),
        ("from ecoflow_energy.config_flow_setup import X\n", False),
        ('patch("ecoflow_energy.coordinator.core.time")\n', False),
        ("from ecoflow_energy import sensor\n", False),
        ("from ecoflow_energy.number import X\n", False),
    ],
)
def test_checker_flags_every_wrong_form(source: str, in_ha_dir: bool) -> None:
    assert _problems(source, in_ha_dir), source


@pytest.mark.parametrize(
    ("source", "in_ha_dir"),
    [
        ("from ecoflow_energy.ecoflow.parsers import stream_proto\n", False),
        ('patch("ecoflow_energy.ecoflow.cloud_mqtt.time")\n', False),
        ("from ecoflow_energy.const import DOMAIN\n", False),
        ("from custom_components.ecoflow_energy.sensor import X\n", True),
        ('"""Prose: use ecoflow_energy.sensor here."""\n', True),
        ("from custom_components.ecoflow_energy.sensor import X\n", False),
    ],
)
def test_checker_leaves_the_right_forms_alone(source: str, in_ha_dir: bool) -> None:
    assert _problems(source, in_ha_dir) == [], source


def test_every_test_file_keeps_to_the_rules() -> None:
    files = _test_files()
    assert len(files) >= FILE_FLOOR, len(files)
    offenders: dict[str, list[str]] = {}
    for path in files:
        in_ha = path.parent == HA_DIR
        problems = _problems(path.read_text(encoding="utf-8"), in_ha)
        if not in_ha and path.name in LONG_NAME_ALLOWLIST:
            # Allowed to use the long name, still never both names.
            problems = [p for p in problems if p == "uses both import names"]
        elif not in_ha and _uses(_dotted_names(path.read_text(encoding="utf-8")))[0]:
            problems.append(
                "top-level file uses the long name: import the core as "
                "ecoflow_energy.ecoflow or move it to tests/ha/"
            )
        if problems:
            offenders[str(path.relative_to(REPO_ROOT))] = problems
    assert offenders == {}


def test_the_long_name_allowlist_only_shrinks() -> None:
    for name in sorted(LONG_NAME_ALLOWLIST):
        path = TESTS_DIR / name
        assert path.is_file(), f"allowlisted file is gone, drop it: {name}"
        long_used, _ = _uses(_dotted_names(path.read_text(encoding="utf-8")))
        assert long_used, f"no longer uses the long name, drop it: {name}"
