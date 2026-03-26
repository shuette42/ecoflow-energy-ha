"""Shared fixtures for EcoFlow Energy tests.

The ecoflow/ sub-package (core library) has no HA dependencies and can be
tested standalone.  We add its parent to sys.path so tests can import
``ecoflow_energy.ecoflow.*`` — but the HA integration modules
(coordinator, config_flow, sensor, …) are NOT importable here because
they require homeassistant.

For the ecoflow sub-package we also register it directly so that
``from ecoflow_energy.ecoflow.X import Y`` works even though the
parent ``ecoflow_energy.__init__`` would fail (HA imports).
"""

import importlib
import sys
import types
from pathlib import Path

_CC = Path(__file__).resolve().parent.parent / "custom_components"

# Put custom_components/ on sys.path
if str(_CC) not in sys.path:
    sys.path.insert(0, str(_CC))

# Register the ecoflow_energy package as a namespace so that importing
# ecoflow_energy.ecoflow.* works without triggering ecoflow_energy/__init__.py
# (which imports homeassistant).
_PKG = "ecoflow_energy"
if _PKG not in sys.modules:
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(_CC / _PKG)]
    pkg.__package__ = _PKG
    sys.modules[_PKG] = pkg
