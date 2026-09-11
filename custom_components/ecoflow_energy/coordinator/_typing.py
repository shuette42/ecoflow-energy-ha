"""Type-check-only declarations of the state the coordinator mixins borrow.

Never imported at runtime: every mixin imports `CoordinatorState` inside an
`if TYPE_CHECKING:` block and binds `object` in its place. Nothing here has a
body and nothing here declares `__init__`, so the runtime MRO of
`EcoFlowDeviceCoordinator` is exactly what it was before this file existed.

A name belongs here when a mixin reads it and does not assign it. State a
mixin owns is annotated in that mixin's own class body.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

if TYPE_CHECKING:
    from ..ecoflow.cloud_http import EcoFlowHTTPQuota
    from ..ecoflow.cloud_mqtt import EcoFlowMQTTClient
    from ..ecoflow.energy_integrator import EnergyIntegrator
    from ..ecoflow.frame_capture import TypedFrameBuffer
    from ..ecoflow.iot_api import IoTApiClient
    from .core import DeviceSnapshot, EcoFlowDeviceCoordinator, WallboxActionPending


class CoordinatorState(DataUpdateCoordinator[dict[str, Any]]):
    """Declaration-only view of `EcoFlowDeviceCoordinator`.

    Group (A) - `hass`, `data`, `config_entry`, `update_interval`,
    `async_request_refresh`, `async_set_updated_data`,
    `async_update_listeners` - is inherited from the base above and is
    deliberately absent below.
    """

    # -- group (B): assigned in EcoFlowDeviceCoordinator.__init__ --
    device_sn: str
    device_type: str
    device_name: str
    _entry: ConfigEntry
    _auth_method: str
    _enhanced_mode: bool
    _shutdown: bool
    _device_data: dict[str, Any]
    _mqtt_client: EcoFlowMQTTClient | None
    _http_client: EcoFlowHTTPQuota | None
    _iot_api: IoTApiClient | None
    _config_writes_sent: dict[int, float]
    _device_config_lock: asyncio.Lock
    _wallbox_action_lock: asyncio.Lock
    _wallbox_action_pending: WallboxActionPending | None
    _powerocean_soc_pending: tuple[int, int] | None
    _powerocean_soc_pending_revision: int
    _powerocean_soc_debounce_unsub: asyncio.TimerHandle | None
    _powerocean_soc_flush_tasks: set[asyncio.Task[None]]
    _powerocean_soc_write_tasks: set[asyncio.Future[Any]]
    _powerocean_soc_before: dict[str, Any]
    _powerocean_soc_generation: int
    _powerocean_soc_request_revision: int
    _powerocean_soc_confirmed_revision: int
    _powerocean_soc_active_revisions: set[int]
    _powerocean_soc_latest_outcome: bool | None
    _powerocean_soc_cycle_open: bool
    _powerocean_soc_rollback_generation: int
    _last_mqtt_ts: float
    _energy_integrator: EnergyIntegrator
    _bp_sn_to_index: dict[str, int]
    _raw_frames: TypedFrameBuffer
    _raw_frames_lock: threading.Lock
    _schedule_indices: set[int]
    _feed_schedule_indices: set[int]
    _schedule_divergent_bundles: int
    _energy_from_api: list[tuple[str, str]]
    _power_to_energy: dict[str, str]
    _firmware: dict[str, dict[str, Any]]
    _schedule_armed_latch: dict[str, tuple[bool, float]]
    _shutdown_complete: asyncio.Event
    _shutdown_task: asyncio.Task[None] | None
    _keepalive_unsub: asyncio.TimerHandle | None
    _stale_check_unsub: asyncio.TimerHandle | None
    _quotas_unsub: asyncio.TimerHandle | None
    _ping_unsub: asyncio.TimerHandle | None
    _credential_refresh_unsub: asyncio.TimerHandle | None
    _last_smartplug_get_all_ts: float
    _last_mqtt_event_ts: float
    _last_flush_ts: float
    _surplus_sync_record: dict[str, int | bool] | None
    _last_app_surplus_sync_ts: float
    _batt_w_samples: list[tuple[float, float]]
    _batt_state_changed_at: float
    _consecutive_http_failures: int
    _snapshot: DeviceSnapshot
    _device_available: bool
    _last_stale_reconnect_ts: float
    _stale_reactivate_tried: bool
    _unit_power_stats: dict[str, Any] | None

    # -- group (C): implemented by core.py or by a sibling mixin --
    @property
    def device_tag(self) -> str: ...

    @property
    def device_data(self) -> dict[str, Any]: ...

    @property
    def _energy_integrator_keys(self) -> set[str]: ...

    def set_device_value(self, key: str, value: Any) -> None: ...

    def powerocean_sibling(self) -> EcoFlowDeviceCoordinator | None: ...

    def _log_event(self, event_type: str, detail: str) -> None: ...

    def latch_schedule_armed(self, state_key: str, armed: bool) -> None: ...

    def clear_schedule_armed_latch(self, state_key: str) -> None: ...

    def record_unknown_proto_fields(
        self, cmd_key: str, fields: dict[int, Any]
    ) -> None: ...

    def _note_value_change(self, parsed: dict[str, Any]) -> None: ...

    def _enforce_monotonic(self, parsed: dict[str, Any]) -> dict[str, Any]: ...

    def _apply_data(self, parsed: dict[str, Any]) -> None: ...

    def _resolve_soc(self, parsed: dict[str, Any]) -> None: ...

    def _resolve_wallbox_action(self, parsed: dict[str, Any]) -> None: ...

    def _integrate_energy(self, parsed: dict[str, Any]) -> None: ...

    def _derive_battery_state(self) -> None: ...

    def _on_mqtt_message(self, topic: str, payload: bytes) -> None: ...

    def _on_mqtt_auth_error(self) -> None: ...

    def _schedule_keepalive(self) -> None: ...

    def _schedule_ping(self) -> None: ...

    def _schedule_quotas_poll(self) -> None: ...

    def _schedule_stale_check(self) -> None: ...

    def _schedule_credential_refresh(self) -> None: ...

    def _schedule_powerocean_soc_write(
        self,
        backup_reserve_pct: int,
        solar_surplus_pct: int,
        *,
        name: str,
    ) -> asyncio.Task[object] | None: ...
