"""Coordinator package for EcoFlow devices: re-exports the composed coordinator."""

# Load-bearing import: tests patch
# "custom_components.ecoflow_energy.coordinator.time.monotonic", which
# resolves through this package attribute. Do not remove.
import time  # noqa: F401

from .core import DeviceSnapshot, EcoFlowDeviceCoordinator

__all__ = ["DeviceSnapshot", "EcoFlowDeviceCoordinator"]
