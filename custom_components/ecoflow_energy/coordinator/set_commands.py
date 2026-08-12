"""SET command dispatch for the EcoFlow device coordinator."""

from __future__ import annotations

import json
import logging
import time
from functools import partial
from typing import Any

from ..const import (
    AUTH_METHOD_APP,
    POWEROCEAN_SOC_DEBOUNCE_S,
    POWEROCEAN_SOC_STATE_KEYS,
)
from ..ecoflow.frame_capture import sanitize_frame
from ..entity import as_known_int

_LOGGER = logging.getLogger(__name__)


class DeviceValueNotReported(Exception):
    """A write needs a value the device has not reported yet.

    Raised where the value is read rather than where the user-facing error is
    built, because the read has to happen inside the write lock. Platforms
    translate it into `raise_set_not_ready`.
    """


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
            _LOGGER.warning("SoC limit SET requires Enhanced Mode (%s)", self.device_sn[:4])
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send SoC limits - MQTT not connected (%s)", self.device_sn[:4])
            return False

        from ..ecoflow.energy_stream import build_soc_limit_set_payload

        payload = build_soc_limit_set_payload(max_charge_soc, min_discharge_soc)
        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if ok:
            _LOGGER.debug(
                "SoC limits sent: max=%d, min=%d (%s)",
                max_charge_soc, min_discharge_soc, self.device_sn[:4],
            )
            self._log_event("set_soc_limits", f"max={max_charge_soc}, min={min_discharge_soc}")
        else:
            _LOGGER.warning("SoC limits SET failed (%s)", self.device_sn[:4])
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
                "PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_sn[:4],
            )
            return False
        if backup_reserve_pct > solar_surplus_pct:
            _LOGGER.warning(
                "PowerOcean SoC SET rejected locally: backup_reserve (%d) > "
                "solar_surplus (%d). Device requires backup <= solar.",
                backup_reserve_pct, solar_surplus_pct,
            )
            return False

        if not self._powerocean_soc_cycle_open:
            # A cycle is opening. This is the last moment the device values
            # are still in _device_data: the caller applies its optimistic
            # value the instant this returns True, so a snapshot taken later
            # - in the flush, as the first version of this did - captures the
            # requested value and "rolling back" to it changes nothing.
            #
            # The gate is the open cycle, not the pending value. The flush
            # clears the pending value when it starts, so a drag arriving
            # while its write is still in flight would look like a fresh
            # window and snapshot the failing write's optimistic values.
            self._powerocean_soc_generation += 1
            self._powerocean_soc_cycle_open = True
            self._powerocean_soc_before = {
                key: self._device_data.get(key)
                for key in POWEROCEAN_SOC_STATE_KEYS
                if key in self._device_data
            }

        self._powerocean_soc_pending = (backup_reserve_pct, solar_surplus_pct)
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
        self._powerocean_soc_debounce_unsub = self.hass.loop.call_later(
            POWEROCEAN_SOC_DEBOUNCE_S,
            lambda: self.hass.async_create_task(self._flush_powerocean_soc()),
        )
        return True

    async def _flush_powerocean_soc(self) -> None:
        """Send the most recent debounced SoC SET to the device.

        The service call returned before this runs, so a failure here can
        no longer be raised at the caller. What it must not do is leave the
        sliders showing a value the device never took: both entities applied
        an optimistic value on the way in, and on failure that value is the
        only thing left saying the write worked (issue #185).
        """
        if self._powerocean_soc_debounce_unsub is not None:
            self._powerocean_soc_debounce_unsub.cancel()
            self._powerocean_soc_debounce_unsub = None
        pending = self._powerocean_soc_pending
        if pending is None:
            return
        self._powerocean_soc_pending = None
        backup, solar = pending
        generation = self._powerocean_soc_generation
        before = dict(self._powerocean_soc_before)

        try:
            delivered = await self.async_set_powerocean_soc(backup, solar)
        except Exception:  # noqa: BLE001
            # Anything unexpected here would otherwise skip the rollback and
            # leave the optimistic value standing, which is the failure this
            # whole path exists to prevent.
            _LOGGER.debug("PowerOcean SoC flush raised", exc_info=True)
            delivered = False

        if generation != self._powerocean_soc_generation:
            # A newer cycle opened while this write was in flight. It owns the
            # outcome now: undoing on top of it would replace a current value
            # with a stale one, and closing its cycle would let the next drag
            # snapshot this write's optimistic values.
            return

        if delivered:
            # The device took these, so they are what a later failure has to
            # fall back to. Reading _device_data again would be wrong when a
            # newer drag has already written its own optimistic value there.
            self._powerocean_soc_before = {
                POWEROCEAN_SOC_STATE_KEYS[0]: backup,
                POWEROCEAN_SOC_STATE_KEYS[1]: solar,
            }
            self._powerocean_soc_cycle_open = False
            return

        # Undo the optimistic values so the sliders fall back to whatever
        # the device last reported rather than to the request that failed.
        for key in POWEROCEAN_SOC_STATE_KEYS:
            if key in before:
                self.set_device_value(key, before[key])
                if self.data is not None:
                    self.data[key] = before[key]
            else:
                # The device had never reported this key, so there is nothing
                # to fall back to. Dropping it shows unknown, which is true;
                # leaving it would publish the value the device refused.
                self._device_data.pop(key, None)
                if self.data is not None:
                    self.data.pop(key, None)
        self._powerocean_soc_cycle_open = False
        # The entities hold a 5 s optimistic lock and would ignore the update
        # below; on a dead connection no further update follows to correct
        # them, so the failed value would simply stay on screen.
        self._powerocean_soc_rollback_generation += 1
        self.async_update_listeners()

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
            _LOGGER.warning("PowerOcean SoC SET requires Enhanced Mode (%s)", self.device_sn[:4])
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning("Cannot send PowerOcean SoC - MQTT not connected (%s)", self.device_sn[:4])
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
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        label = f"backup={backup_reserve_pct} solar={solar_surplus_pct}"
        if ok:
            _LOGGER.debug("PowerOcean SoC sent: %s (%s)", label, self.device_sn[:4])
            self._log_event("set_powerocean_soc", label)
        else:
            _LOGGER.warning("PowerOcean SoC SET failed: %s (%s)", label, self.device_sn[:4])
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
                "Work-mode SET requires Enhanced Mode (%s)", self.device_sn[:4],
            )
            return False
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.warning(
                "Cannot send work-mode - MQTT not connected (%s)", self.device_sn[:4],
            )
            return False

        from ..ecoflow.energy_stream import build_work_mode_set_payload

        payload = build_work_mode_set_payload(work_mode)
        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if ok:
            _LOGGER.debug("Work-mode sent: %d (%s)", work_mode, self.device_sn[:4])
            self._log_event("set_work_mode", str(work_mode))
        else:
            _LOGGER.warning("Work-mode SET failed: %d (%s)", work_mode, self.device_sn[:4])
            self._log_event("set_work_mode_fail", str(work_mode))
        return ok

    # ------------------------------------------------------------------
    # Stream BK-series SoC config writes (254/17)
    # ------------------------------------------------------------------

    async def async_set_stream_soc_limits(
        self, *, charge: int | None = None, discharge: int | None = None,
    ) -> bool:
        """Write the grouped Stream charge/discharge limit configuration.

        The app sends charge limit, discharge limit and backup reserve in one
        frame, so all three travel on every write. The two the caller did not
        touch are read from live telemetry and sent back unchanged; a default
        substituted for a missing one would rewrite a setting nobody asked to
        change, which is why an unreported value refuses the write instead.
        """
        from ..ecoflow.energy_stream import build_stream_soc_limits_payload

        async with self._device_config_lock:
            data = self.data or {}
            current_charge = as_known_int(data.get("max_charge_soc_pct"))
            current_discharge = as_known_int(data.get("min_discharge_soc_pct"))
            backup = as_known_int(data.get("backup_reserve_pct"))
            if (
                current_charge is None
                or current_discharge is None
                or backup is None
            ):
                raise DeviceValueNotReported("Stream SoC limits")

            requested_charge = current_charge if charge is None else charge
            requested_discharge = (
                current_discharge if discharge is None else discharge
            )

            payload = build_stream_soc_limits_payload(
                requested_charge,
                requested_discharge,
                backup,
                self.device_sn,
            )
            if not await self.async_send_proto_set_command(
                payload, label="stream_soc_limits"
            ):
                return False
            self._seed_device_values(
                max_charge_soc_pct=requested_charge,
                min_discharge_soc_pct=requested_discharge,
                backup_reserve_pct=backup,
            )
            return True

    async def async_set_stream_backup_reserve(self, reserve_pct: int) -> bool:
        """Write Stream backup reserve without racing a grouped limit write."""
        from ..ecoflow.energy_stream import build_stream_backup_reserve_payload

        async with self._device_config_lock:
            payload = build_stream_backup_reserve_payload(
                reserve_pct, self.device_sn
            )
            if not await self.async_send_proto_set_command(
                payload, label="stream_backup_reserve"
            ):
                return False
            self._seed_device_values(backup_reserve_pct=reserve_pct)
            return True

    # ------------------------------------------------------------------
    # STREAM AC 5000 config writes (254/38)
    #
    # Each one reads what the device currently reports before it can send,
    # so the read and the send are one operation under
    # `_device_config_lock`. A caller that read first and sent after
    # would interleave with the other entity writing the same config field.
    # The values sent are seeded into the store before the lock is released,
    # for the same reason: a waiter deciding what to send must not see the
    # state from before the write it is queued behind. The Stream writes
    # above share both the lock and `_seed_device_values` for exactly these
    # two reasons.
    # ------------------------------------------------------------------

    async def async_set_stream_ac5000_soc_limits(
        self, *, charge: int | None = None, discharge: int | None = None,
    ) -> bool:
        """Write config field 29, which holds both SoC limits.

        The limit not being changed travels at its current value. Pass the one
        being set; the other is read here.
        """
        from ..ecoflow.stream_ac5000_commands import build_soc_limits_payload

        async with self._device_config_lock:
            data = self.data or {}
            if charge is None:
                charge = as_known_int(data.get("max_charge_soc_pct"))
            if discharge is None:
                discharge = as_known_int(data.get("min_discharge_soc_pct"))
            if charge is None or discharge is None:
                raise DeviceValueNotReported("SoC limits")
            # Raises ValueError on a pair the device would refuse.
            payload = build_soc_limits_payload(charge, discharge, self.device_sn)
            if not await self.async_send_proto_set_command(
                payload, label="stream_ac5000_soc_limits"
            ):
                return False
            self._seed_device_values(
                max_charge_soc_pct=charge, min_discharge_soc_pct=discharge
            )
            return True

    async def async_set_stream_ac5000_backup_reserve(
        self, *, enabled: bool | None = None, reserve_pct: int | None = None,
    ) -> bool:
        """Write config field 30, which holds the on/off flag and the level.

        The switch owns the flag and a number owns the level, so this is the
        one config write whose two halves live on different platforms.
        """
        from ..ecoflow.stream_ac5000_commands import build_backup_reserve_payload

        async with self._device_config_lock:
            data = self.data or {}
            if enabled is None:
                enabled = data.get("backup_reserve_enabled")
            if reserve_pct is None:
                reserve_pct = as_known_int(data.get("backup_reserve_pct"))
            if not isinstance(enabled, bool) or reserve_pct is None:
                raise DeviceValueNotReported("backup reserve")
            payload = build_backup_reserve_payload(
                enabled, reserve_pct, self.device_sn
            )
            if not await self.async_send_proto_set_command(
                payload, label="stream_ac5000_backup_reserve"
            ):
                return False
            self._seed_device_values(
                backup_reserve_enabled=enabled, backup_reserve_pct=reserve_pct
            )
            return True

    async def async_set_stream_ac5000_task_power(
        self, kind: str, power_w: int,
    ) -> bool:
        """Replace the scheduled task with one of `kind` at `power_w`.

        This device has no direct power setpoint: a scheduled task is the
        setpoint. Charge and discharge are separate whole-day tasks, so the
        other kind is removed before the new one is written, or the device
        sees overlapping time periods and acts on neither.
        """
        from ..ecoflow.stream_ac5000_commands import (
            MINUTES_PER_DAY,
            TASK_REMOVE,
            build_task_payload,
        )

        other = "discharge" if kind == "charge" else "charge"
        async with self._device_config_lock:
            data = self.data or {}
            if as_known_int(data.get(f"scheduled_{other}_power_w")) is not None:
                if not await self.async_send_proto_set_command(
                    build_task_payload(
                        other, 0, MINUTES_PER_DAY - 1, 0, self.device_sn,
                        operation=TASK_REMOVE,
                    ),
                    label=f"stream_ac5000_{other}_task_remove",
                ):
                    # The task may well still be there, so writing the new one
                    # would leave exactly the overlapping pair.
                    return False
                self._clear_stream_ac5000_task(other)
            payload = self._build_stream_ac5000_task(kind, power_w, data)
            if not await self.async_send_proto_set_command(
                payload, label=f"stream_ac5000_{kind}_power"
            ):
                return False
            self._seed_device_values(**{f"scheduled_{kind}_power_w": power_w})
            return True

    def _seed_device_values(self, **values: Any) -> None:
        """Record values just sent, in both the store and the snapshot."""
        for key, value in values.items():
            self.set_device_value(key, value)
            if self.data is not None:
                self.data[key] = value

    def _clear_stream_ac5000_task(self, kind: str) -> None:
        """Forget a task this integration has just removed.

        The device stops mentioning a deleted task rather than reporting it
        empty, and the parser's clear-everything branch only fires on an empty
        task list, which the replacement task keeps it from being. So nothing
        would ever retract these values and the setpoint entity would go on
        showing a task that no longer exists.

        `scheduled_charge_soc_target` deliberately survives: it is the app's
        charge limit, the next charge write reads it back, and clearing it
        would reset a task set to stop at 80% into charging to 100%.
        """
        for suffix in ("power_w", "enabled", "start_min", "end_min"):
            state_key = f"scheduled_{kind}_{suffix}"
            self.set_device_value(state_key, None)
            if self.data is not None:
                self.data[state_key] = None

    def _build_stream_ac5000_task(
        self, kind: str, power_w: int, data: dict[str, Any]
    ) -> bytes:
        """Build the task frame that carries a power setpoint.

        It is always a whole-day, enabled task, so the value asked for is the
        value that acts. The only thing carried over from a reported task is
        the charge target SoC, which decides what charging does rather than
        whether it happens.

        Zero is a real setpoint, not an absence: a 0 W discharge task parks the
        battery, while removing every task leaves it on its own 200 W base
        output.
        """
        from ..ecoflow.stream_ac5000_commands import (
            MINUTES_PER_DAY,
            TASK_ADD,
            TASK_UPDATE,
            build_task_payload,
        )

        mode = data.get("work_mode")
        if mode is not None and mode != "custom":
            _LOGGER.warning(
                "Setting the %s power on %s while it is in %s mode: a "
                "scheduled task is only acted on in custom mode, so the "
                "device will accept this and may do nothing with it",
                kind, self.device_sn[:4], mode,
            )

        reported = as_known_int(data.get(f"scheduled_{kind}_power_w")) is not None
        soc_target = data.get("scheduled_charge_soc_target")
        return build_task_payload(
            kind,
            0,
            MINUTES_PER_DAY - 1,
            power_w,
            self.device_sn,
            enabled=True,
            operation=TASK_UPDATE if reported else TASK_ADD,
            charge_soc_target=soc_target if isinstance(soc_target, int) else 100,
        )

    async def async_send_proto_set_command(
        self, payload: bytes, label: str,
    ) -> bool:
        """Send a protobuf SET command via WSS MQTT."""
        if self._mqtt_client is None or not self._mqtt_client.is_connected():
            _LOGGER.debug("Cannot send proto SET (%s) - MQTT not connected (%s)", label, self.device_sn[:4])
            return False

        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if ok:
            _LOGGER.debug("Proto SET sent: %s (%s)", label, self.device_sn[:4])
            self._log_event(f"proto_set_{label}", "ok")
        else:
            _LOGGER.debug("Proto SET not delivered: %s (%s)", label, self.device_sn[:4])
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
                self.device_sn[:4],
            )
            return False

        result = await self._http_client.set_quota(command)
        params = command.get("params", {})
        if result is None:
            _LOGGER.warning("SET failed for %s: no response", self.device_sn[:4])
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        _LOGGER.debug("SET applied for %s: %s", self.device_sn[:4], params)
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
                self.device_sn[:4],
            )
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        payload = build_proto_command(command, self.device_sn)
        if payload is None:
            _LOGGER.warning(
                "Cannot apply setting for %s - unsupported control %s",
                self.device_sn[:4],
                list(params)[:3],
            )
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.send_proto_set, payload, wait=True),
        )
        if not ok:
            _LOGGER.warning("SET failed for %s: not sent", self.device_sn[:4])
            self._log_event("set_cmd_fail", f"params={list(params)[:3]}")
            return False

        _LOGGER.debug("SET sent for %s: %s", self.device_sn[:4], params)
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
            _LOGGER.debug("Cannot send SET command - MQTT not connected (%s)", self.device_sn[:4])
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

        # wait=True: the caller reports the outcome to the user, so a
        # locally queued message is not good enough (issue #185).
        ok = await self.hass.async_add_executor_job(
            partial(self._mqtt_client.publish, topic, payload, 1, wait=True),
        )
        if ok:
            # The command body is what makes a failed write debuggable, but it
            # carries the full serial (the Smart Plug JSON has an "sn" field),
            # and reporters attach debug logs to public issues. Masked first
            # and truncated second, so the cut can never leave a serial
            # fragment too short for the mask to recognize.
            _LOGGER.debug(
                "SET command sent: %s -> %s",
                self._mqtt_client.mask_topic(topic),
                sanitize_frame(payload.encode(), [self.device_sn]).decode()[:120],
            )
            self._log_event("set_cmd", f"keys={list(command.keys())[:3]}")
        else:
            # The entity raises for the user; a warning here would only
            # duplicate the error Home Assistant already logs.
            _LOGGER.debug(
                "SET command not delivered: %s",
                self._mqtt_client.mask_topic(topic),
            )
            self._log_event("set_cmd_fail", f"keys={list(command.keys())[:3]}")
        return ok
