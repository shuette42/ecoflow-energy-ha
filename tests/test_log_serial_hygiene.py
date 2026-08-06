"""No log call may pass a full device serial or an unmasked topic.

Reporters are routinely asked to enable debug logging and attach the output to
a public issue, so a log line is as public as a diagnostics download. The
diagnostics export was given a single check on the way out for exactly this
reason; logging has no such choke point, so the guarantee is held here instead
of in each of the thirty-odd call sites.

The convention is `sn[:4]` for serials, `mask_topic()` for topics and
`sanitize_frame()` for payloads. This test fails on a new call site that
forgets any of the three, which is the failure mode that produced three
separate leaks in one release - each found in the section next to the one
being worked on.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "ecoflow_energy"

_LOG_CALL = re.compile(
    r"(?:_LOGGER\.(?:debug|info|warning|error|exception|critical)|_log_issue|_log_retryable)\(",
)
# `self.device_sn` / `self._device_sn` NOT followed by a slice.
_BARE_SERIAL = re.compile(r"self\._?device_sn(?!\s*\[)")
# A topic f-string or variable handed straight to a log call.
_BARE_TOPIC = re.compile(r"(?<![\w.])topic(?![\w])")
# A wire or command payload handed straight to a log call. The two patterns
# above match the *variables* that carry a serial, which is why they missed
# the payloads whose *content* carries one: the Smart Plug SET JSON embeds
# the full serial in its "sn" field, and a Standard Mode SET reply echoes it
# in the body. Truncating is not a defense - a serial cut in half is still a
# fragment of a serial.
_BARE_PAYLOAD = re.compile(r"(?<![\w.])payload(?![\w])")


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


def _strip_sanitize_frame_args(body: str) -> str:
    """Blank the argument text of every sanitize_frame(...) call in a body.

    The masker takes the full serial as the secret to erase, so its argument
    list is the one place inside a log call where `self.device_sn` is the fix
    rather than the leak. Only that span is exempted - a serial passed to the
    log call next to a sanitize_frame() call still fails.
    """
    out: list[str] = []
    index = 0
    while True:
        start = body.find("sanitize_frame(", index)
        if start == -1:
            out.append(body[index:])
            return "".join(out)
        scan = start + len("sanitize_frame(")
        out.append(body[index:scan])
        depth = 1
        while scan < len(body) and depth:
            if body[scan] == "(":
                depth += 1
            elif body[scan] == ")":
                depth -= 1
            scan += 1
        index = scan


def _offenders(
    pattern: re.Pattern[str],
    transform: Callable[[str], str] | None = None,
) -> list[str]:
    found: list[str] = []
    for path in sorted(COMPONENT.rglob("*.py")):
        source = path.read_text()
        for line_no, body in _log_call_bodies(source):
            if pattern.search(transform(body) if transform else body):
                rel = path.relative_to(COMPONENT.parent.parent)
                found.append(f"{rel}:{line_no}: {' '.join(body.split())[:100]}")
    return found


def test_no_full_serial_in_log_calls() -> None:
    offenders = _offenders(_BARE_SERIAL, _strip_sanitize_frame_args)
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


def test_no_unmasked_payload_in_log_calls() -> None:
    offenders = [
        entry
        for entry in _offenders(_BARE_PAYLOAD)
        # sanitize_frame(payload, ...) is the fix, not a violation.
        if "sanitize_frame(" not in entry
    ]
    assert not offenders, (
        "log calls pass a raw payload, mask it with sanitize_frame() before "
        "truncating:\n" + "\n".join(offenders)
    )
