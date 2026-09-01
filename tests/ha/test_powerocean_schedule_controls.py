"""Release-surface checks for PowerOcean scheduled charge tasks."""

from custom_components.ecoflow_energy.const import (
    POWEROCEAN_BINARY_SENSORS,
    POWEROCEAN_NUMBERS,
    POWEROCEAN_SENSORS,
    POWEROCEAN_SWITCHES,
    SCHEDULE_MAX_INDEX,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator


def _schedule_keys(definitions) -> set[str]:
    return {
        definition.key
        for definition in definitions
        if definition.key.startswith("schedule_")
    }


def test_only_device_reported_running_flags_are_exposed() -> None:
    """Unverified configuration fields stay out of the HA entity surface."""
    assert _schedule_keys(POWEROCEAN_SENSORS) == set()
    assert _schedule_keys(POWEROCEAN_NUMBERS) == set()
    assert _schedule_keys(POWEROCEAN_SWITCHES) == set()
    assert _schedule_keys(POWEROCEAN_BINARY_SENSORS) == {
        f"schedule_{index}_running"
        for index in range(1, SCHEDULE_MAX_INDEX + 1)
    }


def test_schedule_writes_have_no_coordinator_entrypoint() -> None:
    """A broker acknowledgement cannot be surfaced as schedule success."""
    assert not hasattr(
        EcoFlowDeviceCoordinator, "async_set_powerocean_schedule_armed"
    )
    assert not hasattr(
        EcoFlowDeviceCoordinator, "async_set_powerocean_schedule_power"
    )
