"""Replay of observed Delta 2 Max MPPT frames in Enhanced mode (issue #516).

The fixture holds the ``mppt.*`` keys of 14 observed device frames: 13 replies
to the latestQuotas request and one /app/device/property push. The device
reports millivolts, milliamperes, watts and degrees Celsius. Voltage times
current stays within 5 W of the reported watts on every frame that carries
all three values, so these are the units to assert.

Expected values are computed in the test from the raw value and the physical
unit, never copied from the parser output.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from custom_components.ecoflow_energy.coordinator.mqtt_ingest import MqttIngestMixin
from custom_components.ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_DELTA,
    device_log_tag,
)
from custom_components.ecoflow_energy.ecoflow.parsers.delta import parse_delta_report
from custom_components.ecoflow_energy.ecoflow.parsers.delta_http import (
    parse_delta_http_quota,
)

_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "delta2max"
    / "enhanced_r351_mppt.json"
)

# (raw key in the frame, parser output key, divisor from the device unit)
_PHYSICS: list[tuple[str, str, float]] = [
    ("mppt.inVol", "solar_in_vol_v", 1000.0),  # mV -> V
    ("mppt.pv2InVol", "solar2_in_vol_v", 1000.0),  # mV -> V
    ("mppt.pv2InAmp", "solar2_in_amp_a", 1000.0),  # mA -> A
    ("mppt.pv2InWatts", "solar2_in_w", 1.0),  # W
    ("mppt.outWatts", "mppt_out_w", 1.0),  # W
    ("mppt.pv2MpptTemp", "solar2_mppt_temp_c", 1.0),  # degrees Celsius
]

_FRAMES: list[dict[str, Any]] = json.loads(_FIXTURE.read_text())["frames"]
_BY_INDEX: dict[int, dict[str, Any]] = {f["frame_index"]: f for f in _FRAMES}


def _expected(frame: dict[str, Any]) -> dict[str, float]:
    """Output key -> value, for the target keys this frame carries."""
    return {
        out_key: frame["mppt"][raw_key] / divisor
        for raw_key, out_key, divisor in _PHYSICS
        if raw_key in frame["mppt"]
    }


class _DeltaIngest(MqttIngestMixin):
    """Minimal host for the real MQTT message parser of a Delta device."""

    device_sn = "DELTA2MAXTEST0001"
    device_type = DEVICE_TYPE_DELTA
    device_tag = device_log_tag(device_sn)


def test_fixture_topic_split() -> None:
    """The module docstring counts 13 replies and one push."""
    assert Counter(f["topic"] for f in _FRAMES) == {"get_reply": 13, "property": 1}


def test_car_and_12v_outputs_are_zero_in_every_observed_frame() -> None:
    """The factors of these keys stay unconfirmed while the device reports 0."""
    keys = ["carOutWatts", "dcdc12vWatts", "dcdc12vVol", "carOutVol"]
    observed = [
        frame["mppt"][f"mppt.{key}"]
        for frame in _FRAMES
        for key in keys
        if f"mppt.{key}" in frame["mppt"]
    ]
    assert len(observed) == 52
    assert set(observed) == {0}


def test_voltage_times_current_matches_reported_watts() -> None:
    """Parser output and the frame's own watts agree, whatever the divisors."""
    triples = 0
    mismatches: list[str] = []
    for frame in _FRAMES:
        mppt = frame["mppt"]
        parsed = parse_delta_http_quota(dict(mppt))
        products = {
            "solar_in_w": parsed.get("solar_in_vol_v", 0)
            * parsed.get("solar_in_amp_a", 0),
            "solar2_in_w": parsed.get("solar2_in_vol_v", 0)
            * parsed.get("solar2_in_amp_a", 0),
        }
        if "mppt.outVol" in mppt:
            products["mppt_out_w"] = mppt["mppt.outVol"] * mppt["mppt.outAmp"] / 1e6
        for out_key, product in products.items():
            if out_key not in parsed or product == 0:
                continue
            triples += 1
            if abs(product - parsed[out_key]) > 5:
                mismatches.append(
                    f"frame {frame['frame_index']} {out_key}: "
                    f"{product:.2f} W from V*I, {parsed[out_key]!r} W reported"
                )
    assert triples == 40
    assert not mismatches, "\n".join(mismatches)


def test_replay_observed_frames_through_delta_http_parser() -> None:
    """Every observed frame yields the physical value for the six MPPT keys.

    Catches a wrong divisor on any one of the six keys (for example restoring
    ``/ 10.0`` on ``mppt_out_w`` while the other five are fixed): that key is
    off by a factor of ten on every frame and the mismatch list names it.
    """
    compared_frames = 0
    compared_values = 0
    mismatches: list[str] = []
    for frame in _FRAMES:
        parsed = parse_delta_http_quota(dict(frame["mppt"]))
        compared_frames += 1
        for out_key, expected in _expected(frame).items():
            compared_values += 1
            actual = parsed.get(out_key)
            if actual != pytest.approx(expected, rel=1e-9):
                mismatches.append(
                    f"frame {frame['frame_index']} {out_key}: "
                    f"got {actual!r}, expected {expected!r}"
                )
    assert compared_frames == 14
    assert compared_values == 82
    assert not mismatches, "\n".join(mismatches)


@pytest.mark.parametrize("frame_index", [35, 39])
def test_enhanced_ingest_path_carries_physical_mppt_values(frame_index: int) -> None:
    """The real message parser returns the physical values for both topics.

    Frame 35 is a latestQuotas reply and frame 39 an /app/device/property
    push, the two Enhanced mode paths for a Delta 2 Max. Catches a routing
    change that bypasses the Delta quota parser on either topic (for example
    ``_parse_message`` returning the raw ``quotaMap`` or ``params``): the raw
    keys would come back and the six output keys would be missing.
    """
    frame = _BY_INDEX[frame_index]
    sn = _DeltaIngest.device_sn
    if frame["topic"] == "get_reply":
        topic = f"/app/user123/{sn}/thing/property/get_reply"
        payload = json.dumps({"data": {"quotaMap": frame["mppt"]}}).encode()
    else:
        assert frame["topic"] == "property"
        topic = f"/app/device/property/{sn}"
        payload = json.dumps({"params": frame["mppt"]}).encode()

    parsed = _DeltaIngest()._parse_message(topic, payload)

    assert parsed is not None
    expected = _expected(frame)
    assert len(expected) == (6 if frame["topic"] == "get_reply" else 4)
    mismatches = [
        f"{out_key}: got {parsed.get(out_key)!r}, expected {value!r}"
        for out_key, value in expected.items()
        if parsed.get(out_key) != pytest.approx(value, rel=1e-9)
    ]
    assert not mismatches, "\n".join(mismatches)


def test_report_parser_and_quota_parser_agree_on_frame_35() -> None:
    """The typeCode push parser and the quota parser give one answer per key.

    Both write the same entities, and no observed frame is a typeCode push, so
    the raw values of frame 35 go through ``parse_delta_report`` as an
    ``mpptStatus`` report and through ``parse_delta_http_quota`` as dotted
    ``mppt.*`` keys. Catches one parser keeping an old divisor while the other
    is fixed (for example ``pv2InWatts`` back at ``/ 10`` in the report parser
    only): that key then differs between the two outputs and from the
    physical value.
    """
    mppt = _BY_INDEX[35]["mppt"]
    raw = {raw_key: mppt[raw_key] for raw_key, _, _ in _PHYSICS}
    # A zero raw value divides to zero under any divisor and would hide a wrong one.
    assert all(value > 0 for value in raw.values()), raw
    report = {
        "typeCode": "mpptStatus",
        "params": {key.removeprefix("mppt."): value for key, value in raw.items()},
    }

    from_report = parse_delta_report(report)
    from_quota = parse_delta_http_quota(dict(raw))

    expected = _expected(_BY_INDEX[35])
    assert len(expected) == len(_PHYSICS) == 6
    for _, out_key, _ in _PHYSICS:
        assert from_report.get(out_key) == pytest.approx(expected[out_key], rel=1e-9), (
            f"report parser {out_key}"
        )
        assert from_quota.get(out_key) == pytest.approx(expected[out_key], rel=1e-9), (
            f"quota parser {out_key}"
        )
        assert from_report[out_key] == pytest.approx(from_quota[out_key], rel=1e-9)
