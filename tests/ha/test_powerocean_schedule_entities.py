"""Entity checks for read-only PowerOcean schedule status."""

from custom_components.ecoflow_energy.const import (
    POWEROCEAN_BINARY_SENSORS,
    POWEROCEAN_SENSORS,
)


def test_schedule_running_is_accessory_gated_and_enhanced_only() -> None:
    definitions = [
        definition
        for definition in POWEROCEAN_BINARY_SENSORS
        if definition.key.startswith("schedule_")
    ]

    assert len(definitions) == 8
    assert all(definition.key.endswith("_running") for definition in definitions)
    assert all(definition.accessory for definition in definitions)
    assert all(definition.enhanced_only for definition in definitions)


def test_schedule_window_is_not_an_entity() -> None:
    assert not any(
        definition.key.startswith("schedule_")
        for definition in POWEROCEAN_SENSORS
    )
