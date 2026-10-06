"""Completed orders: attribution, correction, pagination and privacy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from ecoflow_energy.ecoflow.app_api import AppApiClient
from ecoflow_energy.ecoflow.charging_history import (
    identity,
    merge_orders,
    vehicle_totals,
)

SERIAL = "C371TEST0001"


def order(
    order_id="order-a", vehicle="profile-a", energy=16004, name="Family EV", **kw
):
    return {
        "sn": SERIAL,
        "orderId": order_id,
        "vehicleId": vehicle,
        "vehicleName": name,
        "chargedEnergy": energy,
        "endTime": "2026-10-02 12:00:00",
        **kw,
    }


def test_attribution_repoll_restart_and_correction():
    rows = [order(), order("order-b", "profile-b", 2000, "Second EV")]
    ledger = merge_orders({}, rows, SERIAL)
    assert merge_orders(ledger, rows, SERIAL) == ledger
    # A persisted/reloaded ledger has the same identities, independent of names.
    import json

    restored = json.loads(json.dumps(ledger))
    assert vehicle_totals(restored)[identity("profile-a")]["energy_wh"] == 16004
    assert vehicle_totals(restored)[identity("profile-b")]["energy_wh"] == 2000
    corrected = merge_orders(restored, [order(energy=16000)], SERIAL)
    assert vehicle_totals(corrected)[identity("profile-a")]["energy_wh"] == 16000
    assert merge_orders(corrected, [], SERIAL) == corrected


def test_other_missing_name_same_name_and_rename():
    rows = [
        order(vehicle="-1", name="", energy=10),
        order("b", "a", 20, "Same"),
        order("c", "b", 30, "Same"),
    ]
    totals = vehicle_totals(merge_orders({}, rows, SERIAL))
    assert len(totals) == 3
    assert totals[identity("-1")]["other"] is True
    ledger = merge_orders({}, [order()], SERIAL)
    ledger = merge_orders(ledger, [order(name="Renamed")], SERIAL)
    assert len(ledger) == 1
    assert vehicle_totals(ledger)[identity("profile-a")]["name"] == "Renamed"


def test_newest_record_names_the_vehicle_by_instant_not_by_text():
    # Text order says the -02:00 record is older (05th < 06th); by instant it is
    # 2026-10-06T01:30Z, one hour newer than the +00:00 record at 00:30Z.
    rows = [
        order("new", "profile-a", 100, "New", endTime="2026-10-05T23:30:00-02:00"),
        order("old", "profile-a", 200, "Old", endTime="2026-10-06T00:30:00+00:00"),
    ]
    total = vehicle_totals(merge_orders({}, rows, SERIAL))[identity("profile-a")]
    assert total["name"] == "New"
    assert total["sessions"] == 2
    assert total["energy_wh"] == 300


def test_naive_and_aware_end_times_compare_with_naive_as_utc():
    # Text order puts the naive record (space sorts before "T") first, so a text
    # sort names the vehicle "Aware"; by instant the naive 02:00 UTC is newer.
    rows = [
        order("aware", "profile-a", 100, "Aware", endTime="2026-10-06T01:30:00+00:00"),
        order("naive", "profile-a", 200, "Naive", endTime="2026-10-06 02:00:00"),
    ]
    total = vehicle_totals(merge_orders({}, rows, SERIAL))[identity("profile-a")]
    assert total["name"] == "Naive"
    assert total["sessions"] == 2


def test_no_current_selection_or_private_fields_are_used():
    ledger = merge_orders(
        {}, [order(userId="secret-user", cardId="secret-card")], SERIAL
    )
    text = repr(ledger)
    for secret in (SERIAL, "secret-user", "secret-card", "profile-a", "order-a"):
        assert secret not in text
    assert vehicle_totals(ledger)[identity("profile-a")]["energy_wh"] == 16004


def test_active_order_is_not_added_and_duplicates_are_idempotent():
    assert merge_orders({}, [order(endTime="")], SERIAL) == {}
    ledger = merge_orders({}, [order(), order()], SERIAL)
    assert len(ledger) == 1
    with pytest.raises(ValueError, match="conflicting"):
        merge_orders({}, [order(), order(energy=12)], SERIAL)


@pytest.mark.parametrize(
    "kw",
    [
        {"chargedEnergy": None},
        {"chargedEnergy": -1},
        {"chargedEnergy": True},
        {"chargedEnergy": 1.5},
        {"vehicleId": None},
        {"vehicleId": ""},
        {"orderId": None},
        {"endTime": "invalid"},
        {"sn": "C371OTHER"},
        {"vehicleName": {}},
    ],
)
def test_malformed_record_fails_atomically(kw):
    old = merge_orders({}, [order()], SERIAL)
    with pytest.raises(ValueError):
        merge_orders(old, [order("new"), order("bad", **kw)], SERIAL)
    assert len(old) == 1


def response(rows, total, more=False, code="0", status=200):
    resp = AsyncMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "code": code,
        "data": {"content": rows, "total": total, "hasNext": more},
    }
    context = AsyncMock()
    context.__aenter__.return_value = resp
    return context


@pytest.mark.asyncio
async def test_paging_and_auth_retry():
    session = MagicMock()
    session.get.side_effect = [
        response([], 0, code="401", status=401),
        response([order()], 2, True),
        response([order("b")], 2),
    ]
    api = AppApiClient(session, "test@example.com", "test_password")
    api._token = "expired"
    api.login = AsyncMock(return_value=True)
    rows = await api.get_powerpulse_orders(SERIAL)
    assert len(rows) == 2
    assert [c.kwargs["params"]["page"] for c in session.get.call_args_list] == [1, 1, 2]
    api.login.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [response([], 1)],
        [response([order()], 2, True), response([order()], 2, True)],
        [response([order()], 2, True), response([order("b")], 3)],
        [response([order()], 2, True), response([], 2)],
        [response([], 0, code="500")],
    ],
)
async def test_partial_or_rejected_history_is_not_empty_history(pages):
    session = MagicMock()
    session.get.side_effect = pages
    api = AppApiClient(session, "test@example.com", "test_password")
    api._token = "test_password"
    with pytest.raises(ValueError):
        await api.get_powerpulse_orders(SERIAL)


def test_invalid_field_logging_is_sanitized(caplog):
    import logging

    with caplog.at_level(logging.DEBUG), pytest.raises(ValueError):
        merge_orders({}, [order(chargedEnergy="private-invalid-value")], SERIAL)
    assert "chargedEnergy" in caplog.text
    assert "private-invalid-value" not in caplog.text
    assert SERIAL not in caplog.text
    assert "Family EV" not in caplog.text


@pytest.mark.asyncio
async def test_history_login_failure_backs_off_for_all_chargers():
    from unittest.mock import patch

    from ecoflow_energy.ecoflow.app_api import HistoryLoginError

    session = MagicMock()
    api = AppApiClient(session, "test@example.com", "test_password")
    api.login = AsyncMock(return_value=False)
    with (
        patch("ecoflow_energy.ecoflow.app_api.time.time", return_value=100),
        pytest.raises(HistoryLoginError),
    ):
        await api.get_powerpulse_orders(SERIAL)
    with (
        patch("ecoflow_energy.ecoflow.app_api.time.time", return_value=3699),
        pytest.raises(HistoryLoginError),
    ):
        await api.get_powerpulse_orders("C376TEST0002")
    api.login.assert_awaited_once()
    session.get.assert_not_called()
    api.login.return_value = True
    session.get.return_value = response([], 0)
    with patch("ecoflow_energy.ecoflow.app_api.time.time", return_value=3700):
        assert await api.get_powerpulse_orders(SERIAL) == []
    assert api.login.await_count == 2


@pytest.mark.asyncio
async def test_concurrent_chargers_share_one_history_login():
    import asyncio

    session = MagicMock()
    session.get.return_value = response([], 0)
    api = AppApiClient(session, "test@example.com", "test_password")

    async def login():
        await asyncio.sleep(0)
        api._token = "test_token"
        return True

    api.login = AsyncMock(side_effect=login)
    assert await asyncio.gather(
        api.get_powerpulse_orders(SERIAL), api.get_powerpulse_orders("C376TEST0002")
    ) == [[], []]
    api.login.assert_awaited_once()


@pytest.mark.asyncio
async def test_rejected_refreshed_session_backs_off():
    from ecoflow_energy.ecoflow.app_api import HistoryLoginError

    session = MagicMock()
    session.get.return_value = response([], 0, code="401", status=401)
    api = AppApiClient(session, "test@example.com", "test_password")
    api._token = "expired"
    api.login = AsyncMock(return_value=True)
    with pytest.raises(HistoryLoginError):
        await api.get_powerpulse_orders(SERIAL)
    with pytest.raises(HistoryLoginError):
        await api.get_powerpulse_orders(SERIAL)
    api.login.assert_awaited_once()
    assert session.get.call_count == 2


def test_newest_nonempty_name_wins_independent_of_input_order():
    older = order("old", name="Old", endTime="2026-10-01 10:00:00")
    newer = order("new", name="New", endTime="2026-10-02 10:00:00")
    blank = order("blank", name="", endTime="2026-10-03 10:00:00")
    for rows in ([newer, older, blank], [blank, older, newer]):
        assert (
            vehicle_totals(merge_orders({}, rows, SERIAL))[identity("profile-a")][
                "name"
            ]
            == "New"
        )


@pytest.mark.parametrize("end", [None, 0, "0", ""])
def test_unfinished_order_sentinels(end):
    assert merge_orders({}, [order(endTime=end)], SERIAL) == {}


@pytest.mark.parametrize("field", ["orderId", "vehicleId"])
@pytest.mark.parametrize("value", [True, False])
def test_boolean_identifiers_are_invalid(field, value):
    with pytest.raises(ValueError, match=field):
        merge_orders({}, [order(**{field: value})], SERIAL)


@pytest.mark.parametrize("status,code", [(401, "0"), (200, "401")])
async def test_auth_status_and_body_code_independently(status, code):
    session = MagicMock()
    session.get.side_effect = [
        response([], 0, code=code, status=status),
        response([], 0),
    ]
    api = AppApiClient(session, "test@example.com", "test_password")
    api._token = "expired"
    api.login = AsyncMock(return_value=True)
    assert await api.get_powerpulse_orders(SERIAL) == []
    api.login.assert_awaited_once()
    assert session.get.call_count == 2


async def test_changing_total_is_the_pagination_failure():
    session = MagicMock()
    session.get.side_effect = [response([order()], 2, True), response([order("b")], 3)]
    api = AppApiClient(session, "test@example.com", "test_password")
    api._token = "test_token"
    with pytest.raises(ValueError, match="changed during pagination"):
        await api.get_powerpulse_orders(SERIAL)
