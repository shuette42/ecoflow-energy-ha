"""PowerPulse 1 Start/Stop Charging written through a PowerOcean (#731).

Every frame is from the reporter's recording of four app writes
(`tests/fixtures/powerpulse/r372_start_stop_731_20261114.json`, serial masked
as sixteen X). The builder is checked byte for byte against the writes, only
the sequence number is taken from the frame; the reports go through the same
decode and remap path a live frame takes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest

from custom_components.ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from custom_components.ecoflow_energy.ecoflow.energy_stream import (
    build_wallbox_charge_action_payload,
)
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_ev_charging_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
    decode_header_message,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "powerpulse"
    / "r372_start_stop_731_20261114.json"
)
FRAMES: dict[str, dict[str, Any]] = {
    f["label"]: f for f in json.loads(FIXTURE.read_text())["frames"]
}
SERIAL = "X" * 16  # both serials were masked in the download

WRITES: list[tuple[str, Literal["start", "stop"]]] = [
    ("app_start_1", "start"),
    ("app_stop_1", "stop"),
    ("app_start_2", "start"),
    ("app_stop_2", "stop"),
]


@pytest.mark.parametrize(("label", "action"), WRITES, ids=[w[0] for w in WRITES])
def test_builder_reproduces_the_app_write(
    label: str, action: Literal["start", "stop"]
) -> None:
    """Every byte of the app's frame, the envelope included.

    Mutation probes: swap the values of field 2 (start 1, stop 2) -> all four
    fail; swap field 1 and field 2 of the pdata -> all four fail.
    """
    wire = bytes.fromhex(FRAMES[label]["hex"])
    (header,), _ = decode_header_message(wire)
    built = build_wallbox_charge_action_payload(
        SERIAL, SERIAL, action=action, seq=header["seq"]
    )
    assert built == wire


@pytest.mark.parametrize("action", ["pause", "", "START", None])
def test_builder_refuses_any_other_action(action: Any) -> None:
    """Mutation probe: drop the action check -> `pause` encodes as a stop."""
    with pytest.raises(ValueError, match="action"):
        build_wallbox_charge_action_payload(SERIAL, SERIAL, action=action)


def test_builder_refuses_a_malformed_serial() -> None:
    """Mutation probe: drop the serial validation -> the short serial is sent."""
    with pytest.raises(ValueError):
        build_wallbox_charge_action_payload("X" * 15, SERIAL, action="start")


def _report(label: str) -> dict[str, Any]:
    """Run a recorded frame through the PowerOcean decode and remap path."""
    results = decode_proto_runtime_headers(
        bytes.fromhex(FRAMES[label]["hex"]), device_type=DEVICE_TYPE_POWEROCEAN
    )
    mapped = [r.mapped for r in results if r.mapped.get("_is_ev_charging_param")]
    assert mapped, f"{label}: the 209/8 header was not routed"
    raw = {k: v for k, v in mapped[0].items() if not k.startswith("_")}
    return remap_ev_charging_keys(raw)


STATUS_REPORTS = [
    ("report_status_4_after_start_1", "suspended_charger"),
    ("report_status_6_after_stop_1", "finishing"),
    ("report_status_4_after_start_2", "suspended_charger"),
    ("report_status_6_after_stop_2", "finishing"),
]


@pytest.mark.parametrize(
    ("label", "status"), STATUS_REPORTS, ids=[s[0] for s in STATUS_REPORTS]
)
def test_status_report_carries_status_and_serial(label: str, status: str) -> None:
    """The read-back a press waits for: status and the wallbox it belongs to.

    Mutation probe: map status 4 to `charging` -> the start reports fail.
    """
    parsed = _report(label)
    assert parsed["ev_charge_status"] == status
    assert parsed["ev_charger_sn"] == SERIAL


def test_order_state_reports_do_not_move_the_status() -> None:
    """The faster order-state reports must not confirm or change a press.

    Mutation probes: map `order_state` to a reading (`ev_order_state`) -> the
    key check fails; map the suspended status to `charging` -> the stop's
    order-state report no longer carries `suspended_charger`.
    """
    for label in (
        "report_order_state_1_after_start_1",
        "report_order_state_3_after_stop_1",
    ):
        # The order state itself is not a reading: no key may carry it.
        assert not [k for k in _report(label) if "order_state" in k]

    store: dict[str, Any] = {}
    store.update(_report("report_status_6_after_stop_2"))
    assert store["ev_charge_status"] == "finishing"
    # The start's order-state report still carries the previous status.
    store.update(_report("report_order_state_1_after_start_1"))
    assert store["ev_charge_status"] == "finishing"

    store.update(_report("report_status_4_after_start_1"))
    assert store["ev_charge_status"] == "suspended_charger"
    # The stop's order-state report arrives before the stop is reported.
    store.update(_report("report_order_state_3_after_stop_1"))
    assert store["ev_charge_status"] == "suspended_charger"
    store.update(_report("report_status_6_after_stop_1"))
    assert store["ev_charge_status"] == "finishing"
