"""The two layers keep their boundary.

``custom_components/ecoflow_energy/ecoflow/`` is the core library: it talks to
the cloud and decodes frames, and it must stay importable and testable without
Home Assistant. The platforms and coordinators above it are the only code that
may import ``homeassistant``.

Two checks, both AST walks so that a function-local import counts and a
docstring that merely mentions the package does not:

1. No file under ``ecoflow/`` imports ``homeassistant`` and none imports
   anything that leaves ``ecoflow/`` for the Home Assistant layer above it. The
   second half matters because the root ``const.py`` imports Home Assistant, so
   ``from ..const import X`` written in a core file would reach it without ever
   naming ``homeassistant``. Relative imports are resolved against the file's
   own package: ``from ..const import X`` inside ``ecoflow/parsers/`` stays in
   ``ecoflow/const.py`` and is fine, the same line directly in ``ecoflow/``
   points at the root package and is flagged. A string literal handed to
   ``import_module`` or ``__import__`` is checked the same way. A module name
   that is built at run time cannot be seen and is the one gap left.
2. No top-level ``tests/*.py`` file imports ``homeassistant`` directly, except
   an explicit allowlist. A stale allowlist entry (file gone, or no longer
   importing Home Assistant) fails too, so the list can only shrink.

Check 2 proves only DIRECT imports. A top-level test can still reach Home
Assistant through the root ``const.py`` (which imports ``Platform``) or through
the ``custom_components.`` import name (which runs the package ``__init__``).
``test_ha_independence.py`` covers that by running the top-level suite with
every Home Assistant import blocked, and ``test_import_names.py`` keeps each
file to one import name.

Each check has positive controls (sample sources it must flag), negative
controls (sample sources it must leave alone) and a floor on the number of
files walked, so an empty glob cannot pass as a clean tree.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "custom_components" / "ecoflow_energy" / "ecoflow"
TESTS_DIR = REPO_ROOT / "tests"

CORE_PACKAGE = "custom_components.ecoflow_energy.ecoflow"
# The two names under which the integration is imported. Everything below the
# core package is fine from inside the core library, everything else is not.
ROOT_PACKAGES = ("custom_components.ecoflow_energy", "ecoflow_energy")
CORE_PACKAGES = tuple(f"{root}.ecoflow" for root in ROOT_PACKAGES)
IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})

# Top-level test files that still import Home Assistant directly. The list is
# empty and stays empty: a test that needs Home Assistant goes under tests/ha/.
HA_TEST_ALLOWLIST: frozenset[str] = frozenset()

# Floors well below the current counts: an empty glob must not pass as clean.
CORE_FILE_FLOOR = 40
TOP_LEVEL_TEST_FLOOR = 50


def _dynamic_import_names(node: ast.AST) -> list[str]:
    """Return the literal module name handed to ``import_module`` or ``__import__``."""
    if not isinstance(node, ast.Call) or not node.args:
        return []
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    first = node.args[0]
    if (
        name in IMPORT_FUNCTIONS
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
    ):
        return [first.value]
    return []


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
        else:
            hits.extend(
                name
                for name in _dynamic_import_names(node)
                if name.split(".")[0] == "homeassistant"
            )
    return hits


def _is_inside_core(target: str) -> bool:
    return any(
        target == core or target.startswith(f"{core}.") for core in CORE_PACKAGES
    )


def _is_integration_name(target: str) -> bool:
    return any(
        target == root or target.startswith(f"{root}.") for root in ROOT_PACKAGES
    )


def _resolved_targets(node: ast.ImportFrom, package: str) -> list[str]:
    """Resolve a ``from`` import to the dotted names it points at.

    ``package`` is the dotted package the importing file lives in. For an
    import of names from a bare root package (``from ecoflow_energy import x``)
    each name is its own target, because ``x`` may be a submodule.
    """
    module = node.module or ""
    if node.level:
        parts = package.split(".")
        keep = len(parts) - (node.level - 1)
        if keep < 1:
            return ["<beyond the top-level package>"]
        base = ".".join(parts[:keep])
        module = f"{base}.{module}" if module else base
    if module in ROOT_PACKAGES or module == "custom_components" or not node.module:
        return [f"{module}.{alias.name}" for alias in node.names]
    return [module]


def _core_escapes(source: str, package: str) -> list[str]:
    """Return every import in a core file that leaves ``ecoflow/``."""
    hits: list[str] = []
    for node in ast.walk(ast.parse(source)):
        relative = isinstance(node, ast.ImportFrom) and node.level > 0
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            targets = _resolved_targets(node, package)
        else:
            targets = _dynamic_import_names(node)
        # A relative import that leaves ecoflow/ is always a way out. An absolute
        # one only counts when it names the integration (stdlib stays free).
        hits.extend(
            target
            for target in targets
            if not _is_inside_core(target)
            and (relative or _is_integration_name(target))
        )
    return hits


def _core_files() -> list[Path]:
    return sorted(
        path
        for path in CORE_DIR.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )


def _top_level_test_files() -> list[Path]:
    return sorted(path for path in TESTS_DIR.glob("*.py") if path.is_file())


def _package_of(path: Path) -> str:
    """Dotted package a file belongs to (an ``__init__.py`` is its own package)."""
    return ".".join(path.relative_to(REPO_ROOT).parts[:-1])


def _offenders(files: list[Path]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in files:
        hits = _imports_home_assistant(path.read_text(encoding="utf-8"))
        if hits:
            found[str(path.relative_to(REPO_ROOT))] = hits
    return found


def _core_offenders(files: list[Path]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in files:
        source = path.read_text(encoding="utf-8")
        hits = _imports_home_assistant(source) + _core_escapes(
            source, _package_of(path)
        )
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
        'import importlib\nimportlib.import_module("homeassistant.core")\n',
        'from importlib import import_module\nimport_module("homeassistant")\n',
        '__import__("homeassistant.const")\n',
    ]
    for source in flagged:
        assert _imports_home_assistant(source), source
    quiet = [
        '"""Mentions homeassistant in prose only."""\n',
        "from custom_components.ecoflow_energy import const\n",
        "import homeassistant_stub\n",
        "from . import homeassistant\n",
        'import importlib\nimportlib.import_module("json")\n',
        "name = 'homeassistant'\nimport_module(name)\n",
    ]
    for source in quiet:
        assert _imports_home_assistant(source) == [], source


def test_the_core_checker_flags_every_way_out_of_the_core_layer() -> None:
    """Positive control: each escape route to the layer above must be seen."""
    package = CORE_PACKAGE
    flagged = [
        # Resolves to the root const.py, which imports Home Assistant.
        "from ..const import PLATFORMS\n",
        "from .. import const\n",
        "def late():\n    from ..const import PLATFORMS\n",
        "from custom_components.ecoflow_energy.const import DOMAIN\n",
        "from custom_components.ecoflow_energy import const\n",
        "from ecoflow_energy.const import DOMAIN\n",
        "from ecoflow_energy import const\n",
        "import custom_components.ecoflow_energy.const\n",
        "import ecoflow_energy.coordinator\n",
        'import importlib\nimportlib.import_module("ecoflow_energy.const")\n',
        "from importlib import import_module\n"
        'import_module("custom_components.ecoflow_energy.const")\n',
        # Climbing past the top-level package cannot be a valid core import.
        "from .... import x\n",
    ]
    for source in flagged:
        assert _core_escapes(source, package), source
    # One level deeper, `..` is the core package itself, `...` is the root.
    assert _core_escapes("from ...const import DOMAIN\n", f"{package}.parsers")
    assert _core_escapes("from .. import const\n", package)


def test_the_core_checker_leaves_imports_inside_the_core_layer_alone() -> None:
    """Negative control: the patterns the real tree uses must stay quiet."""
    package = CORE_PACKAGE
    parsers = f"{CORE_PACKAGE}.parsers"
    proto = f"{CORE_PACKAGE}.proto"
    quiet_in_parsers = [
        # parsers/powerpulse_proto.py and parsers/powerocean_proto.py
        "from ..const import DEVICE_TYPE_POWEROCEAN\n",
        "def late():\n    from ..const import DEVICE_TYPE_POWEROCEAN\n",
        "from . import powerocean\n",
        "from .powerocean import parse\n",
        "from ..proto.runtime import decode\n",
    ]
    for source in quiet_in_parsers:
        assert _core_escapes(source, parsers) == [], source
    # proto/runtime.py reaches ecoflow/const.py the same way.
    assert _core_escapes("from ..const import ENC_TYPE\n", proto) == []
    quiet_in_core = [
        "from .const import API_HOST\n",
        "from . import cloud_http\n",
        "from .parsers import powerocean\n",
        "from custom_components.ecoflow_energy.ecoflow.const import API_HOST\n",
        "from ecoflow_energy.ecoflow import const\n",
        "from ecoflow_energy.ecoflow.parsers import powerocean\n",
        "import ecoflow_energy.ecoflow.const\n",
        "import json\nfrom pathlib import Path\n",
        'import importlib\nimportlib.import_module("json")\n',
    ]
    for source in quiet_in_core:
        assert _core_escapes(source, package) == [], source


def test_the_package_of_a_file_is_resolved_from_its_path() -> None:
    """The resolver feeds on this, so check it on the three depths in use."""
    assert _package_of(CORE_DIR / "const.py") == CORE_PACKAGE
    assert (
        _package_of(CORE_DIR / "parsers" / "powerocean.py") == f"{CORE_PACKAGE}.parsers"
    )
    assert (
        _package_of(CORE_DIR / "parsers" / "__init__.py") == f"{CORE_PACKAGE}.parsers"
    )


def test_core_library_imports_no_home_assistant() -> None:
    files = _core_files()
    assert len(files) >= CORE_FILE_FLOOR, len(files)
    assert _core_offenders(files) == {}


def test_top_level_tests_import_no_home_assistant_directly() -> None:
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
