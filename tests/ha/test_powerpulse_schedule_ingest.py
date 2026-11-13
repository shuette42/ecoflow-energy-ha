"""The wallbox charging schedule (209/2, #731) through the PowerOcean
coordinator, for the plain `property` push and for the `get_reply` bundle.
Both reach the coordinator through the merged-bundle path of the ingest.

`tests/test_powerpulse_wallbox.py` tests the mapping itself; this only proves
the coordinator calls it for each kind of frame. Both frames are real, taken from
`tests/fixtures/powerpulse/r372_timer_task_731_20261112.json`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    EV_TASK_LIST_KEY,
)
from tests.ha.test_powerpulse_edev_ingest import POWEROCEAN_DEVICE, _coordinator

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerpulse"
    / "r372_timer_task_731_20261112.json"
)


def _frame(label: str) -> bytes:
    frames: list[dict[str, Any]] = json.loads(FIXTURE.read_text())["frames"]
    return bytes.fromhex(next(f["hex"] for f in frames if f["label"] == label))


async def test_a_property_push_lands_the_schedule(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property",
        _frame("weekly_mon_thu"),
    )

    assert parsed is not None
    tasks = parsed[EV_TASK_LIST_KEY]
    assert [(t["current_a"], t["repeat"], t["weekdays"]) for t in tasks] == [
        (10, "weekly", ["mon", "thu"])
    ]


async def test_the_get_all_reply_lands_the_schedule_beside_the_rest(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply",
        _frame("get_all_reply_with_209_2"),
    )

    assert parsed is not None
    tasks = parsed[EV_TASK_LIST_KEY]
    assert [(t["current_a"], t["window"], t["repeat"]) for t in tasks] == [
        (6, "23:30-24:00", "daily")
    ]
    # The bundle is merged, not replaced by the schedule.
    assert len(parsed) > 1
