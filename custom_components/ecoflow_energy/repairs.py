"""Repairs flows for EcoFlow Energy.

A Local lifetime counter held back for a day raises a notice (see
``coordinator/local_modbus.py``). Submitting it takes the inverter's value;
Home Assistant's own Ignore keeps the current total.
"""

from __future__ import annotations

from typing import Any, cast

from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator.local_modbus import EcoFlowLocalModbusCoordinator


class LocalCounterHeldRepairFlow(ConfirmRepairFlow):
    """Take the inverter's reading as the new floor of a held counter."""

    def __init__(self, entry_id: str, key: str) -> None:
        """Remember which entry's counter this notice is about."""
        self._entry_id = entry_id
        self._key = key

    # ``Any``: newer Home Assistant versions name this result type differently
    # (``RepairsFlowResult``), and an override must match the installed one.
    async def async_step_confirm(self, user_input: dict[str, str] | None = None) -> Any:
        """Accept the device value; a gone entry or key still finishes the flow."""
        if user_input is not None:
            coordinators = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
            for coordinator in coordinators.values():
                if isinstance(coordinator, EcoFlowLocalModbusCoordinator):
                    coordinator.accept_device_counter(self._key)
            return self.async_create_entry(data={})
        return await super().async_step_confirm(user_input)


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Return the fix flow of a repairs issue this integration raised."""
    data = data or {}
    return LocalCounterHeldRepairFlow(
        cast(str, data.get("entry_id", "")), cast(str, data.get("key", ""))
    )
