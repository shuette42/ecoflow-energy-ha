"""Top-level tests run without Home Assistant, except the ones listed here.

``test_layer_boundary.py`` proves that no top-level test imports
``homeassistant`` directly. That is not enough: a test can reach Home Assistant
through the root ``const.py`` (which imports ``Platform``), through the long
import name (which runs the package ``__init__``), or by an import inside a
function. This test runs the top-level suite in a subprocess in which every
``homeassistant`` import raises, and demands that everything outside
``HA_DEPENDENT`` passes.

``HA_DEPENDENT`` is a shrink-only list of the files that still need Home
Assistant. Every entry must still fail under the blocker, so a file that stops
needing it has to leave the list.

The subprocess loads only what the core tests need: the blocker, the asyncio
plugin and the socket plugin (the Home Assistant test package that normally
brings the last two is the thing being kept out).

The checker checks: the blocker must stop a file that needs Home Assistant, the
clean run must execute a plausible number of tests, and the list is verified
against a real run, not against a reading of the sources.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"
PASSED_FLOOR = 1500

BLOCKER = """\
import sys
from importlib.abc import MetaPathFinder


class _BlockHA(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "homeassistant" or fullname.startswith("homeassistant."):
            raise ImportError(f"blocked for the independence test: {fullname}")
        return None


for _name in [m for m in sys.modules if m.split(".")[0] == "homeassistant"]:
    del sys.modules[_name]
sys.meta_path.insert(0, _BlockHA())
"""

# Top-level test files that still need Home Assistant, at collection time or
# while running. Most reach it through the root const.py (its one Home Assistant
# import is `Platform`); the rest through the long import name or a platform
# module. Moving the platforms into __init__.py frees the first group.
HA_DEPENDENT = frozenset(
    {
        "test_const.py",
        "test_coordinator_mro.py",
        "test_delta2max_mppt_replay.py",
        "test_delta3_commands.py",
        "test_delta3_idle_shutdown.py",
        "test_delta3_port_priority.py",
        "test_delta3_screen_timeout.py",
        "test_entity_documentation.py",
        "test_entity_translations.py",
        "test_frame_capture.py",
        "test_powerglow_commands.py",
        "test_powerocean_bundled_frames.py",
        "test_powerocean_parser.py",
        "test_powerpulse_c371.py",
        "test_powerstream_parser.py",
        "test_prefix_and_powerpulse2_documentation.py",
        "test_river2max_r611_routing.py",
        "test_river3_r655_routing.py",
        "test_stream_5000.py",
        "test_stream_ac5000_commands.py",
        "test_stream_commands.py",
        "test_stream_http_parser.py",
        "test_translations.py",
    }
)

_RESULT = re.compile(r"^(?:FAILED|ERROR) tests/(test_\w+\.py)", re.MULTILINE)
_PASSED = re.compile(r"(\d+) passed")


def _run_blocked(tmp_path: Path, names: list[str]) -> tuple[int, str]:
    (tmp_path / "block_ha.py").write_text(BLOCKER, encoding="utf-8")
    # A developer's PYTEST_ADDOPTS or PYTEST_PLUGINS would reach the child and
    # change what it loads, so the child starts without them.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
    }
    env["PYTHONPATH"] = str(tmp_path)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "block_ha",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "pytest_socket",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "-o",
        "asyncio_mode=auto",
        "-q",
        "-rfE",
        "--continue-on-collection-errors",
        *[f"tests/{name}" for name in names],
    ]
    done = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    return done.returncode, done.stdout + done.stderr


def _top_level_names() -> list[str]:
    own = Path(__file__).name
    return sorted(
        p.name for p in TESTS_DIR.glob("test_*.py") if p.is_file() and p.name != own
    )


def test_the_blocker_stops_a_file_that_needs_home_assistant(tmp_path: Path) -> None:
    """Positive control: a known dependent file must fail under the blocker."""
    code, out = _run_blocked(tmp_path, ["test_const.py"])
    assert code != 0, out[-800:]
    assert "blocked for the independence test" in out


def test_top_level_tests_outside_the_list_run_without_home_assistant(
    tmp_path: Path,
) -> None:
    names = [n for n in _top_level_names() if n not in HA_DEPENDENT]
    assert len(names) >= 40, len(names)
    code, out = _run_blocked(tmp_path, names)
    passed = _PASSED.search(out)
    assert code == 0, out[-3000:]
    assert passed and int(passed.group(1)) >= PASSED_FLOOR, out[-400:]


def test_the_dependent_list_only_shrinks(tmp_path: Path) -> None:
    present = set(_top_level_names())
    gone = sorted(HA_DEPENDENT - present)
    assert gone == [], f"listed file is gone, drop it: {gone}"
    _, out = _run_blocked(tmp_path, sorted(HA_DEPENDENT))
    failing = set(_RESULT.findall(out))
    freed = sorted(HA_DEPENDENT - failing)
    assert freed == [], f"no longer needs Home Assistant, drop it: {freed}"


@pytest.mark.parametrize(
    "line", ["FAILED tests/test_const.py::t - x", "ERROR tests/test_const.py"]
)
def test_result_lines_are_parsed(line: str) -> None:
    assert _RESULT.findall(line) == ["test_const.py"]
