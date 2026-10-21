"""The two layers keep their boundary.

``custom_components/ecoflow_energy/ecoflow/`` is the core library: it talks to
the cloud and decodes frames, and it must stay importable and testable without
Home Assistant. The platforms and coordinators above it are the only code that
may import ``homeassistant``. The same split holds in the test tree: the
top-level ``tests/*.py`` files run without Home Assistant installed, and
everything that needs it lives under ``tests/ha/``.

Two checks, both AST walks so that a function-local import counts and a
docstring that merely mentions the package does not:

1. No file under ``ecoflow/`` imports ``homeassistant``.
2. No top-level ``tests/*.py`` file imports ``homeassistant``, except an
   explicit allowlist. A stale allowlist entry (file gone, or no longer
   importing Home Assistant) fails too, so the list can only shrink.

Each check has a positive control (a sample source it must flag) and a floor
on the number of files walked, so an empty glob cannot pass as a clean tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "custom_components" / "ecoflow_energy" / "ecoflow"
TESTS_DIR = REPO_ROOT / "tests"

# Top-level test files that still import Home Assistant. Moving one under
# tests/ha/ removes its entry; adding an entry is a decision, not a drift.
HA_TEST_ALLOWLIST = frozenset({"test_entity_definitions.py"})

# Counted at write time: 46 core files and 74 top-level test files.
CORE_FILE_FLOOR = 40
TOP_LEVEL_TEST_FLOOR = 50


def _imports_home_assistant(source: str) -> list[str]:
    """Return every ``homeassistant`` module the source imports, at any depth."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            hits.extend(
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] == "homeassistant"
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] == "homeassistant"
        ):
            hits.append(node.module)
    return hits


def _core_files() -> list[Path]:
    return sorted(
        path
        for path in CORE_DIR.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _top_level_test_files() -> list[Path]:
    return sorted(path for path in TESTS_DIR.glob("*.py") if path.is_file())


def _offenders(files: list[Path]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in files:
        hits = _imports_home_assistant(path.read_text(encoding="utf-8"))
        if hits:
            found[str(path.relative_to(REPO_ROOT))] = hits
    return found


def test_the_import_checker_flags_every_import_form() -> None:
    """Positive control: each form must be seen, and plain text must not be."""
    flagged = [
        "from homeassistant.const import Platform\n",
        "import homeassistant\n",
        "import homeassistant.core as core\n",
        "def late():\n    import homeassistant\n",
        "def late():\n    from homeassistant.core import HomeAssistant\n",
    ]
    for source in flagged:
        assert _imports_home_assistant(source), source
    quiet = [
        '"""Mentions homeassistant in prose only."""\n',
        "from custom_components.ecoflow_energy import const\n",
        "import homeassistant_stub\n",
        "from . import homeassistant\n",
    ]
    for source in quiet:
        assert _imports_home_assistant(source) == [], source


def test_core_library_imports_no_home_assistant() -> None:
    files = _core_files()
    assert len(files) >= CORE_FILE_FLOOR, len(files)
    assert _offenders(files) == {}


def test_top_level_tests_do_not_need_home_assistant() -> None:
    files = _top_level_test_files()
    assert len(files) >= TOP_LEVEL_TEST_FLOOR, len(files)
    offenders = _offenders(files)
    unexpected = {
        name: hits
        for name, hits in offenders.items()
        if Path(name).name not in HA_TEST_ALLOWLIST
    }
    assert unexpected == {}, "move these under tests/ha/ or fix the import"

    for name in sorted(HA_TEST_ALLOWLIST):
        path = TESTS_DIR / name
        assert path.is_file(), f"allowlisted file is gone, drop it: {name}"
        assert str(path.relative_to(REPO_ROOT)) in offenders, (
            f"allowlisted file no longer imports Home Assistant, drop it: {name}"
        )
