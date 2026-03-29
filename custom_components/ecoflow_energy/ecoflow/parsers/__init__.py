# Device-specific parsers for EcoFlow IoT-API reports.

from __future__ import annotations

from typing import Any


def _safe_float(val: Any) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
