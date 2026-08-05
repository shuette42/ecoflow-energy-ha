"""No log call may pass a full device serial or an unmasked topic.

Reporters are routinely asked to enable debug logging and attach the output to
a public issue, so a log line is as public as a diagnostics download. The
diagnostics export was given a single check on the way out for exactly this
reason; logging has no such choke point, so the guarantee is held here instead
of in each of the thirty-odd call sites.

The convention is `sn[:4]` for serials and `mask_topic()` for topics. This test
fails on a new call site that forgets either, which is the failure mode that
produced three separate leaks in one release - each found in the section next
to the one being worked on.
"""

from __future__ import annotations

import re
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "ecoflow_energy"

_LOG_CALL = re.compile(
    r"(?:_LOGGER\.(?:debug|info|warning|error|exception|critical)|_log_issue|_log_retryable)\(",
)
# `self.device_sn` / `self._device_sn` NOT followed by a slice.
_BARE_SERIAL = re.compile(r"self\._?device_sn(?!\s*\[)")
# A topic f-string or variable handed straight to a log call.
_BARE_TOPIC = re.compile(r"(?<![\w.])topic(?![\w])")


def _log_call_bodies(source: str) -> list[tuple[int, str]]:
    """Return (line number, argument text) for every logging call.

    Balanced-paren scan rather than a line regex, because most of these calls
    are wrapped across several lines and the serial usually sits on one of the
    continuation lines rather than next to the call.
    """
    bodies: list[tuple[int, str]] = []
    for match in _LOG_CALL.finditer(source):
        depth = 1
        index = match.end()
        while index < len(source) and depth:
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
            index += 1
        line_no = source.count("\n", 0, match.start()) + 1
        bodies.append((line_no, source[match.end() : index - 1]))
    return bodies


def _offenders(pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    for path in sorted(COMPONENT.rglob("*.py")):
        source = path.read_text()
        for line_no, body in _log_call_bodies(source):
            if pattern.search(body):
                rel = path.relative_to(COMPONENT.parent.parent)
                found.append(f"{rel}:{line_no}: {' '.join(body.split())[:100]}")
    return found


def test_no_full_serial_in_log_calls() -> None:
    offenders = _offenders(_BARE_SERIAL)
    assert not offenders, "log calls pass a full serial, use sn[:4]:\n" + "\n".join(
        offenders
    )


def test_no_unmasked_topic_in_log_calls() -> None:
    offenders = [
        entry
        for entry in _offenders(_BARE_TOPIC)
        # mask_topic(topic) is the fix, not a violation.
        if "mask_topic(" not in entry
    ]
    assert not offenders, (
        "log calls pass a raw topic, wrap it in mask_topic():\n" + "\n".join(offenders)
    )
