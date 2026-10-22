"""Explicit grid connection beats energized backup; all inputs are synthetic."""

from __future__ import annotations

import pytest
from ecoflow_energy.ecoflow.parsers.powerocean_http import parse_powerocean_http_quota
from ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    flatten_heartbeat,
    remap_bp_keys,
    remap_ems_state_keys,
)


@pytest.mark.parametrize(
    "raw,expected", [(0, "ok"), (1, "not_detected"), (7, None)], ids=["0", "1", "7"]
)
def test_grid_state_http_and_proto_agree(raw: int, expected: str | None) -> None:
    """Integer codes 0 and 1 and an unknown code read the same on both paths."""
    assert (
        parse_powerocean_http_quota({"ems_change_report.sysGridSta": raw})[
            "grid_status"
        ]
        == expected
    )
    assert (
        remap_bp_keys({"sys_grid_sta": raw}, {}, "HJ31TEST00000001")["grid_status"]
        == expected
    )


def test_outage_survives_backup_voltage_and_restores() -> None:
    state = remap_bp_keys(
        {"sys_grid_sta": 1, "grid_is_energized": True}, {}, "HJ31TEST00000001"
    )
    state.update(
        flatten_heartbeat(
            {
                "pcs_a_phase": {"vol": 230.0},
                "pcs_load_info": [{"vol": 230.0, "pwr": 500.0}],
            }
        )
    )
    assert state["grid_status"] == "not_detected"
    state.update(remap_bp_keys({"sys_grid_sta": 0}, {}, "HJ31TEST00000001"))
    assert state["grid_status"] == "ok"
    state.update(remap_bp_keys({"sys_grid_sta": 9}, {}, "HJ31TEST00000001"))
    assert state["grid_status"] is None


def test_absence_and_command17_do_not_invent_grid_state() -> None:
    assert "grid_status" not in parse_powerocean_http_quota({})
    assert "grid_status" not in remap_bp_keys({}, {}, "HJ31TEST00000001")
    assert "grid_status" not in remap_ems_state_keys({"sys_grid_sta": 0})
    assert "grid_status" not in flatten_heartbeat({"pcs_a_phase": {"vol": 0}})


@pytest.mark.parametrize(
    "raw,http_expected,proto_expected",
    [
        (True, None, "not_detected"),
        (1.0, None, "not_detected"),
        ("1", None, None),
        (0.5, None, None),
        (0.0, None, "ok"),
    ],
    ids=["bool_true", "float_one", "string_one", "float_half", "float_zero"],
)
def test_non_integer_grid_codes_differ_between_http_and_proto(
    raw: object, http_expected: str | None, proto_expected: str | None
) -> None:
    """Pin how non-integer codes read today, including the asymmetry.

    The HTTP parser accepts only a plain ``int``, so a bool, a float or a
    string is unknown and the status clears to None. The protobuf path turns
    every numeric value, a bool included, into a float before its own bool
    guard runs, so ``True`` and ``1.0`` read ``not_detected`` and ``0.0``
    reads ``ok``; only ``0.5`` and the string are unknown there. A real EMS
    report carries an integer, so none of these reach a user today; the test
    pins the behaviour so a change to either path is a decision, not a drift.
    """
    http = parse_powerocean_http_quota({"ems_change_report.sysGridSta": raw})
    proto = remap_bp_keys({"sys_grid_sta": raw}, {}, "HJ31TEST00000001")
    assert http["grid_status"] is http_expected
    assert proto["grid_status"] == proto_expected
