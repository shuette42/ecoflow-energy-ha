"""HA persistence and entity lifecycle for per-profile energy."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.charging_history import (
    ChargingHistoryCoordinator,
    VehicleEnergySensor,
    async_register_history_stores,
    async_remove_history_stores,
    async_setup_charging_history,
    history_limits_store,
)
from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.ecoflow.app_api import (
    AppApiClient,
    HistoryDeferred,
)
from custom_components.ecoflow_energy.ecoflow.charging_history import identity
from custom_components.ecoflow_energy.ecoflow.const import device_log_tag

from .conftest import add_entities_collector

SERIAL = "C371TEST0001"


def record(oid="a", vid="profile-a", energy=16004, name="Example EV"):
    return {
        "sn": SERIAL,
        "orderId": oid,
        "vehicleId": vid,
        "vehicleName": name,
        "chargedEnergy": energy,
        "endTime": "2026-10-02 12:00:00",
    }


def entry(hass):
    config = MockConfigEntry(
        domain=DOMAIN, data={"email": "test@example.com", "password": "test_password"}
    )
    config.add_to_hass(hass)
    return config


def api(hass):
    return AppApiClient(
        async_get_clientsession(hass), "test@example.com", "test_password"
    )


async def test_restart_does_not_double_count_and_save_failure_is_atomic(
    hass: HomeAssistant,
):
    config = entry(hass)
    coord = ChargingHistoryCoordinator(hass, config, SERIAL, api(hass))
    coord.api.get_powerpulse_orders = AsyncMock(return_value=[record()])
    await coord.async_refresh()
    assert coord.data[identity("profile-a")]["energy_wh"] == 16004
    restored = ChargingHistoryCoordinator(hass, config, SERIAL, api(hass))
    await restored.async_restore()
    restored.api.get_powerpulse_orders = AsyncMock(return_value=[record()])
    await restored.async_refresh()
    assert restored.data == coord.data
    restored.api.get_powerpulse_orders.return_value = [
        record(),
        record("b", "profile-b", 2000),
    ]
    with patch.object(
        restored.store, "_async_write_data", side_effect=WriteError("disk full")
    ):
        await restored.async_refresh()
    assert not restored.last_update_success
    assert restored.data == coord.data
    assert restored.orders == coord.orders
    await restored.async_refresh()
    assert restored.data[identity("profile-b")]["energy_wh"] == 2000
    await coord.async_shutdown()
    await restored.async_shutdown()


async def test_discovery_units_corrections_and_failure(hass: HomeAssistant):
    config = entry(hass)
    added: list[Entity] = []

    def add(new_entities: Iterable[Entity], update_before_add: bool = False) -> None:
        added.extend(new_entities)

    with patch(
        "custom_components.ecoflow_energy.charging_history.AppApiClient.get_powerpulse_orders",
        new_callable=AsyncMock,
        return_value=[record()],
    ):
        coord = await async_setup_charging_history(
            hass,
            config,
            SERIAL,
            DeviceInfo(identifiers={(DOMAIN, SERIAL)}),
            add,
            api(hass),
        )
        await hass.async_block_till_done(wait_background_tasks=True)
    sensor = added[0]
    assert isinstance(sensor, VehicleEnergySensor)
    assert sensor.native_value == 16.004
    assert sensor.state_class == "total"
    uid = sensor.unique_id
    coord.api.get_powerpulse_orders = AsyncMock(
        return_value=[record(energy=15000), record("b", "-1", 1000)]
    )
    await coord.async_refresh()
    assert len(added) == 2
    assert sensor.native_value == 15
    assert sensor.unique_id == uid
    assert isinstance(added[1], VehicleEnergySensor)
    assert added[1].translation_key == "other_vehicle_energy"
    coord.api.get_powerpulse_orders.side_effect = ValueError("incomplete")
    await coord.async_refresh()
    assert not sensor.available
    assert sensor.native_value == 15
    coord.api.get_powerpulse_orders.side_effect = None
    # A correction that moves the sole order off a vehicle clears its total.
    coord.api.get_powerpulse_orders.return_value = [record(vid="profile-b")]
    await coord.async_refresh()
    assert sensor.native_value == 0
    await coord.async_shutdown()


@pytest.mark.parametrize(
    "name, label", [("Example EV", "Example EV"), ("", "Unnamed Vehicle")]
)
async def test_registered_c371_setup_and_unload(hass: HomeAssistant, name, label):
    """Use the existing PowerPulse coordinator registration supplied by #446."""
    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "mode": "enhanced",
            "email": "test@example.com",
            "password": "test_password",
            "powerpulse_vehicle_energy": True,
            "devices": [
                {"sn": SERIAL, "product_name": "", "device_type": "powerpulse2"}
            ],
        },
    )
    config.add_to_hass(hass)
    with (
        patch(
            "custom_components.ecoflow_energy.charging_history.AppApiClient.get_powerpulse_orders",
            new_callable=AsyncMock,
            return_value=[record(name=name)],
        ) as read,
        patch(
            "custom_components.ecoflow_energy.EcoFlowDeviceCoordinator.async_setup",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        read.assert_awaited_once()
        states = [
            s
            for s in hass.states.async_all("sensor")
            if "completed_sessions" in s.attributes
        ]
        assert len(states) == 1
        assert states[0].state == "16.004"
        assert states[0].attributes["unit_of_measurement"] == "kWh"
        assert (
            "Wallbox Completed Charging Energy " + label
            in states[0].attributes["friendly_name"]
        )
        assert await hass.config_entries.async_unload(config.entry_id)
        await hass.async_block_till_done()


async def test_option_is_opt_in_and_persists(hass: HomeAssistant):
    from homeassistant.data_entry_flow import FlowResultType

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "mode": "enhanced",
            "email": "test@example.com",
            "password": "test_password",
            "devices": [{"sn": SERIAL, "product_name": "", "device_type": "unknown"}],
        },
    )
    config.add_to_hass(hass)
    with patch(
        "custom_components.ecoflow_energy.config_flow_options._async_fetch_app_devices",
        new_callable=AsyncMock,
        return_value=[],
    ):
        form = await hass.config_entries.options.async_init(config.entry_id)
        assert form["type"] is FlowResultType.FORM
        assert form["data_schema"] is not None
        schema = form["data_schema"].schema
        option = next(k for k in schema if k.schema == "powerpulse_vehicle_energy")
        assert option.default() is False
        saved = await hass.config_entries.options.async_configure(
            form["flow_id"],
            {
                "mode": "enhanced",
                "devices": [SERIAL],
                "powerpulse_vehicle_energy": True,
            },
        )
        assert saved["type"] is FlowResultType.CREATE_ENTRY
        assert config.data["powerpulse_vehicle_energy"] is True


@pytest.mark.parametrize(
    ("devices", "shown"),
    [
        ([(SERIAL, "powerpulse2")], True),
        ([("R351TEST0001", "delta2max")], False),
        ([], False),
    ],
)
async def test_charging_history_option_only_shown_for_powerpulse2(
    hass: HomeAssistant, devices, shown
):
    from homeassistant.data_entry_flow import FlowResultType

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "mode": "enhanced",
            "email": "test@example.com",
            "password": "test_password",
            "devices": [
                {"sn": sn, "product_name": "", "device_type": kind}
                for sn, kind in devices
            ],
        },
    )
    config.add_to_hass(hass)
    with patch(
        "custom_components.ecoflow_energy.config_flow_options._async_fetch_app_devices",
        new_callable=AsyncMock,
        return_value=[],
    ):
        form = await hass.config_entries.options.async_init(config.entry_id)
    assert form["type"] is FlowResultType.FORM
    schema = form["data_schema"]
    assert schema is not None
    keys = {k.schema for k in schema.schema}
    assert ("powerpulse_vehicle_energy" in keys) is shown
    # The other app-mode switch stays: only this option depends on the charger.
    assert "raw_capture" in keys


async def test_coordinator_name_includes_device_tag(hass: HomeAssistant):
    """HA prints the coordinator name in its own error lines: tag, never serial."""
    coord = ChargingHistoryCoordinator(hass, entry(hass), SERIAL, api(hass))
    assert device_log_tag(SERIAL) in coord.name
    assert SERIAL not in coord.name
    assert SERIAL[:8] not in coord.name
    await coord.async_shutdown()


async def test_cloud_retention_keeps_published_totals_after_restart(hass):
    config = entry(hass)
    client = api(hass)
    client.get_powerpulse_orders = AsyncMock(
        return_value=[record(), record("b", "profile-b", 2000)]
    )
    coord = ChargingHistoryCoordinator(hass, config, SERIAL, client)
    await coord.async_refresh()
    sensor = VehicleEnergySensor(
        coord, identity("profile-b"), DeviceInfo(identifiers={(DOMAIN, SERIAL)})
    )
    client.get_powerpulse_orders.return_value = [record()]
    await coord.async_refresh()
    assert sensor.native_value == 2
    restored = ChargingHistoryCoordinator(hass, config, SERIAL, client)
    await restored.async_restore()
    restored_sensor = VehicleEnergySensor(
        restored, identity("profile-b"), DeviceInfo(identifiers={(DOMAIN, SERIAL)})
    )
    assert restored_sensor.native_value == 2
    assert restored.data[identity("profile-b")]["sessions"] == 1
    await restored.async_refresh()
    assert restored_sensor.native_value == 2
    await coord.async_shutdown()
    await restored.async_shutdown()


@pytest.mark.parametrize(
    "saved",
    [
        [],
        {},
        {"orders": [], "vehicles": {}},
        {"orders": {"bad": {}}, "vehicles": {}},
        {
            "orders": {},
            "vehicles": {
                identity("a"): {
                    "name": "EV",
                    "energy_wh": -1,
                    "sessions": 1,
                    "other": False,
                }
            },
        },
    ],
)
async def test_invalid_saved_ledger_starts_empty(hass, caplog, saved):
    coord = ChargingHistoryCoordinator(hass, entry(hass), SERIAL, api(hass))
    with patch.object(coord.store, "async_load", return_value=saved):
        await coord.async_restore()
    assert coord.data == {}
    assert coord.orders == {}
    assert "Invalid stored charging ledger" in caplog.text
    assert SERIAL not in caplog.text
    await coord.async_shutdown()


async def test_restore_precedes_background_fetch(hass):
    config = entry(hass)
    client = api(hass)
    coord = ChargingHistoryCoordinator(hass, config, SERIAL, client)
    client.get_powerpulse_orders = AsyncMock(return_value=[record()])
    await coord.async_refresh()
    await coord.async_shutdown()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch(serial):
        started.set()
        await release.wait()
        return [record(energy=17000)]

    client.get_powerpulse_orders = AsyncMock(side_effect=slow_fetch)
    added: list[VehicleEnergySensor] = []
    restored = await asyncio.wait_for(
        async_setup_charging_history(
            hass,
            config,
            SERIAL,
            DeviceInfo(identifiers={(DOMAIN, SERIAL)}),
            add_entities_collector(added),
            client,
        ),
        1,
    )
    assert len(added) == 1
    assert added[0].native_value == 16.004
    await started.wait()
    assert added[0].native_value == 16.004
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert added[0].native_value == 17
    await restored.async_shutdown()


async def test_entry_removal_deletes_current_and_deselected_ledgers(hass):
    from homeassistant.helpers.storage import Store

    from custom_components.ecoflow_energy import async_remove_entry
    from custom_components.ecoflow_energy.charging_history import (
        async_register_history_stores,
    )

    config = entry(hass)
    await async_register_history_stores(hass, config, [SERIAL])
    await async_register_history_stores(hass, config, ["C376TEST0002"])
    stores: list[Store[dict[str, Any]]] = [
        Store(hass, 1, f"{DOMAIN}_charging_history_{config.entry_id}_{identity(sn)}")
        for sn in [SERIAL, "C376TEST0002", "C376UNRELATED"]
    ]
    for store in stores:
        await store.async_save({"orders": {}, "vehicles": {}})
    limits = history_limits_store(hass, config)
    await limits.async_save({"last_reads": {}, "retry_after": 1})
    await async_remove_entry(hass, config)
    assert await limits.async_load() is None
    assert await stores[0].async_load() is None
    assert await stores[1].async_load() is None
    assert await stores[2].async_load() is not None
    index: Store[list[str]] = Store(
        hass, 1, f"{DOMAIN}_charging_history_index_{config.entry_id}"
    )
    assert await index.async_load() is None


async def test_sensor_platform_shares_history_client(hass):
    from custom_components.ecoflow_energy import sensor
    from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "email": "test@example.com",
            "password": "test_password",
            "powerpulse_vehicle_energy": True,
        },
    )
    config.add_to_hass(hass)
    sources = {
        sn: EcoFlowDeviceCoordinator(
            hass,
            config,
            {"sn": sn, "device_type": "powerpulse2", "product_name": "PowerPulse 2"},
        )
        for sn in [SERIAL, "C376TEST0002"]
    }
    hass.data.setdefault(DOMAIN, {})[config.entry_id] = sources
    with patch(
        "custom_components.ecoflow_energy.charging_history.async_setup_charging_history",
        new_callable=AsyncMock,
    ) as setup:
        await sensor.async_setup_entry(hass, config, add_entities_collector([]))
    assert setup.await_count == 2
    assert setup.call_args_list[0].args[-1]._history_store is not None
    assert setup.call_args_list[0].args[-1] is setup.call_args_list[1].args[-1]
    for source in sources.values():
        await source.async_shutdown()


async def test_unconfirmed_index_skips_history_only_and_warns_with_tags(hass, caplog):
    """A failed index write must not take the entry's live sensors down.

    The real `async_register_history_stores` raises when the saved index
    cannot be read back; the sensor platform turns that into one warning that
    names the chargers by device tag and carries on without history sensors.
    """
    from custom_components.ecoflow_energy import sensor
    from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
    from custom_components.ecoflow_energy.ecoflow.const import device_log_tag

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "email": "test@example.com",
            "password": "test_password",
            "powerpulse_vehicle_energy": True,
        },
    )
    config.add_to_hass(hass)
    serials = [SERIAL, "C376TEST0002"]
    sources = {
        sn: EcoFlowDeviceCoordinator(
            hass,
            config,
            {"sn": sn, "device_type": "powerpulse2", "product_name": "PowerPulse 2"},
        )
        for sn in serials
    }
    hass.data.setdefault(DOMAIN, {})[config.entry_id] = sources
    added: list[Any] = []
    with (
        caplog.at_level(logging.WARNING),
        patch.object(Store, "_async_write_data", side_effect=WriteError("disk full")),
        patch(
            "custom_components.ecoflow_energy.charging_history.async_setup_charging_history",
            new_callable=AsyncMock,
        ) as setup,
    ):
        await sensor.async_setup_entry(hass, config, add_entities_collector(added))
    assert added, "the live sensors of the entry are still created"
    setup.assert_not_awaited()
    warnings = [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.endswith(".sensor")
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "index write could not be verified" in message
    for sn in serials:
        assert device_log_tag(sn) in message
        assert sn not in caplog.text
    for source in sources.values():
        await source.async_shutdown()


async def test_entry_unload_cancels_initial_history_fetch(hass):
    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "mode": "enhanced",
            "email": "test@example.com",
            "password": "test_password",
            "powerpulse_vehicle_energy": True,
            "devices": [
                {"sn": SERIAL, "product_name": "", "device_type": "powerpulse2"}
            ],
        },
    )
    config.add_to_hass(hass)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_fetch(serial):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with (
        patch(
            "custom_components.ecoflow_energy.charging_history.AppApiClient.get_powerpulse_orders",
            side_effect=slow_fetch,
        ),
        patch(
            "custom_components.ecoflow_energy.EcoFlowDeviceCoordinator.async_setup",
            new_callable=AsyncMock,
        ),
    ):
        assert await hass.config_entries.async_setup(config.entry_id)
        await started.wait()
        assert await hass.config_entries.async_unload(config.entry_id)
        await asyncio.wait_for(cancelled.wait(), 1)


@pytest.mark.parametrize("enabled", [None, False])
async def test_absent_or_false_option_never_sets_up_or_fetches_history(hass, enabled):
    from custom_components.ecoflow_energy import sensor
    from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

    data = {
        "auth_method": "app",
        "email": "test@example.com",
        "password": "test_password",
    }
    if enabled is not None:
        data["powerpulse_vehicle_energy"] = enabled
    config = MockConfigEntry(domain=DOMAIN, data=data)
    config.add_to_hass(hass)
    source = EcoFlowDeviceCoordinator(
        hass, config, {"sn": SERIAL, "device_type": "powerpulse2"}
    )
    hass.data.setdefault(DOMAIN, {})[config.entry_id] = {SERIAL: source}
    with (
        patch(
            "custom_components.ecoflow_energy.charging_history.async_setup_charging_history",
            new_callable=AsyncMock,
        ) as setup,
        patch.object(
            AppApiClient, "get_powerpulse_orders", new_callable=AsyncMock
        ) as fetch,
    ):
        await sensor.async_setup_entry(hass, config, add_entities_collector([]))
        await hass.async_block_till_done(wait_background_tasks=True)
    setup.assert_not_awaited()
    fetch.assert_not_awaited()
    await source.async_shutdown()


async def test_history_selection_uses_device_type_only(hass):
    from custom_components.ecoflow_energy import sensor
    from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "email": "test@example.com",
            "password": "test_password",
            "powerpulse_vehicle_energy": True,
        },
    )
    config.add_to_hass(hass)
    sources = {
        sn: EcoFlowDeviceCoordinator(hass, config, {"sn": sn, "device_type": kind})
        for sn, kind in [
            ("C376TEST0002", "powerpulse2"),
            ("C371TEST0001", "powerpulse2"),
            (SERIAL, "unknown"),
            ("R351TEST0001", "delta2max"),
        ]
    }
    hass.data.setdefault(DOMAIN, {})[config.entry_id] = sources
    with patch(
        "custom_components.ecoflow_energy.charging_history.async_setup_charging_history",
        new_callable=AsyncMock,
    ) as setup:
        await sensor.async_setup_entry(hass, config, add_entities_collector([]))
    assert sorted([call.args[2] for call in setup.call_args_list]) == sorted(
        ["C376TEST0002", "C371TEST0001"]
    )
    for source in sources.values():
        await source.async_shutdown()


async def test_profile_rename_keeps_identity_and_creates_no_entity(hass):
    config = entry(hass)
    client = api(hass)
    client.get_powerpulse_orders = AsyncMock(return_value=[record(name="Before")])
    added: list[VehicleEnergySensor] = []
    coord = await async_setup_charging_history(
        hass,
        config,
        SERIAL,
        DeviceInfo(identifiers={(DOMAIN, SERIAL)}),
        add_entities_collector(added),
        client,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    uid = added[0].unique_id
    client.get_powerpulse_orders.return_value = [record(name="After")]
    await coord.async_refresh()
    assert len(added) == 1
    assert added[0].unique_id == uid
    assert added[0].extra_state_attributes["profile_name"] == "After"
    # Recreating the entity catches implementations that use its discovery name.
    restored = ChargingHistoryCoordinator(hass, config, SERIAL, client)
    await restored.async_restore()
    after = VehicleEnergySensor(
        restored, identity("profile-a"), DeviceInfo(identifiers={(DOMAIN, SERIAL)})
    )
    assert after.unique_id == uid
    await coord.async_shutdown()
    await restored.async_shutdown()


@pytest.mark.parametrize(
    "corruption",
    [
        "energy",
        "ended",
        "totals",
        "name_none",
        "name_missing",
        "other_string",
        "other_missing",
        "missing_vehicle",
        "vehicle_list",
        "vehicle_name_missing",
        "vehicle_name_none",
        "sessions_bool",
        "energy_bool",
        "vehicle_bad_key",
    ],
)
async def test_corrupt_saved_order_or_inconsistent_totals_refused(
    hass, caplog, corruption
):
    from custom_components.ecoflow_energy.ecoflow.charging_history import (
        merge_orders,
        vehicle_totals,
    )

    orders = merge_orders({}, [record()], SERIAL)
    saved = {"orders": orders, "vehicles": vehicle_totals(orders)}
    if corruption == "energy":
        orders[identity("a")]["energy_wh"] = "16004"
    elif corruption == "ended":
        orders[identity("a")]["ended"] = "private-invalid-date"
    elif corruption == "name_none":
        orders[identity("a")]["name"] = None
    elif corruption == "name_missing":
        del orders[identity("a")]["name"]
    elif corruption == "other_string":
        saved["vehicles"][identity("profile-a")]["other"] = "yes"
    elif corruption == "other_missing":
        del saved["vehicles"][identity("profile-a")]["other"]
    elif corruption == "missing_vehicle":
        saved["vehicles"] = {}
    elif corruption == "vehicle_list":
        orders[identity("a")]["vehicle"] = []
    elif corruption == "vehicle_name_missing":
        del saved["vehicles"][identity("profile-a")]["name"]
    elif corruption == "vehicle_name_none":
        saved["vehicles"][identity("profile-a")]["name"] = None
    elif corruption in {"sessions_bool", "energy_bool"}:
        orders[identity("a")]["energy_wh"] = 1
        saved["vehicles"] = vehicle_totals(orders)
        field = "sessions" if corruption == "sessions_bool" else "energy_wh"
        saved["vehicles"][identity("profile-a")][field] = True
    elif corruption == "vehicle_bad_key":
        saved["vehicles"]["bad"] = {
            **saved["vehicles"][identity("profile-a")],
            "energy_wh": 0,
            "sessions": 0,
        }
    else:
        saved["vehicles"][identity("profile-a")]["energy_wh"] += 1
    coord = ChargingHistoryCoordinator(hass, entry(hass), SERIAL, api(hass))
    with patch.object(coord.store, "async_load", return_value=saved):
        await coord.async_restore()
    assert coord.orders == {}
    assert coord.data == {}
    assert "Invalid stored charging ledger; starting empty" in caplog.text
    from custom_components.ecoflow_energy.ecoflow.const import device_log_tag

    assert device_log_tag(SERIAL) in caplog.text
    assert "private-invalid-date" not in caplog.text
    await coord.async_shutdown()


@pytest.mark.parametrize(
    "mode,submitted,expected",
    [("standard", True, False), ("enhanced", False, False), ("enhanced", None, True)],
)
async def test_options_disable_and_preserve_history(hass, mode, submitted, expected):
    from custom_components.ecoflow_energy.config_flow import EcoFlowOptionsFlow

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "app",
            "mode": "enhanced",
            "email": "test@example.com",
            "password": "test_password",
            "powerpulse_vehicle_energy": True,
            "devices": [],
        },
    )
    config.add_to_hass(hass)
    flow = EcoFlowOptionsFlow()
    flow.hass = hass
    flow.handler = config.entry_id
    flow._pending_vehicle_energy = submitted
    # Exercise the options save itself; no device discovery or credentials needed.
    flow._save_options(mode, [])
    assert config.data["powerpulse_vehicle_energy"] is expected


async def test_history_read_deadline_survives_new_setup(hass, caplog):
    from unittest.mock import MagicMock

    from custom_components.ecoflow_energy.charging_history import history_limits_store
    from tests.test_charging_history import order, response

    config = entry(hass)
    session = MagicMock()
    session.get.return_value = response([order()], 1)

    def client():
        result = AppApiClient(
            session,
            "test@example.com",
            "test_password",
            history_store=history_limits_store(hass, config),
        )
        result._token = "test_token"
        return result

    first = ChargingHistoryCoordinator(hass, config, SERIAL, client())
    with patch(
        "custom_components.ecoflow_energy.ecoflow.app_api.time.time", return_value=10000
    ):
        await first.async_refresh()
    second = ChargingHistoryCoordinator(hass, config, SERIAL, client())
    await second.async_restore()
    sensor = VehicleEnergySensor(
        second, identity("profile-a"), DeviceInfo(identifiers={(DOMAIN, SERIAL)})
    )
    caplog.clear()
    with patch(
        "custom_components.ecoflow_energy.ecoflow.app_api.time.time", return_value=10299
    ):
        await second.async_refresh()
    assert session.get.call_count == 1
    assert second.data == first.data
    assert second.history_available
    assert sensor.available
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert second.update_interval is not None
    assert second.update_interval.total_seconds() == 1
    with patch(
        "custom_components.ecoflow_energy.ecoflow.app_api.time.time", return_value=10300
    ):
        await second.async_refresh()
    assert session.get.call_count == 2
    assert second.update_interval is not None
    assert second.update_interval.total_seconds() == 300
    await first.async_shutdown()
    await second.async_shutdown()


async def test_auth_backoff_survives_new_setup(hass):
    from custom_components.ecoflow_energy.charging_history import history_limits_store

    config = entry(hass)
    first_client = AppApiClient(
        async_get_clientsession(hass),
        "test@example.com",
        "test_password",
        history_store=history_limits_store(hass, config),
    )
    first_client.login = AsyncMock(return_value=False)
    first = ChargingHistoryCoordinator(hass, config, SERIAL, first_client)
    with patch(
        "custom_components.ecoflow_energy.ecoflow.app_api.time.time", return_value=10000
    ):
        await first.async_refresh()
    second_client = AppApiClient(
        async_get_clientsession(hass),
        "test@example.com",
        "test_password",
        history_store=history_limits_store(hass, config),
    )
    second_client.login = AsyncMock(return_value=False)
    second = ChargingHistoryCoordinator(hass, config, SERIAL, second_client)
    with patch(
        "custom_components.ecoflow_energy.ecoflow.app_api.time.time", return_value=13599
    ):
        await second.async_refresh()
    second_client.login.assert_not_awaited()
    assert not second.history_available
    with patch(
        "custom_components.ecoflow_energy.ecoflow.app_api.time.time", return_value=13600
    ):
        await second.async_refresh()
    second_client.login.assert_awaited_once()
    await first.async_shutdown()
    await second.async_shutdown()


async def test_developer_auth_never_constructs_history_client_or_registers_store(hass):
    from custom_components.ecoflow_energy import sensor
    from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator

    config = MockConfigEntry(
        domain=DOMAIN,
        data={
            "auth_method": "developer",
            "powerpulse_vehicle_energy": True,
            "email": "test@example.com",
            "password": "test_password",
        },
    )
    config.add_to_hass(hass)
    source = EcoFlowDeviceCoordinator(
        hass, config, {"sn": SERIAL, "device_type": "powerpulse2"}
    )
    hass.data.setdefault(DOMAIN, {})[config.entry_id] = {SERIAL: source}
    with (
        patch(
            "custom_components.ecoflow_energy.ecoflow.app_api.AppApiClient"
        ) as client,
        patch(
            "custom_components.ecoflow_energy.charging_history.async_register_history_stores"
        ) as register,
    ):
        await sensor.async_setup_entry(hass, config, add_entities_collector([]))
    client.assert_not_called()
    register.assert_not_called()
    await source.async_shutdown()


@pytest.mark.parametrize("phase", ["before_request", "after_login"])
async def test_silent_limits_write_failure_fails_closed(hass, phase):
    config = entry(hass)
    limits = history_limits_store(hass, config)
    client = api(hass)
    client._history_store = limits
    client.login = AsyncMock(return_value=False)
    coord = ChargingHistoryCoordinator(hass, config, SERIAL, client)
    original = limits._async_write_data

    async def write(data):
        if phase == "before_request" or data["data"]["retry_after"]:
            raise WriteError("disk full")
        await original(data)

    with patch.object(limits, "_async_write_data", side_effect=write):
        await coord.async_refresh()
    assert not coord.last_update_success
    assert coord.orders == coord.data == {}
    assert await coord.store.async_load() is None
    assert client.login.await_count == (phase == "after_login")
    await coord.async_shutdown()


@pytest.mark.parametrize(
    "saved,delay",
    [
        ({"last_reads": {identity(SERIAL): 1e300}, "retry_after": 0}, 300),
        ({"last_reads": {}, "retry_after": 1e300}, 3600),
        ({"last_reads": {}, "retry_after": -1}, 3600),
    ],
)
async def test_future_or_corrupt_limits_heal_and_expire(hass, saved, delay):
    config = entry(hass)
    limits = history_limits_store(hass, config)
    await limits.async_save(saved)
    client = api(hass)
    client._history_store = limits
    client._get_powerpulse_orders = AsyncMock(return_value=[])
    coord = ChargingHistoryCoordinator(hass, config, SERIAL, client)
    clock = "custom_components.ecoflow_energy.ecoflow.app_api.time.time"
    with patch(clock, return_value=10000):
        await coord.async_refresh()
    client._get_powerpulse_orders.assert_not_awaited()
    assert coord.update_interval is not None
    assert coord.update_interval.total_seconds() == delay
    # A new client must also honor the healed on-disk deadline, not extend it.
    client = api(hass)
    client._history_store = limits
    client._get_powerpulse_orders = AsyncMock(return_value=[])
    coord.api = client
    with patch(clock, return_value=10000 + delay):
        await coord.async_refresh()
    client._get_powerpulse_orders.assert_awaited_once()
    assert coord.history_available
    await coord.async_shutdown()


async def test_ledger_load_error_preserves_unread_store(hass, caplog):
    client = api(hass)
    client.get_powerpulse_orders = AsyncMock(return_value=[record()])
    with (
        patch.object(
            Store,
            "async_load",
            side_effect=HomeAssistantError("sentinel-7f3a-load-detail"),
        ),
        patch.object(Store, "async_save") as save,
    ):
        coord = await async_setup_charging_history(
            hass,
            entry(hass),
            SERIAL,
            DeviceInfo(identifiers={(DOMAIN, SERIAL)}),
            add_entities_collector([]),
            client,
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        await coord.async_refresh()
    save.assert_not_called()
    client.get_powerpulse_orders.assert_not_awaited()
    assert not coord.last_update_success
    assert "Could not restore charging ledger" in caplog.text
    assert "sentinel-7f3a-load-detail" not in caplog.text
    await coord.async_shutdown()


async def test_index_load_error_does_not_overwrite_and_removal_cleans_known_stores(
    hass, caplog
):
    config = MockConfigEntry(domain=DOMAIN, data={"devices": [{"sn": SERIAL}]})
    config.add_to_hass(hass)
    coord = ChargingHistoryCoordinator(hass, config, SERIAL, api(hass))
    await coord.store.async_save({"orders": {}, "vehicles": {}})
    limits = history_limits_store(hass, config)
    await limits.async_save({"last_reads": {}, "retry_after": 100})
    with (
        patch.object(
            Store,
            "async_load",
            side_effect=HomeAssistantError("sentinel-7f3a-load-detail"),
        ),
        patch.object(Store, "async_save") as save,
    ):
        await async_register_history_stores(hass, config, [SERIAL])
        await async_remove_history_stores(hass, config)
    save.assert_not_called()
    assert await limits.async_load() is None
    assert await coord.store.async_load() is None
    assert "only selected ledgers" in caplog.text
    await coord.async_shutdown()


async def test_same_serial_ledgers_are_isolated_by_entry(hass):
    configs = [entry(hass), entry(hass)]
    coords = [
        ChargingHistoryCoordinator(hass, config, SERIAL, api(hass))
        for config in configs
    ]
    for coord, energy in zip(coords, [1000, 2000], strict=True):
        coord.api.get_powerpulse_orders = AsyncMock(
            return_value=[record(energy=energy)]
        )
        await coord.async_refresh()
        assert coord.config_entry is not None
        await async_register_history_stores(hass, coord.config_entry, [SERIAL])
    index: Store[list[str]] = Store(
        hass, 1, f"{DOMAIN}_charging_history_index_{configs[0].entry_id}"
    )
    assert await index.async_load() == [identity(SERIAL)]
    assert coords[0].store.key != coords[1].store.key
    await async_remove_history_stores(hass, configs[0])
    removed: Store[dict[str, Any]] = Store(hass, 1, coords[0].store.key)
    assert await removed.async_load() is None
    restored = ChargingHistoryCoordinator(hass, configs[1], SERIAL, api(hass))
    await restored.async_restore()
    assert restored.data[identity("profile-a")]["energy_wh"] == 2000
    for coord in [*coords, restored]:
        await coord.async_shutdown()


@pytest.mark.parametrize(
    "failure", [ValueError("sentinel-7f3a-load-detail"), HistoryDeferred(1e300, True)]
)
async def test_read_failures_warn_once_and_recover_without_error_logs(
    hass, caplog, failure
):
    coord = ChargingHistoryCoordinator(hass, entry(hass), SERIAL, api(hass))
    coord.api.get_powerpulse_orders = AsyncMock(return_value=[record()])
    await coord.async_refresh()
    sensor = VehicleEnergySensor(
        coord, identity("profile-a"), DeviceInfo(identifiers={(DOMAIN, SERIAL)})
    )
    caplog.clear()
    history_logger = "custom_components.ecoflow_energy.charging_history"
    caplog.set_level(logging.INFO, logger=history_logger)
    coord.api.get_powerpulse_orders.side_effect = failure
    await coord.async_refresh()
    await coord.async_refresh()
    assert not sensor.available
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]

    def recoveries():
        return [
            r
            for r in caplog.records
            if r.levelno == logging.INFO and r.name == history_logger
        ]

    assert not recoveries()
    assert len(warnings) == 1
    assert device_log_tag(SERIAL) in warnings[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert SERIAL not in caplog.text
    assert "sentinel-7f3a-load-detail" not in caplog.text
    assert coord.update_interval is not None
    assert coord.update_interval.total_seconds() <= 3600
    coord.api.get_powerpulse_orders.side_effect = None
    await coord.async_refresh()
    assert sensor.available
    # The recovery is announced once, names the charger by tag, and a further
    # successful read is silent.
    await coord.async_refresh()
    assert len(recoveries()) == 1
    assert device_log_tag(SERIAL) in recoveries()[0].getMessage()
    assert SERIAL not in recoveries()[0].getMessage()
    coord.api.get_powerpulse_orders.side_effect = failure
    await coord.async_refresh()
    assert not sensor.available
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    await coord.async_shutdown()


async def test_restore_retries_read_before_fetch_or_save(hass):
    from custom_components.ecoflow_energy.ecoflow.charging_history import (
        merge_orders,
        vehicle_totals,
    )

    orders = merge_orders({}, [record()], SERIAL)
    saved = {"orders": orders, "vehicles": vehicle_totals(orders)}
    coord = ChargingHistoryCoordinator(hass, entry(hass), SERIAL, api(hass))
    coord._restore_failed = True
    coord.api.get_powerpulse_orders = AsyncMock(return_value=[])
    with (
        patch.object(
            coord.store,
            "async_load",
            side_effect=[HomeAssistantError("sentinel-7f3a-load-detail"), saved],
        ),
        patch.object(coord.store, "async_save") as save,
    ):
        await coord.async_refresh()
        assert not coord.last_update_success
        save.assert_not_called()
        coord.api.get_powerpulse_orders.assert_not_awaited()
        await coord.async_refresh()
        assert coord.last_update_success
        assert coord.history_available
        assert not coord._restore_failed
        assert coord.data[identity("profile-a")]["energy_wh"] == 16004
        save.assert_not_called()
    await coord.async_shutdown()


async def test_index_silent_write_failure_is_detected(hass):
    config = entry(hass)
    with (
        patch.object(Store, "_async_write_data", side_effect=WriteError("disk full")),
        pytest.raises(OSError, match="index write could not be verified"),
    ):
        await async_register_history_stores(hass, config, [SERIAL])
