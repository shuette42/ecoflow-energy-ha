"""Shared entity helpers for EcoFlow Energy platforms."""

from __future__ import annotations

from homeassistant.core import callback

from typing import TYPE_CHECKING, Any, NoReturn

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import EcoFlowDeviceCoordinator


def raise_set_failed(entity_id: str) -> NoReturn:
    """Report a SET command that never reached the device.

    A control whose command was not delivered must say so. Returning
    quietly leaves Home Assistant showing the requested value while the
    device keeps the old one, and the only trace is a log line the user
    has no reason to look for (issue #185).
    """
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="set_command_failed",
        translation_placeholders={"entity": entity_id},
    )


def raise_set_unsupported(entity_id: str) -> NoReturn:
    """Report a control that has no command template for this device."""
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="set_command_unsupported",
        translation_placeholders={"entity": entity_id},
    )


def raise_set_not_ready(entity_id: str) -> NoReturn:
    """Report a write that needs a device value the device has not sent yet.

    Some settings travel as one wire value with two halves, so writing one
    half means resending the other. Until the device has reported that other
    half there is nothing to resend, and guessing it would change a setting
    the user never touched. This is a temporary state that clears with the
    next status frame - saying "not supported" instead would send the user
    looking for a device limitation that does not exist.
    """
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="set_command_not_ready",
        translation_placeholders={"entity": entity_id},
    )


def raise_set_gone(entity_id: str) -> NoReturn:
    """Report a write against a reading the device has stopped sending.

    Not the same case as `raise_set_not_ready`, which promises the state
    clears with the next status frame. A schedule the owner deleted in the
    app is dropped from the device's task list for good, so the next frame
    changes nothing and telling the user to try again in a moment would send
    them waiting on something that is never coming.
    """
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="set_command_gone",
        translation_placeholders={"entity": entity_id},
    )


def raise_set_rejected(entity_id: str, reason: str) -> NoReturn:
    """Report a value this device will not accept, before anything is sent.

    Some settings constrain each other: a discharge limit has to stay below
    the charge limit, for instance. A builder that refuses such a pair raises
    ``ValueError``, which Home Assistant renders as "Unknown error" with a
    traceback. The user asked for something the device cannot do, which is
    worth saying plainly.
    """
    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="set_value_rejected",
        translation_placeholders={"entity": entity_id, "reason": reason},
    )


@callback
def reading_reported(
    coordinator: EcoFlowDeviceCoordinator, key: str, needs_nonzero: bool = False
) -> bool:
    """Return whether the device has this reading in its current state.

    Both stores are checked because they are filled at different points:
    the persistent device data by the parsers, the coordinator payload by
    the update that follows.

    With ``needs_nonzero`` the key has to carry a value other than zero. A
    reading that must be published as an explicit zero to stop it latching
    would otherwise announce itself on the first frame of every device.

    A key that is present and None counts as reported: several messages use
    None to retract a reading, and an accessory that vanished from its device
    keeps its entity rather than having it removed underneath the user. A
    control that cannot be sent against a retracted reading therefore has to
    check the value itself before it builds a write - existence here is not a
    promise that there is something to send.

    Used by the sensor and binary sensor platforms to decide whether an
    accessory entity exists yet, which is why it lives here rather than on
    one of them.
    """
    for store in (coordinator.device_data, coordinator.data or {}):
        if key not in store:
            continue
        if not needs_nonzero:
            return True
        value = store[key]
        if isinstance(value, (int, float)) and value:
            return True
    return False


def as_known_int(value: Any) -> int | None:
    """Return an integral setting as an int, or None if it is not known.

    Settings that constrain each other have to be read back out of the
    coordinator before they can be written together, and the value found
    there may be a float: Home Assistant hands ``number.set_value`` a float,
    and the optimistic write stores exactly that for the lock window and
    until the device echoes the change. A strict ``isinstance(v, int)`` on
    the counterpart therefore fails for several seconds after any number
    write, turning a value that is merely in flight into a refused write.

    A None here means "not reported yet", so it pairs with
    ``raise_set_not_ready`` rather than ``raise_set_unsupported``.

    ``bool`` is excluded because it is an ``int`` in Python and never a
    percentage.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else None


class EcoFlowWriteGateMixin:
    """State-write gate shared by all EcoFlow entity platforms.

    Coordinator ticks arrive every few seconds, but the HA recorder writes
    on every state update. The gate skips ``async_write_ha_state()`` when
    nothing observable changed. Both the entity VALUE and its AVAILABILITY
    are part of the comparison: an availability flip with an unchanged
    value must still reach the state machine, otherwise entities appear
    available long after the connection degraded (and vice versa).
    """

    _last_written_value: Any = None
    _last_written_available: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Seed the availability sentinel with the state HA writes on add.

        The sentinel must capture what actually reached the state machine,
        not what the first gate pass observes. If the device is silent while
        Home Assistant starts, HA writes ``unavailable`` on add; once data
        resumes with unchanged values, the availability flip is the only
        difference. A lazily seeded sentinel would classify that flip as
        "unchanged" and the entity would stay unavailable until its value
        happens to change.
        """
        await super().async_added_to_hass()  # type: ignore[misc]
        self._last_written_available = self.available  # type: ignore[attr-defined]

    def _write_state_if_changed(self, new_value: Any) -> None:
        """Write HA state when the value or the availability changed."""
        new_available = self.available  # type: ignore[attr-defined]
        if (
            # Unseeded sentinel (entity not fully added yet): write, never
            # skip - a spurious write is harmless, a swallowed availability
            # flip strands the entity.
            self._last_written_available is not None
            and new_value == self._last_written_value
            and new_available == self._last_written_available
        ):
            return
        self._last_written_value = new_value
        self._last_written_available = new_available
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _write_state_always(self, new_value: Any) -> None:
        """Write HA state unconditionally (optimistic SET feedback path)."""
        self._last_written_value = new_value
        self._last_written_available = self.available  # type: ignore[attr-defined]
        self.async_write_ha_state()  # type: ignore[attr-defined]
