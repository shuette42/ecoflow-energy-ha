"""Coordinator for a three-phase PowerOcean read over local Modbus/TCP.

Read-only and credential-free: one poll every ``LOCAL_MODBUS_POLL_INTERVAL_S``,
no MQTT, no HTTP, no energy integration and no write path. The cloud
coordinator's mixins are deliberately not used, because they carry an MQTT
liveness head, cloud-only resolvers and an energy integrator, none of which
applies to a device that reports its own lifetime counters.

Availability follows the poll: failures 1 and 2 keep the last data, the third
in a row marks the device unavailable, and one success restores it. An entry
without credentials never starts a reauth.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ..const import (
    CONF_UNIT_ID,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    LOCAL_MODBUS_FAILURES_UNAVAILABLE,
    LOCAL_MODBUS_POLL_INTERVAL_S,
    LOCAL_MODBUS_TIMEOUT_S,
    POWEROCEAN_LOCAL_COUNTER_KEYS,
)
from ..ecoflow.const import device_log_tag, get_device_name
from ..ecoflow.modbus_local import (
    MODBUS_DEFAULT_PORT,
    ModbusLocalClient,
    ModbusLocalError,
)
from ..ecoflow.parsers.powerocean_modbus import (
    POLL_BLOCKS,
    SETUP_BLOCKS,
    parse_device_info,
    parse_registers,
)

_LOGGER = logging.getLogger(__name__)


class _BlockReader(Protocol):
    """The one call the coordinator needs from a Modbus client."""

    async def read_blocks(
        self, blocks: Sequence[tuple[int, int]]
    ) -> dict[int, bytes]: ...


class EcoFlowLocalModbusCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll one PowerOcean over Modbus/TCP and publish the decoded readings."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: dict[str, Any],
        client: _BlockReader | None = None,
    ) -> None:
        """Initialize the coordinator for the device named in ``device_info``."""
        self.device_sn: str = device_info["sn"]
        self.device_type: str = DEVICE_TYPE_POWEROCEAN
        product_name = device_info.get("product_name") or ""
        display_name = DEVICE_TYPE_DISPLAY_NAMES[DEVICE_TYPE_POWEROCEAN]
        self.device_name: str = (
            device_info.get("name")
            or get_device_name(product_name, self.device_sn)
            or display_name
        )
        self.product_name: str = (
            product_name
            or get_device_name(product_name, self.device_sn)
            or display_name
        )
        self._sw_version: str = device_info.get("sw_version", "")

        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data.get(CONF_PORT, MODBUS_DEFAULT_PORT)
        self.unit_id: int = entry.data.get(CONF_UNIT_ID, 1)
        self._client: _BlockReader = client or ModbusLocalClient(
            self.host,
            self.port,
            self.unit_id,
            timeout=LOCAL_MODBUS_TIMEOUT_S,
        )

        # Accumulated readings; a poll only overwrites the keys it delivered.
        self._device_data: dict[str, Any] = {}
        self._device_available: bool = True
        self._consecutive_failures: int = 0
        self.last_poll_ok: bool | None = None
        self.last_poll_time: datetime | None = None
        self.last_error: str | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"EcoFlow local {device_log_tag(self.device_sn)}",
            config_entry=entry,
            update_interval=timedelta(seconds=LOCAL_MODBUS_POLL_INTERVAL_S),
        )

    # ------------------------------------------------------------------
    # Surface the sensor platform reads
    # ------------------------------------------------------------------

    @property
    def device_tag(self) -> str:
        """Return the one-way log tag for this device."""
        return device_log_tag(self.device_sn)

    @property
    def device_data(self) -> dict[str, Any]:
        """Return the accumulated device readings."""
        return self._device_data

    @property
    def device_available(self) -> bool:
        """Return False after repeated failed polls."""
        return self._device_available

    @property
    def consecutive_failures(self) -> int:
        """Return the number of failed polls in a row."""
        return self._consecutive_failures

    @property
    def enhanced_mode(self) -> bool:
        """Local mode is neither Standard nor Enhanced."""
        return False

    @property
    def connection_mode(self) -> str:
        """Return the value of the connection mode diagnostic sensor."""
        return "local"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info; identifiers match the cloud device exactly."""
        info = DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            manufacturer="EcoFlow",
            model=self.product_name,
            name=f"EcoFlow {self.device_name}",
        )
        if self._sw_version:
            info["sw_version"] = self._sw_version
        return info

    def seed_energy_total(self, key: str, total_kwh: float) -> None:
        """Accept a restored total and ignore it: nothing is integrated here."""

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Read the register blocks and fold them into the accumulated data."""
        # Identity blocks are read once, until the firmware version is known.
        blocks = POLL_BLOCKS if self._sw_version else SETUP_BLOCKS + POLL_BLOCKS
        try:
            raw = await self._client.read_blocks(blocks)
        except ModbusLocalError as err:
            return self._handle_failure(err)

        self._handle_success()
        if not self._sw_version:
            self._sw_version = parse_device_info(raw).get("firmware", "")
        parsed = parse_registers(raw)
        self._hold_counters(parsed)
        self._device_data.update(parsed)
        return dict(self._device_data)

    def _handle_failure(self, err: ModbusLocalError) -> dict[str, Any]:
        """Count a failed poll; mark the device unavailable on the third."""
        self._consecutive_failures += 1
        self.last_poll_ok = False
        self.last_poll_time = dt_util.utcnow()
        self.last_error = f"{type(err).__name__}: {err}"

        if not self._device_data:
            # Never delivered anything: HA turns this into ConfigEntryNotReady
            # on the first refresh and retries with its own backoff.
            raise UpdateFailed(
                f"Local Modbus device {self.device_tag} not reachable: {err}"
            ) from err

        if self._consecutive_failures < LOCAL_MODBUS_FAILURES_UNAVAILABLE:
            _LOGGER.debug(
                "Local Modbus poll failed for %s (%d in a row): %s",
                self.device_tag,
                self._consecutive_failures,
                err,
            )
        elif self._device_available:
            self._device_available = False
            _LOGGER.warning(
                "Local Modbus device %s is not answering (%s). Check that Modbus "
                "is enabled on the device (EcoFlow support enables it) and that "
                "no other Modbus client holds its single connection",
                self.device_tag,
                err,
            )
        else:
            _LOGGER.debug(
                "Local Modbus device %s still not answering (%d in a row): %s",
                self.device_tag,
                self._consecutive_failures,
                err,
            )
        # Keep serving the last data: before the third failure so entities do
        # not flicker, after it because availability alone carries the state.
        return dict(self._device_data)

    def _handle_success(self) -> None:
        """Reset the failure count and restore availability."""
        recovered = not self._device_available
        self._consecutive_failures = 0
        self._device_available = True
        self.last_poll_ok = True
        self.last_poll_time = dt_util.utcnow()
        self.last_error = None
        if recovered:
            _LOGGER.info("Local Modbus device %s is answering again", self.device_tag)

    def _hold_counters(self, parsed: dict[str, Any]) -> None:
        """Drop a counter reading that is lower than the last published one.

        A lifetime counter only grows. A lower value is a bad read, and
        publishing it would register as a meter reset in the statistics.
        """
        for key in POWEROCEAN_LOCAL_COUNTER_KEYS:
            new = parsed.get(key)
            last = self._device_data.get(key)
            if new is None or last is None or new >= last:
                continue
            _LOGGER.debug(
                "Local Modbus %s: ignoring %s reading %s below last value %s",
                self.device_tag,
                key,
                new,
                last,
            )
            del parsed[key]
