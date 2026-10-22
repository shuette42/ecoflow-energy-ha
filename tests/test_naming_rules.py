"""The naming rules that a file listing can check.

``documentation/contributing/naming.md`` says how modules and builders are
named. These checks hold the parts a directory listing and the function names
can decide, so a new file cannot slip past the page:

1. A module name exists in both the integration root and ``ecoflow/`` only for
   the three pairs that were there before the rules (``__init__``, ``const``,
   ``charging_history``). A new pair is a naming collision.
2. Every ``build_*`` function in an ``ecoflow/*_commands.py`` file ends in
   ``_payload`` (it returns bytes) or ``_command`` (it returns a JSON dict).
3. A parser file is ``<family>_proto.py``, ``<family>_http.py`` or
   ``<family>_modbus.py``, except the two older Standard Mode parsers named in
   ``UNSUFFIXED_PARSERS``.

The checker checks: each rule has a sample it must flag and one it must leave
alone, and each scan has a floor, so an empty glob cannot pass as clean.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROOT_DIR = REPO_ROOT / "custom_components" / "ecoflow_energy"
CORE_DIR = ROOT_DIR / "ecoflow"
PARSER_DIR = CORE_DIR / "parsers"

SHARED_STEMS = frozenset({"__init__", "const", "charging_history"})
UNSUFFIXED_PARSERS = frozenset({"delta", "smartplug"})
BUILDER_SUFFIXES = ("_payload", "_command")
PARSER_NAME = re.compile(r"^[a-z0-9_]+_(proto|http|modbus)$")


def _stems(directory: Path) -> set[str]:
    return {p.stem for p in directory.glob("*.py")}


def _bad_builders(source: str) -> list[str]:
    return [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("build_")
        and not node.name.endswith(BUILDER_SUFFIXES)
    ]


def _bad_parser_stems(stems: set[str]) -> list[str]:
    return sorted(
        s
        for s in stems
        if s != "__init__" and s not in UNSUFFIXED_PARSERS and not PARSER_NAME.match(s)
    )


def test_the_builder_checker_flags_and_spares() -> None:
    assert _bad_builders("def build_write(x):\n    pass\n") == ["build_write"]
    assert _bad_builders("async def build_set(x):\n    pass\n") == ["build_set"]
    assert _bad_builders("def build_write_payload(x):\n    pass\n") == []
    assert _bad_builders("def build_toggle_command(x):\n    pass\n") == []
    assert _bad_builders("def helper_write(x):\n    pass\n") == []


def test_the_parser_checker_flags_and_spares() -> None:
    assert _bad_parser_stems({"powerocean", "hr61"}) == ["hr61", "powerocean"]
    assert _bad_parser_stems({"stream_proto", "powerocean_http", "x_modbus"}) == []
    assert _bad_parser_stems({"delta", "smartplug", "__init__"}) == []


def test_only_the_three_known_module_names_exist_in_both_layers() -> None:
    root, core = _stems(ROOT_DIR), _stems(CORE_DIR)
    assert len(root) >= 15 and len(core) >= 15, (len(root), len(core))
    assert root & core == SHARED_STEMS


def test_command_builders_name_their_return_kind() -> None:
    files = sorted(CORE_DIR.glob("*_commands.py"))
    assert len(files) >= 4, files
    offenders = {
        f.name: bad
        for f in files
        if (bad := _bad_builders(f.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_parser_files_carry_a_transport_suffix() -> None:
    stems = _stems(PARSER_DIR)
    assert len(stems) >= 15, len(stems)
    assert _bad_parser_stems(stems) == []


@pytest.mark.parametrize("name", sorted(UNSUFFIXED_PARSERS))
def test_the_unsuffixed_parsers_still_exist(name: str) -> None:
    assert (PARSER_DIR / f"{name}.py").is_file(), f"drop {name} from the list"
