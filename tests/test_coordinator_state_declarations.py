"""AST-only drift check for `coordinator/_typing.py` (ADR-022 decision 5).

`CoordinatorState` in `coordinator/_typing.py` is the single declaration
point the eight coordinator mixins type-check their borrowed `self.<x>`
reads against. mypy itself already catches three of the four ways that
declaration can drift from the real coordinator:

    a mixin reads a name nobody declares        -> mypy [attr-defined]
    a declaration disagrees with core.__init__  -> mypy [assignment]
    a declared method disagrees with its def     -> mypy [override]

The fourth direction is invisible to mypy: a declaration can outlive its
implementation. Delete an attribute from `core.__init__` (or a method from
its mixin) and leave the line in `_typing.py` standing, and mypy has nothing
left to disagree with - every read of the removed name still type-checks and
then raises `AttributeError` at runtime. This test closes that gap, and only
that gap: it does not duplicate what mypy already checks.

Two mechanisms count as "implemented" here, matched against how this
codebase actually assigns state:

- an attribute (`AnnAssign` in `CoordinatorState`, e.g. `device_sn: str`) is
  owned by whichever module assigns `self.<name>` inside `__init__`. This
  coordinator has exactly one `__init__`, `EcoFlowDeviceCoordinator.__init__`
  in `core.py`; every attribute in `CoordinatorState` is first assigned
  there. A sibling mixin later writing the same name from one of its own
  methods (`self._shutdown = True` in `setup.py`'s shutdown path) is normal
  operation on already-declared state, not a second declaration - which is
  why the scan is restricted to `__init__` bodies. A module-wide scan for any
  `self.<name> = ` reports three dozen of these as "implemented in two
  modules" and none of them are drift; restricting to `__init__` was checked
  against this file's own tree before being trusted (see the parse control
  below - a check earns trust only after it is run against a case whose
  answer is already known).
- a method or `@property` (`FunctionDef`/`AsyncFunctionDef` directly in a
  class body, e.g. `def _log_event(...): ...`) is owned by whichever module
  defines it at class level, decorator or not - a decorator never changes
  the AST node type.

Pure `ast` on both sides: no Home Assistant import, no coordinator
construction, no fixture. Runs in milliseconds and cannot be broken by a
mock.

Both controls below were run and recorded before this test was trusted:

- Positive (declaration outliving its implementation): removed
  `self._shutdown: bool = False` from `core.py.__init__` while its
  declaration stood in `_typing.py` -> failed, naming `_shutdown` with zero
  owners. Restored.
- Negative (a declaration nothing implements): added `_nonexistent_probe:
  str` to `CoordinatorState` -> failed, naming `_nonexistent_probe` with
  zero owners. Restored.
- Parse control (the floor, not the per-name assertion): stubbed
  `_declared_names` to return an empty dict -> the per-name loop does not
  run, `checked` stays 0, and the floor assertion fails instead of the test
  passing on an empty comparison. Restored.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_DIR = REPO_ROOT / "custom_components" / "ecoflow_energy" / "coordinator"
TYPING_FILE = COORDINATOR_DIR / "_typing.py"

# core.py plus the eight mixins ADR-022 composes EcoFlowDeviceCoordinator
# from (coordinator/core.py:101-111).
COORDINATOR_MODULES = (
    "core.py",
    "setup.py",
    "credentials.py",
    "keepalive.py",
    "mqtt_ingest.py",
    "state_apply.py",
    "set_commands.py",
    "http_poll.py",
    "availability.py",
)

# Measured 2026-09-08 against this branch's `_typing.py`: CoordinatorState
# declares 79 names (57 attribute annotations, 22 def/property stubs). The
# floor is set close to that count on purpose: a floor of 1 only proves the
# ast walk did not crash, it does not prove it
# walked the whole class body. An empty or truncated CoordinatorState would
# still clear a floor of 1 and would not clear this one. Left with a small
# margin below 79 so that ordinary future edits (a name renamed, one
# declaration removed as state is retired) do not require touching this
# constant on every change - only a wholesale drop would.
MIN_DECLARATIONS_CHECKED = 75


def _find_coordinator_state(tree: ast.Module) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CoordinatorState":
            return node
    raise AssertionError(
        "CoordinatorState class not found in coordinator/_typing.py"
    )


def _declared_names(typing_source: str) -> dict[str, str]:
    """Return {name: "attr"|"def"} for every name CoordinatorState declares."""
    tree = ast.parse(typing_source)
    class_node = _find_coordinator_state(tree)
    declared: dict[str, str] = {}
    for stmt in class_node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            declared[stmt.target.id] = "attr"
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            declared[stmt.name] = "def"
    return declared


def _record_self_attr_targets(target: ast.expr, out: set[str]) -> None:
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id == "self"
    ):
        out.add(target.attr)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _record_self_attr_targets(elt, out)
    elif isinstance(target, ast.Starred):
        _record_self_attr_targets(target.value, out)


def _self_attrs_assigned_in_init(module_tree: ast.Module) -> set[str]:
    """`self.<name> = ` assigned inside an `__init__` method body only.

    Deliberately not module-wide - see the module docstring for why a
    sibling mixin writing already-declared state from a non-`__init__`
    method must not count as a second owner.
    """
    names: set[str] = set()
    for node in ast.walk(module_tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "__init__"
        ):
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        _record_self_attr_targets(target, names)
                elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                    _record_self_attr_targets(stmt.target, names)
    return names


def _class_level_defs(module_tree: ast.Module) -> set[str]:
    """Every `def`/`@property` defined directly in a class body in this module."""
    names: set[str] = set()
    for node in ast.walk(module_tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(stmt.name)
    return names


def _module_implementations() -> dict[str, tuple[set[str], set[str]]]:
    """Map each coordinator module to (init-assigned attrs, class-level defs)."""
    implementations: dict[str, tuple[set[str], set[str]]] = {}
    for filename in COORDINATOR_MODULES:
        module_tree = ast.parse((COORDINATOR_DIR / filename).read_text())
        implementations[filename] = (
            _self_attrs_assigned_in_init(module_tree),
            _class_level_defs(module_tree),
        )
    return implementations


def test_every_coordinator_state_declaration_has_exactly_one_implementation():
    declared = _declared_names(TYPING_FILE.read_text())
    implementations = _module_implementations()

    checked = 0
    for name, kind in declared.items():
        owners = [
            module
            for module, (init_attrs, defs) in implementations.items()
            if (name in init_attrs) or (name in defs)
        ]
        assert len(owners) == 1, (
            f"CoordinatorState declares `{name}` ({kind}) but it is "
            f"implemented in {owners!r} coordinator modules, expected "
            "exactly one. Zero owners means the declaration has outlived "
            "its implementation - every read of it still type-checks and "
            "then raises AttributeError at runtime. More than one owner "
            "means two modules now disagree about who owns this state."
        )
        checked += 1

    assert checked >= MIN_DECLARATIONS_CHECKED, (
        f"only compared {checked} CoordinatorState declarations, expected "
        f"at least {MIN_DECLARATIONS_CHECKED} - the ast walk over "
        "CoordinatorState likely returned an empty or truncated class body "
        "rather than the real declarations, and this floor exists so that "
        "case fails instead of passing as a clean run of nothing"
    )
