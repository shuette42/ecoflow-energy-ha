"""No tracked public path carries an internal plan number.

A file name such as ``ac71_frames_plan046.json`` points at a planning document
nobody outside the project can read. The naming rules give fixtures a name by
what was recorded, so this test fails on any tracked path under the public
trees whose name contains ``plan`` followed by three digits.

The checker checks: the pattern must catch samples that look like the real
offenders, must leave alone names that merely contain the word, and the tracked
listing must hold a plausible number of files, so an empty listing cannot pass
as a clean one.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_TREES = ("tests", "documentation", "custom_components")
PLAN_NUMBER = re.compile(r"(?i)plan[-_]?\d{3}")
TRACKED_FLOOR = 200


def _tracked_public_paths() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", *PUBLIC_TREES],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout, the tracked listing is unavailable")
    return out.splitlines()


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/wave3/ac71_frames_plan046.json",
        "tests/fixtures/powerpulse/c376_frames_plan132.json",
        "documentation/guides/PLAN-214-notes.md",
        "tests/test_plan_213_layers.py",
    ],
)
def test_pattern_catches_a_plan_number_in_a_path(path: str) -> None:
    assert PLAN_NUMBER.search(path)


@pytest.mark.parametrize(
    "path",
    [
        "tests/fixtures/wave3/ac71_frames.json",
        "documentation/guides/floor-plan.md",
        "tests/test_plan_view.py",
        "custom_components/ecoflow_energy/ecoflow/parsers/delta3_proto.py",
    ],
)
def test_pattern_leaves_other_names_alone(path: str) -> None:
    assert not PLAN_NUMBER.search(path)


def test_tracked_listing_is_not_empty() -> None:
    assert len(_tracked_public_paths()) >= TRACKED_FLOOR


def test_no_public_path_carries_a_plan_number() -> None:
    offenders = [p for p in _tracked_public_paths() if PLAN_NUMBER.search(p)]
    assert offenders == [], f"internal plan numbers in public paths: {offenders}"
