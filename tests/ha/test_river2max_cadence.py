"""The RIVER 2 Max pushes about every 4 minutes; the stale watch must allow it (#585).

Under the 35 s default the coordinator re-sends its initial requests and then
reconnects through every quiet spell. The cadence is read from the recorded
frames, not typed in: the gaps between the frames of the 2026-10-18 recording
set the floor the threshold has to clear.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DOMAIN,
    HARD_UNAVAILABLE_S,
    MODE_ENHANCED,
    RIVER2MAX_SOFT_UNAVAILABLE_S,
    RIVER2MAX_STALE_THRESHOLD_S,
    SOFT_UNAVAILABLE_S,
    STALE_THRESHOLD_S,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.const import DEVICE_TYPE_DELTA

_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "river2max"
    / "r611_frames_issue585.json"
)
_OFFSETS: list[float] = [
    f["offset_s"] for f in json.loads(_FIXTURE.read_text())["frames"]
]

# Fictional serials - never a real device.
_R611 = {
    "sn": "R611TEST0000ABCD",
    "name": "",
    "product_name": "",
    "device_type": DEVICE_TYPE_DELTA,
    "online": 1,
}
_R351 = {**_R611, "sn": "R351TEST0000ABCD"}


def _coordinator(
    hass: HomeAssistant, device: dict[str, Any]
) -> EcoFlowDeviceCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, device)


def test_the_recorded_gaps_are_longer_than_the_default_watch() -> None:
    """Without the longer watch the recording itself would read as silent."""
    gaps = [b - a for a, b in zip(_OFFSETS, _OFFSETS[1:], strict=False)]
    assert max(gaps) > STALE_THRESHOLD_S


async def test_the_river2max_watch_clears_the_longest_recorded_gap(
    hass: HomeAssistant,
) -> None:
    gaps = [b - a for a, b in zip(_OFFSETS, _OFFSETS[1:], strict=False)]
    coordinator = _coordinator(hass, _R611)
    assert coordinator._stale_threshold_s() == RIVER2MAX_STALE_THRESHOLD_S
    assert coordinator._stale_threshold_s() > max(gaps)
    assert coordinator._soft_unavailable_s() == RIVER2MAX_SOFT_UNAVAILABLE_S
    # The stale band keeps its width and the hard limit stays above the soft one.
    assert (
        RIVER2MAX_SOFT_UNAVAILABLE_S - RIVER2MAX_STALE_THRESHOLD_S
        >= SOFT_UNAVAILABLE_S - STALE_THRESHOLD_S
    )
    assert coordinator._hard_unavailable_s() == HARD_UNAVAILABLE_S
    assert HARD_UNAVAILABLE_S > RIVER2MAX_SOFT_UNAVAILABLE_S


async def test_a_neighbouring_delta_keeps_the_default_watch(
    hass: HomeAssistant,
) -> None:
    """The negative control: a Delta 2 Max serial is not slowed down."""
    coordinator = _coordinator(hass, _R351)
    assert coordinator._stale_threshold_s() == STALE_THRESHOLD_S
    assert coordinator._soft_unavailable_s() == SOFT_UNAVAILABLE_S
