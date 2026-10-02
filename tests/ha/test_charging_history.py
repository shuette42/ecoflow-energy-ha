"""HA persistence and entity lifecycle for per-profile energy."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.charging_history import (
    ChargingHistoryCoordinator,
    VehicleEnergySensor,
    async_setup_charging_history,
)
from custom_components.ecoflow_energy.const import DOMAIN
from custom_components.ecoflow_energy.ecoflow.app_api import AppApiClient
from custom_components.ecoflow_energy.ecoflow.charging_history import identity

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
    with patch.object(restored.store, "async_save", side_effect=OSError):
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
    "name, label", [("Example EV", "Example EV"), ("", "Unnamed vehicle")]
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
            label + " completed charging energy"
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
        Store(hass, 1, f"{DOMAIN}_charging_history_{identity(sn)}")
        for sn in [SERIAL, "C376TEST0002", "C376UNRELATED"]
    ]
    for store in stores:
        await store.async_save({"orders": {}, "vehicles": {}})
    await async_remove_entry(hass, config)
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
    assert setup.call_args_list[0].args[-1] is setup.call_args_list[1].args[-1]
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
