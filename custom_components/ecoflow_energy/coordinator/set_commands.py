"""SET command dispatch for the EcoFlow device coordinator."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..const import (
    AUTH_METHOD_APP,
    POWEROCEAN_SOC_DEBOUNCE_S,
)

_LOGGER = logging.getLogger(__name__)


class SetCommandsMixin:
    """Mixin providing SET command dispatch and SoC debounce."""

    # ------------------------------------------------------------------
    # SET commands (switches, numbers)
    # ------------------------------------------------------------------

    async def async_set_soc_limits(
        self, max_charge_soc: int, min_discharge_soc: int,
    ) -> bool:
        """Send SoC limits to PowerOcean via WSS Protobuf (Enhanced Mode only).

        Sends a SysBatChgDsgSet message (cmd_func=96, cmd_id=112) with
        2 fields: charge upper limit and discharge lower limit.
        """
        if not self._enhanced_mode:
            _LOGGER.warning("SoC limit SET requires Enhanced Mode (%s)", self.device_sn)
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send SoC limits - MQTT not connected (%s)", self.device_sn)
            return False

        from ..ecoflow.energy_stream import build_soc_limit_set_payload

        payload = build_soc_limit_set_payload(max_charge_soc, min_discharge_soc)
        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        if ok:
            _LOGGER.debug(
                "SoC limits sent: max=%d, min=%d (%s)",
                max_charge_soc, min_discharge_soc, self.device_sn,
            )
            self._log_event("set_soc_limits", f"max={max_charge_soc}, min={min_discharge_soc}")
        else:
            _LOGGER.warning("SoC limits SET failed (%s)", self.device_sn)
            self._log_event("set_soc_limits_fail", f"max={max_charge_soc}, min={min_discharge_soc}")
        return ok

    async def async_set_powerocean_soc_debounced(
        self, backup_reserve_pct: int, solar_surplus_pct: int,
    ) -> bool:
        """Coalesce rapid-fire SoC SET requests (HA slider drag) into one frame.

        HA's Number-Entity emits one async_set_native_value call per 5%-step
        when the user drags the slider, producing 5-10 SETs in <1 s. The
        device cannot keep wire field 3 (sys_bat_backup_ratio, EMS) and
        field 4 (dev_soc, App-Layer) in sync at that cadence, so the two
        fields drift apart and the user sees stale values in HA or the
        EcoFlow app. This method stores the latest (backup, solar) and
        defers the actual MQTT SET by `POWEROCEAN_SOC_DEBOUNCE_S`. Each
        new call within the window resets the timer, so only the final
        value reaches the device.

        Returns True synchronously - the caller should treat this as an
        accepted user request and apply the optimistic UI value. The
        actual SET runs asynchronously and may still fail; failures are
        logged via the underlying async_set_powerocean_soc.
        """
        if not self._enhanced_mode:
            _LOGGER.warning(
                "PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_sn,
            )
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct, solar_surplus_pct,
            )
            return False

        self._powerocean_soc_pending = (backup_reserve_pct, solar_surplus_pct)
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
        self._powerocean_soc_debounce_unsub = self.hass.loop.call_later(
            POWEROCEAN_SOC_DEBOUNCE_S,
            lambda: self.hass.async_create_task(self._flush_powerocean_soc()),
        )
        return True

    async def _flush_powerocean_soc(self) -> None:
        """Send the most recent debounced SoC SET to the device."""
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
            self._powerocean_soc_debounce_unsub = None
        pending = self._powerocean_soc_pending
        if pending is None:
            return
        self._powerocean_soc_pending = None
        backup, solar = pending
        await self.async_set_powerocean_soc(backup, solar)

    async def async_set_powerocean_soc(
        self, backup_reserve_pct: int, solar_surplus_pct: int,
    ) -> bool:
        """Send a 3-field SoC SET to PowerOcean (app-replay format).

        Wire: cmd_id=112 SysBatChgDsgSet with field 1=100 (sys_bat_chg_up_limit),
        field 2=backup (sys_bat_dsg_down_limit), field 3=solar_surplus
        (sys_bat_backup_ratio), plus extended envelope (check_type, from=ios,
        device_sn). The legacy `async_set_soc_limits` only sends fields 1+2
        and is silently ignored by the device for backup-reserve changes.
        """
        if not self._enhanced_mode:
            _LOGGER.warning("PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_sn)
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send PowerOcean SoC - MQTT not connected (%s)", self.device_sn)
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct, solar_surplus_pct,
            )
            return False

        from ..ecoflow.energy_stream import build_powerocean_soc_set_payload

        payload = build_powerocean_soc_set_payload(
            backup_reserve_pct,
            solar_surplus_pct,
            device_sn=self.device_sn,
        )
        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        label = f"backup={backup_reserve_pct} solar={solar_surplus_pct}"
        if ok:
            _LOGGER.debug("PowerOcean SoC sent: %s (%s)", label, self.device_sn)
            self._log_event("set_powerocean_soc", label)
        else:
            _LOGGER.warning("PowerOcean SoC SET failed: %s (%s)", label, self.device_sn)
            self._log_event("set_powerocean_soc_fail", label)
        return ok

    async def async_set_powerocean_work_mode(self, work_mode: int) -> bool:
        """Send SysWorkModeSet (cmd_id=98) for PowerOcean.

        Phase 1 supports only modes that work without sub-params:
        SELFUSE (0) and AI_SCHEDULE (12). TOU (1) and BACKUP (2) require
        TouParam/BackupParam and return result=1 if sent without them.
        """
        if not self._enhanced_mode:
            _LOGGER.warning(
                "Work-mode SET requires Enhanced Mode (%s)", self.device_sn,
            )
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot send work-mode - MQTT not connected (%s)", self.device_sn,
            )
            return False

        from ..ecoflow.energy_stream import build_work_mode_set_payload

        payload = build_work_mode_set_payload(work_mode)
        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        if ok:
            _LOGGER.debug("Work-mode sent: %d (%s)", work_mode, self.device_sn)
            self._log_event("set_work_mode", str(work_mode))
        else:
            _LOGGER.warning("Work-mode SET failed: %d (%s)", work_mode, self.device_sn)
            self._log_event("set_work_mode_fail", str(work_mode))
        return ok

    async def async_send_proto_set_command(
        self, payload: bytes, label: str,
    ) -> bool:
        """Send a protobuf SET command via WSS MQTT."""
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send proto SET (%s) - MQTT not connected (%s)", label, self.device_sn)
            return False

        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload,
        )
        if ok:
            _LOGGER.debug("Proto SET sent: %s (%s)", label, self.device_sn)
            self._log_event(f"proto_set_{label}", "ok")
        else:
            _LOGGER.warning("Proto SET failed: %s (%s)", label, self.device_sn)
            self._log_event(f"proto_set_{label}_fail", "")
        return ok

    async def async_send_delta3_set(self, command: dict[str, Any]) -> bool:
        """Apply a Delta 3 setting on whichever channel this entry is using.

        Developer keys write over the official HTTP endpoint
        `PUT /iot-open/sign/device/quota`. App logins have no HTTP endpoint and
        write the same setting as a binary ConfigWrite frame on the app channel
        instead - verified against hardware (ack plus readback).
        """
        if self._auth_method == AUTH_METHOD_APP:
            return await self._async_send_delta3_set_proto(command)

        if self._http_client is None:
            _LOGGER.warning(
                "Cannot apply setting for %s - no write channel available "
                "(no HTTP client for this entry)",
                self.device_sn,
            )
            return False

        result = await self._http_client.set_quota(command)
        params = command.get("params", {})
        if result is None:
            _LOGGER.warning("SET failed for %s: no response", self.device_sn)
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        _LOGGER.debug("SET applied for %s: %s", self.device_sn, params)
        self._log_event("set_cmd", f"params={list(params)[:3]}")
        # The device needs a moment before the change shows up in the quota.
        # The entity holds an optimistic value until then, so a plain refresh
        # on the next scheduled poll is enough.
        await self.async_request_refresh()
        return True

    async def _async_send_delta3_set_proto(self, command: dict[str, Any]) -> bool:
        """Write a Delta 3 setting as a ConfigWrite frame on the app channel."""
        from ..ecoflow.delta3_commands import build_proto_command

        params = command.get("params", {})
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot apply setting for %s - device connection is down",
                self.device_sn,
            )
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        payload = build_proto_command(command, self.device_sn)
        if payload is None:
            _LOGGER.warning(
                "Cannot apply setting for %s - unsupported control %s",
                self.device_sn,
                list(params)[:3],
            )
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.send_proto_set, payload
        )
        if not ok:
            _LOGGER.warning("SET failed for %s: not sent", self.device_sn)
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        _LOGGER.debug("SET sent for %s: %s", self.device_sn, params)
        self._log_event("set_cmd", f"params={list(params)[:3]}")
        # The device echoes the new value on its own report stream, so the
        # entity only has to hold its optimistic value until then.
        return True

    async def async_send_set_command(self, command: dict[str, Any]) -> bool:
        """Send a SET command to the device via MQTT.

        The IoT API SET format:
        Topic: /open/{certAccount}/{SN}/set
        Payload: {"id": <ts>, "version": "1.0", ...command}
        """
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send SET command - MQTT not connected (%s)", self.device_sn)
            return False

        msg_id = int(time.time() * 1000) % 1_000_000
        payload = json.dumps(
            {
                "from": "Android",
                "id": str(msg_id),
                "version": "1.0",
                **command,
            }
        )
        if self._mqtt_client.wss_mode:
            topic = f"/app/{self._mqtt_client.user_id}/{self.device_sn}/thing/property/set"
        else:
            topic = f"/open/{self._mqtt_client.cert_account}/{self.device_sn}/set"

        ok = await self.hass.async_add_executor_job(
            self._mqtt_client.publish, topic, payload, 1,
        )
        if ok:
            _LOGGER.debug("SET command sent: %s -> %s", topic, payload[:120])
            self._log_event("set_cmd", f"keys={list(command.keys())[:3]}")
        else:
            _LOGGER.warning("SET command failed: %s", topic)
            self._log_event("set_cmd_fail", f"keys={list(command.keys())[:3]}")
        return ok

