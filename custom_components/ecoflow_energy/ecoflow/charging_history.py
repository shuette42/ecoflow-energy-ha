"""Completed PowerPulse orders, retained by identity rather than poll count."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any

_LOGGER = logging.getLogger(__name__)


def invalid_field(field: str) -> ValueError:
    """Log only the schema field, never a record value or identifier."""
    _LOGGER.debug("Invalid charging order field: %s", field)
    return ValueError(f"Invalid charging order field: {field}")


def identity(value: str) -> str:
    """Keep cloud identifiers out of entity IDs and the local ledger."""
    return hashlib.sha256(value.encode()).hexdigest()


def is_identity(value: Any) -> bool:
    """True for a 64-character lowercase hex digest, the shape identity() returns."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


def merge_orders(
    previous: dict[str, dict[str, Any]], rows: list[dict[str, Any]], serial: str
) -> dict[str, dict[str, Any]]:
    """Upsert validated completed orders; never assign from current selection.

    Only the allowlisted fields below leave the response. Order corrections
    replace the earlier entry, including corrections to vehicle attribution.
    Missing cloud history never deletes previously collected energy.
    """
    result = dict(previous)
    updates: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("sn") != serial:
            raise invalid_field("sn")
        end = row.get("endTime")
        if end in (None, "", 0, "0"):
            continue
        if not isinstance(end, str):
            raise invalid_field("endTime")
        try:
            datetime.fromisoformat(end)
        except ValueError:
            raise invalid_field("endTime") from None
        order = row.get("orderId")
        vehicle = row.get("vehicleId")
        energy = row.get("chargedEnergy")
        for field, value in (("orderId", order), ("vehicleId", vehicle)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, str))
                or not str(value).strip()
            ):
                raise invalid_field(field)
        if type(energy) is not int or energy < 0:
            raise invalid_field("chargedEnergy")
        name = row.get("vehicleName")
        if name is not None and not isinstance(name, str):
            raise invalid_field("vehicleName")
        key = identity(str(order))
        record = {
            "vehicle": identity(str(vehicle)),
            "other": str(vehicle) == "-1",
            "name": (name or "").strip()[:200],
            "energy_wh": energy,
            "ended": end,
        }
        if key in updates and updates[key] != record:
            raise invalid_field("orderId (conflicting duplicate)")
        updates[key] = record
    result.update(updates)
    return result


def vehicle_totals(orders: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group stable vehicle identities; the latest completed record names them."""
    totals: dict[str, dict[str, Any]] = {}
    for record in sorted(orders.values(), key=lambda item: item["ended"]):
        key = record["vehicle"]
        total = totals.setdefault(
            key, {"energy_wh": 0, "sessions": 0, "name": "", "other": record["other"]}
        )
        total["energy_wh"] += record["energy_wh"]
        total["sessions"] += 1
        if record["name"]:
            total["name"] = record["name"]
    return totals


def valid_saved_ledger(saved: Any) -> bool:
    """Validate storage before exposing counters or attempting another merge."""
    if (
        not isinstance(saved, dict)
        or not isinstance(saved.get("orders"), dict)
        or not isinstance(saved.get("vehicles"), dict)
    ):
        return False

    for key, record in saved["orders"].items():
        if not is_identity(key) or not isinstance(record, dict):
            return False
        if (
            not is_identity(record.get("vehicle"))
            or type(record.get("other")) is not bool
            or not isinstance(record.get("name"), str)
        ):
            return False
        if (
            type(record.get("energy_wh")) is not int
            or record["energy_wh"] < 0
            or not isinstance(record.get("ended"), str)
        ):
            return False
        try:
            datetime.fromisoformat(record["ended"])
        except ValueError:
            return False
    for key, total in saved["vehicles"].items():
        if not is_identity(key) or not isinstance(total, dict):
            return False
        if (
            not isinstance(total.get("name"), str)
            or type(total.get("other")) is not bool
        ):
            return False
        if any(
            type(total.get(field)) is not int or total[field] < 0
            for field in ("energy_wh", "sessions")
        ):
            return False
    computed = vehicle_totals(saved["orders"])
    if not computed.keys() <= saved["vehicles"].keys():
        return False
    return all(
        total["energy_wh"] == computed.get(key, {}).get("energy_wh", 0)
        and total["sessions"] == computed.get(key, {}).get("sessions", 0)
        for key, total in saved["vehicles"].items()
    )
