"""PowerPulse 1 settings written through a PowerOcean (#731).

The builder is checked byte for byte against the eight app writes of the
reporter's recording (`tests/fixtures/powerpulse/r372_settings_731_20261113.json`,
label `set_fN_V`: field N set to V). Only the sequence number is taken from
the frame, every other byte comes from the builder.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.ecoflow_energy.diagnostics import _redact_serials
from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    build_wallbox_param_set_payload,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerpulse"
    / "r372_settings_731_20261113.json"
)
FRAMES: list[dict[str, Any]] = json.loads(FIXTURE.read_text())["frames"]
SERIAL = "X" * 16  # both serials were masked in the download

# Field of the write -> builder keyword and how the label value maps to it.
_SETTING_BY_FIELD = {
    4: ("plug_and_play", bool),
    5: ("work_mode", int),
    6: ("battery_discharge_block", bool),
    7: ("continuous_charging", bool),
}
WRITES = [f for f in FRAMES if f["topic"] == "set"]


def _setting(label: str) -> dict[str, Any]:
    _, field, value = label.split("_")
    name, cast = _SETTING_BY_FIELD[int(field[1:])]
    return {name: cast(int(value))}


@pytest.mark.parametrize("frame", WRITES, ids=[f["label"] for f in WRITES])
def test_builder_reproduces_the_app_write(frame: dict[str, Any]) -> None:
    """Every byte of the app's frame, the envelope included.

    Mutation probes: drop the explicit zero (encode the setting only when
    true) -> the `_0` cases fail; swap fields 4 and 6 -> plug-and-play and
    battery cases fail; restore the PowerOcean default envelope -> all fail.
    """
    wire = bytes.fromhex(frame["hex"])
    (header,), _ = decode_header_message(wire)
    built = build_wallbox_param_set_payload(
        SERIAL, SERIAL, seq=header["seq"], **_setting(frame["label"])
    )
    assert built == wire


def test_eight_writes_cover_both_values_of_every_setting() -> None:
    """The parametrized cases above are the evidence: one per value."""
    expected = [f"set_f{n}_{v}" for n in (4, 6, 7) for v in (0, 1)]
    assert sorted(f["label"] for f in WRITES) == sorted(
        [*expected, "set_f5_1", "set_f5_2"]
    )


@pytest.mark.parametrize(
    "setting",
    [
        {},
        {"plug_and_play": True, "work_mode": 1},
        {"work_mode": 3},
        {"work_mode": True},
        {"plug_and_play": 1},
        {"current_a": 16},
    ],
)
def test_builder_refuses_anything_but_one_known_setting(
    setting: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        build_wallbox_param_set_payload(SERIAL, SERIAL, **setting)


def test_diagnostics_mask_the_wallbox_serial() -> None:
    """`ev_charger_sn` is new in the PowerOcean store and lands in a dump."""
    redacted = _redact_serials({"ev_charger_sn": "AC31ZZ0000000001"})
    assert "AC31ZZ0000000001" not in json.dumps(redacted)
