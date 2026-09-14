"""DataUpdateCoordinator for EcoFlow devices.

Standard Mode: HTTP polling via IoT Developer API (POST /iot-open/sign/device/quota).
  - Primary data source is HTTP polling (update_interval=30s).
  - MQTT is used for SET commands (switches, numbers) only.
  - Exception: Delta devices additionally subscribe to MQTT push for real-time data.

Enhanced Mode: MQTT push via WSS (port 8084).
  - Primary data source is MQTT push (update_interval=None).
  - EnergyStreamSwitch keep-alive every 20s.
  - Falls back to HTTP polling when the MQTT stream is stale.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from ..const import (
    AUTH_METHOD_APP,
    AUTH_METHOD_DEVELOPER,
    CONF_AUTH_METHOD,
    DELTA3_ENERGY_FROM_API,
    DELTA3_POWER_TO_ENERGY,
    DELTA_ENERGY_FROM_API,
    DELTA_POWER_TO_ENERGY,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_DISPLAY_NAMES,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERSTREAM,
    DEVICE_TYPE_SMARTPLUG,
    DEVICE_TYPE_STREAM,
    DEVICE_TYPE_STREAM_AC5000,
    DEVICE_TYPE_UNKNOWN,
    DEVICE_TYPE_WAVE3,
    DOMAIN,
    HTTP_FALLBACK_INTERVAL_S,
    POWEROCEAN_ENERGY_FROM_API,
    POWEROCEAN_POWER_TO_ENERGY,
    POWERSTREAM_ENERGY_FROM_API,
    POWERSTREAM_POWER_TO_ENERGY,
    RAW_FRAME_LOG_KEYS_MAX,
    RAW_FRAME_LOG_PER_KEY_MAX,
    RAW_FRAME_PER_KEY_MAX,
    SMARTPLUG_ENERGY_FROM_API,
    SMARTPLUG_POWER_TO_ENERGY,
    STREAM_ENERGY_FROM_API,
    STREAM_POWER_TO_ENERGY,
    STREAMAC5000_ENERGY_FROM_API,
    STREAMAC5000_POWER_TO_ENERGY,
    UNKNOWN_FIELD_CMDS_MAX,
    UNKNOWN_FIELD_NUMBERS_MAX,
    WAVE3_ENERGY_FROM_API,
    WAVE3_POWER_TO_ENERGY,
    device_log_tag,
    get_delta_profile,
    get_device_name,
    raw_capture_window_open,
)
from ..ecoflow.energy_integrator import EnergyIntegrator
from ..ecoflow.frame_capture import TypedFrameBuffer
from .availability import AvailabilityMixin
from .credentials import CredentialsMixin
from .http_poll import HttpPollMixin
from .keepalive import KeepaliveMixin
from .mqtt_ingest import MqttIngestMixin
from .set_commands import SetCommandsMixin
from .setup import SetupMixin
from .state_apply import StateApplyMixin

if TYPE_CHECKING:
    from ..ecoflow.cloud_http import EcoFlowHTTPQuota
    from ..ecoflow.cloud_mqtt import EcoFlowMQTTClient
    from ..ecoflow.iot_api import IoTApiClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeviceSnapshot:
    """Immutable snapshot of device state at a point in time."""

    data: dict[str, Any] = field(default_factory=dict)
    captured_at: float = 0.0
    source: str = ""
    key_count: int = 0


@dataclass(frozen=True)
class WallboxActionPending:
    """One PowerPulse 2 write awaiting device confirmation (ADR-009, PLAN-146).

    `future` resolves with the confirming value when the wallbox reports it,
    or is cancelled on coordinator shutdown. `issued_at` is monotonic and is
    not currently compared against an ingest receive timestamp - the
    mqtt_ingest path does not stamp one (checked 2026-09-11), so the field is
    carried for a future comparison rather than used by one now.

    A start/stop (`expected_value is None`) is confirmed by a status in
    `POWERPULSE2_CHARGE_ACTION_CONFIRMED[action]` arriving on `state_key`
    (`ev_charge_status`, the default). A setting write instead names its own
    `state_key` and is confirmed the moment the arriving frame's value at
    that key equals `expected_value` - a status-set membership check would
    not apply to a numeric setting. `expected_value` is `float | str | None`
    since PLAN-147: a numeric setting (max current) compares by tolerance,
    a string setting (charging mode) compares by equality - see
    `_resolve_wallbox_action` in state_apply.py.
    """

    action: str
    issued_at: float
    future: asyncio.Future[str | float]
    state_key: str = "ev_charge_status"
    expected_value: float | str | None = None


class EcoFlowDeviceCoordinator(
    SetupMixin,
    CredentialsMixin,
    KeepaliveMixin,
    MqttIngestMixin,
    StateApplyMixin,
    SetCommandsMixin,
    HttpPollMixin,
    AvailabilityMixin,
    DataUpdateCoordinator[dict[str, Any]],
):
    """Coordinator for a single EcoFlow device."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device_info: dict[str, Any],
    ) -> None:
        """Initialize the coordinator."""
        self.device_sn: str = device_info["sn"]
        # Re-classify device type from product_name if stored as "unknown"
        stored_type = device_info.get("device_type", "")
        if stored_type == DEVICE_TYPE_UNKNOWN:
            from ..const import get_device_type

            stored_type = get_device_type(
                device_info.get("product_name", ""), self.device_sn
            )
        self.device_type: str = stored_type
        product_name = device_info.get("product_name", "")
        display_name = DEVICE_TYPE_DISPLAY_NAMES.get(self.device_type, "")
        self.device_name: str = (
            device_info.get("name")
            or get_device_name(product_name, self.device_sn)
            or display_name
            or "EcoFlow Device"
        )
        self.product_name: str = (
            product_name
            or get_device_name(product_name, self.device_sn)
            or display_name
            or "Unknown"
        )
        self._sw_version: str = device_info.get("sw_version", "")
        self.delta_profile: str = (
            get_delta_profile(self.product_name, self.device_sn)
            if self.device_type == DEVICE_TYPE_DELTA
            else ""
        )

        # App-auth (Enhanced Mode): WSS MQTT push, no HTTP polling.
        # Developer-auth (Standard Mode): HTTP polling + TCP MQTT.
        enhanced_mode = entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_APP

        # Standard Mode: HTTP polling every 30s (primary data source)
        # Enhanced Mode: MQTT push only - protobuf carries all sensor data
        #   (power, battery, MPPT, grid phases, EMS state).
        #   HTTP fallback activates only when MQTT is stale (>35s).
        poll_interval = (
            None if enhanced_mode else timedelta(seconds=HTTP_FALLBACK_INTERVAL_S)
        )

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"EcoFlow {self.device_name} ({self.device_tag})",
            update_interval=poll_interval,
        )

        self._entry = entry
        self._mqtt_client: EcoFlowMQTTClient | None = None
        self._http_client: EcoFlowHTTPQuota | None = None
        self._iot_api: IoTApiClient | None = None

        self._last_mqtt_ts: float = 0.0
        self._device_data: dict[str, Any] = {}
        # ConfigWrite field -> monotonic time this integration last wrote it.
        # Written on the event loop (`async_send_wave3_set`), read on the Paho
        # thread (`_check_config_write_ack`) to tell a rejection of our own
        # write from one of the vendor app's on the shared set_reply topic.
        self._config_writes_sent: dict[int, float] = {}
        # When a value last actually moved, and how many updates in a row have
        # carried nothing new. `update_interval` alone says how often we ask,
        # which is what a diagnostics download reported until 2026-08-27 - so a
        # reporter whose readings moved twice a day could point at a 30 s poll
        # in the file and be entirely right about the number and no closer to
        # the answer (#267). These two say whether the asking achieved
        # anything.
        self._last_value_change_ts: float = 0.0
        self._unchanged_updates: int = 0
        self._snapshot = DeviceSnapshot()
        # Raw HTTP quota snapshot (Delta 3 only): the field map is
        # community-researched but not yet hardware-verified for every key,
        # so diagnostics expose the raw key/value pairs to let beta dumps
        # confirm existing mappings and surface keys still to be added.
        self._raw_quota: dict[str, Any] = {}
        self._raw_quota_captured_at: float = 0.0
        # Firmware revisions read from the quota, per subsystem. Neither device
        # list endpoint reports one, so this is the only source we have - and
        # only in Standard Mode, since Enhanced Mode never polls the quota.
        self._firmware: dict[str, dict[str, Any]] = {}
        self._keepalive_unsub: asyncio.TimerHandle | None = None
        self._stale_check_unsub: asyncio.TimerHandle | None = None
        self._quotas_unsub: asyncio.TimerHandle | None = None
        self._ping_unsub: asyncio.TimerHandle | None = None
        self._enhanced_mode: bool = enhanced_mode
        self._auth_method: str = AUTH_METHOD_DEVELOPER
        self._shutdown: bool = False
        # Every unload caller shields and awaits this one cleanup operation.
        # A failed cleanup stays failed so later callers observe the same
        # outcome instead of being told shutdown succeeded.
        self._shutdown_task: asyncio.Task[None] | None = None
        self._shutdown_complete = asyncio.Event()
        self._last_flush_ts: float = 0.0
        self._last_mqtt_event_ts: float = 0.0
        self._consecutive_http_failures: int = 0
        self._device_available: bool = True
        self._last_stale_reconnect_ts: float = 0.0
        # Stale escalation: the cheap remedy (re-send post-connect requests)
        # runs before the expensive one (force reconnect).
        self._stale_reactivate_tried: bool = False
        self._last_smartplug_get_all_ts: float = 0.0
        # Surplus auto-sync state (PowerOcean Enhanced Mode):
        # the EcoFlow app sets the slider via cmd_id=112 wire field 4 only,
        # which leaves the EMS-side `sys_bat_backup_ratio` unchanged. The
        # device mirrors the app's value back via cmd_id=13 EmsParamChangeReport
        # field 10 (`dev_soc`). When that diverges from the EMS value, the
        # coordinator schedules a corrective both-field SET. The throttle
        # avoids redundant SETs and the user-grace gives the device echo time.
        self._last_app_surplus_sync_ts: float = 0.0
        self._last_user_surplus_set_ts: float = 0.0
        # Timestamp of the most recent EmsParamChangeReport (cmd_id=13) that
        # carried a `dev_soc` field. The auto-sync only acts on frames newer
        # than the last user SET - stale ParamChange frames (e.g. an EMS
        # echo of a value the user has since superseded in HA) would
        # otherwise pull HA back to the obsolete app-side value.
        self._last_ems_param_change_ts: float = 0.0
        # The divergent (app, ems) pair the auto-sync is currently bounded
        # against (ADR-013): app value, ems value, writes issued for this
        # pair, and whether the bound has been reached and reported.
        # Cleared on a user SET, on a changed app value, or on a changed
        # ems value while the app value stays put - each is new intent or
        # new information, not a continuation of the suppressed pair.
        self._surplus_sync_record: dict[str, int | bool] | None = None
        # What the last STREAM per-unit block held: how many linked units it
        # listed, and whether one of them was this device. Diagnostics only.
        # A serial that never matches is the one failure this feature can have
        # that looks exactly like a device that does not send the block, and
        # nothing else in a diagnostics download tells the two apart.
        self._unit_power_stats: dict[str, Any] | None = None
        # Said once per coordinator: a PV block that lists units, none ours.
        self._unit_pv_unmatched_logged = False
        # Monotonic time this coordinator's own connection last delivered a
        # per-unit block, keyed by the block's parser key (PLAN-145, #401).
        # Written only from an own-connection frame; a sibling's hand-over
        # never touches it, which is what lets the hold in
        # `_resolve_unit_power` tell "our connection is fresh" from "our
        # connection is merely alive" - the two differed for seven hours in
        # the 13.09. capture.
        self._own_unit_entry_ts: dict[str, float] = {}
        # Whether a Stream ever reported the system state of charge; once it
        # has, a unit's own figure no longer stands in for it (#323).
        self._soc_from_system = False
        # Arming flags this integration has just written, each with the value
        # sent and the monotonic time the hold expires. A scheduled-task write
        # seeds the flag before it sends, and a device frame that was already
        # in flight would otherwise put the old flag back - a later power
        # write reads the flag out of the store and would then re-send the
        # reverted one, undoing what the owner just asked for.
        self._schedule_armed_latch: dict[str, tuple[bool, float]] = {}
        # Debounce state for the PowerOcean SoC SET. HA Number-Entity sliders
        # send one SET per 5%-step during a mouse drag, which arrives at the
        # device at ~100 ms cadence. The device cannot keep all SETs in sync
        # between Field 3 (EMS) and Field 4 (App-Layer), so the two fields
        # desync. The debouncer coalesces all SET requests inside
        # POWEROCEAN_SOC_DEBOUNCE_S to a single frame carrying the most
        # recent (backup, solar) pair.
        self._powerocean_soc_pending: tuple[int, int] | None = None
        self._powerocean_soc_pending_revision: int = 0
        self._powerocean_soc_debounce_unsub: asyncio.TimerHandle | None = None
        # More than one flush can be in flight when a second debounce window
        # expires while the previous MQTT publish is still waiting for its
        # acknowledgement. Shutdown owns and drains every one before MQTT is
        # disconnected.
        self._powerocean_soc_flush_tasks: set[asyncio.Task[None]] = set()
        # Every direct SoC publish registers its actual executor future. The
        # coordinator-created auto-sync task is tracked here as well so there
        # is no gap before it starts and registers the broker operation.
        self._powerocean_soc_write_tasks: set[asyncio.Future[Any]] = set()
        # Rollback baseline: captured from device state when a cycle opens,
        # then advanced only by successfully delivered requests in revision
        # order. It therefore never contains an unconfirmed optimistic value.
        self._powerocean_soc_before: dict[str, Any] = {}
        # Identifies resolved debounce cycles so completions from an older
        # cycle cannot mutate the current one. Request revisions below order
        # overlapping publishes within and across those cycles.
        self._powerocean_soc_generation: int = 0
        # Generation separates resolved drag cycles. Revisions order every
        # accepted request inside and across those cycles, because two MQTT
        # publishes can complete in the opposite order from their requests.
        self._powerocean_soc_request_revision: int = 0
        self._powerocean_soc_confirmed_revision: int = 0
        # Claimed revisions stay active until their publish result has been
        # reconciled. The latest result is applied only after the pending
        # timer and every claimed revision in the cycle have settled.
        self._powerocean_soc_active_revisions: set[int] = set()
        self._powerocean_soc_latest_outcome: bool | None = None
        # True from the moment a write cycle starts until it is resolved by a
        # success or a rollback. Taking a fresh snapshot needs this rather
        # than "no pending value": the flush clears the pending value when it
        # begins, not when its write comes back, so a second drag during a
        # five second publish timeout would otherwise snapshot the optimistic
        # values of the write that is still failing.
        self._powerocean_soc_cycle_open: bool = False
        # Set when a rollback lands, so the two sliders drop their optimistic
        # lock instead of sitting on a value the device refused for 5 s.
        self._powerocean_soc_rollback_generation: int = 0
        # Serializes every config write that has to read the device's current
        # state before it can build its frame. One coordinator drives one
        # device, so the ES22 and BK-series sequences can never both be in
        # flight here and share the one lock.
        #
        # On an ES22 three config fields each hold two settings and a power
        # setpoint is a remove-then-write across two frames. On a BK-series
        # Stream the SoC-limit write carries charge limit, discharge limit and
        # backup reserve together. Home Assistant runs service calls to
        # different entities concurrently, so without this two power writes
        # each remove the other's task and then write their own, leaving the
        # overlapping pair the removal exists to prevent, and the grouped
        # fields lose one of the two changes by restoring a stale companion
        # over it.
        self._device_config_lock = asyncio.Lock()
        # PowerPulse 2 start/stop (ADR-009): serializes the check-and-publish
        # of one wallbox action so a second press cannot slip past the
        # in-progress check before the first one's record is set.
        self._wallbox_action_lock = asyncio.Lock()
        self._wallbox_action_pending: WallboxActionPending | None = None
        self._credential_obtained_ts: float = 0.0
        self._credential_refresh_unsub: asyncio.TimerHandle | None = None
        self._event_log: deque[dict[str, Any]] = deque(maxlen=50)
        # Raw protobuf frame capture (app-auth push path). A parser can only
        # be verified against the bytes a device actually sends, and device
        # variants sharing a serial family do not necessarily share a field
        # layout. Frames are captured with the serial masked out and
        # truncated, so a diagnostics download stays a safe way to report a
        # mis-decoded device without owning the hardware.
        #
        # Bucketed per message type rather than kept in one ring: a device
        # pushes its live telemetry orders of magnitude more often than a
        # status report, so a shared buffer answers every download with the
        # last minute of the frequent message and nothing else. A rare
        # command now competes only with itself.
        #
        # Two depths, and which one applies is what the volunteer opt-in
        # decides. The shallow one is what every installation pays for as
        # long as it is loaded, so it stays at three frames per message type.
        # The deep one is what somebody asked for, bounded by the same 24 h
        # window that bounds the probe, and it is the depth the field work on
        # this hardware was actually done at: the three STREAM AC 5000
        # captures that produced its parser hold five to eight frames of the
        # one message type nobody had decoded, where the shallow buffer had
        # kept three.
        #
        # No wiring for the flip. Writing the flag calls async_update_entry,
        # whose update listener (registered in __init__.py) reloads the entry
        # and builds a new coordinator, so the depth is read once here and
        # never has to change underneath a live buffer. Do not add a second
        # path that resizes one.
        self._raw_frames = TypedFrameBuffer(
            RAW_FRAME_LOG_KEYS_MAX,
            RAW_FRAME_PER_KEY_MAX
            if raw_capture_window_open(entry.data)
            else RAW_FRAME_LOG_PER_KEY_MAX,
        )
        # TypedFrameBuffer.add() is not atomic the way deque.append() was, and
        # it is called from the Paho thread while diagnostics read it on the
        # event loop.
        self._raw_frames_lock = threading.Lock()
        # Field numbers a device sends that the protobuf binding does not
        # declare, per command. The frame capture above answers "what bytes
        # arrived" up to a per-frame byte budget; this answers "which fields
        # arrived" for the whole message, which is the question asked when a
        # control exists in the schema and nobody knows whether the hardware
        # reports it. Values are scalars or byte counts, never byte content -
        # see `unknown_field_summary`.
        self._unknown_proto_fields: dict[str, dict[int, Any]] = {}
        self._unknown_proto_fields_lock = threading.Lock()
        # Stable SN → pack index mapping for proto heartbeats (cmd_id=7).
        # Each heartbeat contains only one pack; this map ensures the same
        # physical pack always maps to the same pack{n}_* sensor keys.
        self._bp_sn_to_index: dict[str, int] = {}
        # Which schedule slots the device has reported. A task list carries
        # every task the device holds, so a slot that drops out of it has been
        # deleted and its keys have to be retracted - nothing else would ever
        # clear them. Written only from the MQTT ingest thread.
        self._schedule_indices: set[int] = set()
        # How many bundles carried copies of the schedule list that disagreed.
        # The first copy is taken as current; this counts how often that
        # choice mattered, so the rule can be re-judged from data rather than
        # from the single divergence it was derived from. Reported in
        # diagnostics, which is the only way that data ever reaches us.
        self._schedule_divergent_bundles: int = 0
        # Battery charge/discharge state: rolling-average derivation (#63, #50).
        # State is derived from a short moving average of signed batt_w,
        # not the instantaneous value. This filters short oscillations that
        # occur when solar production and house load balance (morning/evening),
        # where instantaneous power swings from +1000W to -300W within seconds.
        # A confirmation window requires a diverging candidate state to persist
        # before the transition is committed; min hold time additionally blocks
        # a new transition right after a commit.
        self._batt_w_samples: list[tuple[float, float]] = []  # (monotonic_ts, batt_w)
        self._batt_state_changed_at: float = 0.0  # monotonic timestamp
        self._batt_pending_state: str | None = None  # candidate awaiting confirmation
        self._batt_pending_since: float = 0.0  # monotonic ts when candidate appeared

        # Energy integrator for power → kWh Riemann sum (all device types)
        state_path = hass.config.path(f".storage/ecoflow_energy_{self.device_sn}.json")
        self._energy_integrator = EnergyIntegrator(state_path)

        # Device-specific power → energy mappings
        if self.device_type == DEVICE_TYPE_POWEROCEAN:
            self._power_to_energy = POWEROCEAN_POWER_TO_ENERGY
            self._energy_from_api = POWEROCEAN_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_DELTA:
            self._power_to_energy = DELTA_POWER_TO_ENERGY
            self._energy_from_api = DELTA_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_DELTA3:
            self._power_to_energy = DELTA3_POWER_TO_ENERGY
            self._energy_from_api = DELTA3_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_SMARTPLUG:
            self._power_to_energy = SMARTPLUG_POWER_TO_ENERGY
            self._energy_from_api = SMARTPLUG_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_STREAM:
            self._power_to_energy = STREAM_POWER_TO_ENERGY
            self._energy_from_api = STREAM_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_STREAM_AC5000:
            self._power_to_energy = STREAMAC5000_POWER_TO_ENERGY
            self._energy_from_api = STREAMAC5000_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_POWERSTREAM:
            self._power_to_energy = POWERSTREAM_POWER_TO_ENERGY
            self._energy_from_api = POWERSTREAM_ENERGY_FROM_API
        elif self.device_type == DEVICE_TYPE_WAVE3:
            self._power_to_energy = WAVE3_POWER_TO_ENERGY
            self._energy_from_api = WAVE3_ENERGY_FROM_API
        else:
            self._power_to_energy = {}
            self._energy_from_api = []

    @property
    def device_tag(self) -> str:
        """Return the one-way log tag for this device (PLAN-124, ADR-019)."""
        return device_log_tag(self.device_sn)

    @property
    def device_available(self) -> bool:
        """Return whether the device is considered reachable."""
        return self._device_available

    @property
    def device_data(self) -> dict[str, Any]:
        """Return the current device data dict."""
        return self._device_data

    def _powerocean_coordinators(self) -> list[EcoFlowDeviceCoordinator] | None:
        """The entry's PowerOcean coordinators, read fresh from `hass.data`.

        Not cached at setup, per ADR-009 decision 2: during teardown the
        entry's coordinator table is popped, and everything built on this
        list must then see no entry at all. That case returns None, and an
        entry that simply holds no PowerOcean returns an empty list - the
        two are not the same thing since PLAN-140, where "no PowerOcean" is
        a route of its own (review finding of 2026-09-12: a popped table
        read as "zero PowerOceans" would have published on the wallbox's
        own topic during teardown).
        """
        coordinators: dict[str, EcoFlowDeviceCoordinator] | None = self.hass.data.get(
            DOMAIN, {}
        ).get(self._entry.entry_id)
        if coordinators is None or coordinators.get(self.device_sn) is not self:
            return None
        return [
            coordinator
            for coordinator in coordinators.values()
            if coordinator.device_type == DEVICE_TYPE_POWEROCEAN
        ]

    def _linked_unit_coordinator(self, serial: str) -> EcoFlowDeviceCoordinator | None:
        """The entry's coordinator that carries `serial`, read fresh from `hass.data`.

        A per-unit STREAM block (PLAN-145, #401) travels on whichever
        linked unit's connection happened to deliver it, and the
        coordinator that must publish an entry is the one whose serial it
        names - not "which coordinators are of this device type", which is
        the question `_powerocean_coordinators()` answers. That makes this
        a lookup of its own rather than a filter on that one, returning the
        matching coordinator or None rather than a list. No device-type
        check: a serial that sits in this entry's table is one of this
        account's own coordinators, and the per-unit block only ever lists
        STREAM serials.

        None in all three cases - the table popped, `self` no longer in it,
        or `serial` absent - is "drop": unlike a PowerPulse 2 without a
        PowerOcean, there is no alternate route for an entry nobody here
        carries. The caller counts the drop and does not log it.
        """
        coordinators: dict[str, EcoFlowDeviceCoordinator] | None = self.hass.data.get(
            DOMAIN, {}
        ).get(self._entry.entry_id)
        if coordinators is None or coordinators.get(self.device_sn) is not self:
            return None
        return coordinators.get(serial)

    def apply_linked_unit_entry(self, parsed: dict[str, Any]) -> None:
        """Apply a per-unit entry a linked sibling's connection delivered for us.

        `parsed` is the parser's own shape restricted to this device's own
        serial - `{UNIT_PV_BY_SN_KEY: {sn: five keys}, UNIT_POWER_BY_SN_KEY:
        {sn: watts}}`, either or both - handed over by the sibling whose
        connection actually carried the block (PLAN-145, #401). A thin call
        to `_apply_data` with `own_connection=False`, so the value goes
        through the identical apply path this device's own frames use, and
        the sibling's connection never stands in for this device's own
        liveness.
        """
        self._apply_data(parsed, own_connection=False)

    def powerocean_sibling(self) -> EcoFlowDeviceCoordinator | None:
        """Return the entry's one PowerOcean coordinator, or None (ADR-009).

        Returns None with zero PowerOcean coordinators and also with two or
        more (no way to tell which one carries this wallbox) - decision 3.
        `charge_action_route()` tells the two apart.
        """
        matches = self._powerocean_coordinators()
        if matches is None or len(matches) != 1:
            return None
        return matches[0]

    def charge_action_route(self) -> Literal["sibling", "own"] | None:
        """Which route a PowerPulse 2 start/stop command takes (PLAN-140).

        `"sibling"`: exactly one PowerOcean in the entry - `241/100` on its
        set topic, addressed to the wallbox accessory (ADR-009 decisions
        1-3). `"own"`: no PowerOcean in the entry - `2/81` on the wallbox's
        own set topic (ADR-009 addendum of 2026-09-12, from one recording of
        the app doing exactly that). `None`: two or more PowerOceans, where
        the relayed command has no way to pick the parent and the own-topic
        command has not been observed on such an account - and also while
        the entry is torn down and its coordinator table is gone, so a press
        racing the unload sends nothing on either route. Resolved fresh on
        every call, like `powerocean_sibling()`.
        """
        matches = self._powerocean_coordinators()
        if matches is None:
            return None
        if len(matches) == 1:
            return "sibling"
        if not matches:
            return "own"
        return None

    @property
    def last_value_change_ts(self) -> float:
        """Monotonic time a device value last changed (0 = never seen one)."""
        return self._last_value_change_ts

    @property
    def unchanged_updates(self) -> int:
        """Consecutive updates that carried no value this device had not sent."""
        return self._unchanged_updates

    @property
    def schedule_divergent_bundles(self) -> int:
        """Bundles whose repeated schedule-list copies disagreed."""
        return self._schedule_divergent_bundles

    @property
    def surplus_auto_sync_diagnostics(self) -> dict[str, int | bool] | None:
        """Return the surplus auto-sync record, or None (ADR-013).

        None on every device that never runs the auto-sync (it is only
        scheduled for a PowerOcean in Enhanced Mode) and equally None once
        no divergent pair is on record - the two cases a diagnostics reader
        cannot otherwise tell apart from an absent key.
        """
        if self._surplus_sync_record is None:
            return None
        return dict(self._surplus_sync_record)

    def _note_value_change(self, parsed: dict[str, Any]) -> None:
        """Record whether this update carried anything the device had not sent.

        Compared key by key against what is already held, so a poll that
        returns a byte-identical payload counts as nothing new even though it
        succeeded. That is the distinction the caller cannot make: an HTTP poll
        that returns the cloud's stored copy looks exactly like one that
        returns a fresh reading.

        Called before the merge, since afterwards every key compares equal.
        """
        for key, value in parsed.items():
            if key.startswith("_"):
                continue
            if key not in self._device_data or self._device_data[key] != value:
                self._last_value_change_ts = time.monotonic()
                self._unchanged_updates = 0
                return
        self._unchanged_updates += 1

    @property
    def snapshot(self) -> DeviceSnapshot:
        """Return the latest device data snapshot."""
        return self._snapshot

    @property
    def raw_quota(self) -> dict[str, Any]:
        """Return the raw HTTP quota snapshot (Delta 3 only, else empty)."""
        return self._raw_quota

    @property
    def raw_quota_captured_at(self) -> float:
        """Return monotonic timestamp of the raw quota capture (0 = never)."""
        return self._raw_quota_captured_at

    @property
    def firmware(self) -> dict[str, dict[str, Any]]:
        """Return firmware revisions per subsystem (empty if none reported)."""
        return self._firmware

    @property
    def linked_unit_stats(self) -> dict[str, Any] | None:
        """Return what the last STREAM per-unit block held, or None if unseen.

        The four `*_listed` / `own_*_matched` fields describe the last block
        seen on this device's own connection. The `*_handed_over` /
        `*_received` / `*_held` / `*_unrouted` / `*_handoff_failed` fields
        (PLAN-145, #401) are running totals since start, covering both
        directions: entries this coordinator passed to a linked sibling, and
        entries a sibling handed to this coordinator. For diagnostics.
        Carries counts only, never a serial.
        """
        return None if self._unit_power_stats is None else dict(self._unit_power_stats)

    @property
    def energy_state(self) -> dict[str, tuple[float, float, float]]:
        """Return the running energy integrator state, for diagnostics.

        The live state, not the file the integrator persists to. The file is
        written at most once a minute, so a reader of the file can be a minute
        behind - and a section that exists to explain a counter standing still
        must not itself report a stale number.
        """
        return self._energy_integrator.state_snapshot()

    def set_device_value(self, key: str, value: Any) -> None:
        """Set a single value in the persistent device data store.

        Used by entity platforms (e.g. number) for optimistic updates that
        must survive coordinator refresh cycles.
        """
        self._device_data[key] = value

    @property
    def enhanced_mode(self) -> bool:
        """Return whether Enhanced Mode is active."""
        return self._enhanced_mode

    @property
    def last_mqtt_ts(self) -> float:
        """Return the timestamp of the last MQTT message."""
        return self._last_mqtt_ts

    @property
    def mqtt_client(self) -> EcoFlowMQTTClient | None:
        """Return the MQTT client (or None if not set up)."""
        return self._mqtt_client

    @property
    def raw_frames(self) -> list[dict[str, Any]]:
        """Return the captured raw protobuf frames for diagnostics export."""
        with self._raw_frames_lock:
            return self._raw_frames.frames()

    @property
    def raw_frame_sampling(self) -> dict[str, Any]:
        """Return what the capture heard versus what it kept.

        A short frame list has two very different causes - a device that
        pushes rarely, and one so chatty that the sampling thinned it out -
        and a reader cannot tell them apart from the frames alone.
        """
        with self._raw_frames_lock:
            return self._raw_frames.stats()

    def raw_frame_capture(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Return the kept frames and the sampling counts from one read.

        Taken under a single acquisition on purpose. The counts exist so a
        reader can reconcile the frame list against what the device actually
        sent, and reading the two halves separately lets the Paho thread add
        a frame in between - which is exactly the discrepancy the counts are
        supposed to explain away.
        """
        with self._raw_frames_lock:
            return self._raw_frames.frames(), self._raw_frames.stats()

    @property
    def app_writes_watched(self) -> bool:
        """Return whether the vendor app's writes are being recorded.

        Read from the live client rather than from the config entry. The
        two agree today - the flag is read once when the client is built,
        and writing it reloads the entry - but only one of them is the
        subscription that either happened or did not, and that is the
        question a diagnostics reader is asking.
        """
        client = self._mqtt_client
        return bool(client is not None and client.capture_writes)

    def record_unknown_proto_fields(self, cmd_key: str, fields: dict[int, Any]) -> None:
        """Merge one message's undeclared field numbers into the running set.

        Called from the Paho thread for every decoded push frame. The newest
        sample wins for a field already seen, so the summary tracks a value
        that changes rather than freezing whatever arrived first - which
        matters when a reporter is asked to change a setting in the app and
        report which number moved.

        Both axes are capped, and the second one is the one that matters: the
        decoder limits how many numbers a single message contributes, but this
        summary accumulates over every message for as long as the integration
        runs. A field number already known keeps updating past the cap, so a
        full summary still tracks the values it holds - it only stops taking
        on new numbers.
        """
        if not fields:
            return
        with self._unknown_proto_fields_lock:
            known = self._unknown_proto_fields.get(cmd_key)
            if known is None:
                # A device sending more command types than this is not the
                # case this was built for.
                if len(self._unknown_proto_fields) >= UNKNOWN_FIELD_CMDS_MAX:
                    return
                known = {}
                self._unknown_proto_fields[cmd_key] = known
            for number, value in fields.items():
                if number in known or len(known) < UNKNOWN_FIELD_NUMBERS_MAX:
                    known[number] = value

    @property
    def unknown_proto_fields(self) -> dict[str, dict[str, Any]]:
        """Return the undeclared field numbers seen per command, for diagnostics.

        Field numbers are stringified because this ends up in a JSON download,
        and they are sorted numerically so two dumps from the same device can
        be diffed by eye.
        """
        with self._unknown_proto_fields_lock:
            return {
                cmd_key: {str(number): fields[number] for number in sorted(fields)}
                for cmd_key, fields in sorted(self._unknown_proto_fields.items())
            }

    @property
    def event_log(self) -> list[dict[str, Any]]:
        """Return the event history for diagnostics export."""
        return list(self._event_log)

    def _log_event(self, event_type: str, detail: str) -> None:
        """Record an event for diagnostics (bounded FIFO, max 50 entries)."""
        self._event_log.append(
            {
                "ts": time.time(),
                "type": event_type,
                "detail": detail,
            }
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for all entities of this device."""
        auth_method = self._auth_method
        config_url = (
            "https://ecoflow.com"
            if auth_method == AUTH_METHOD_APP
            else "https://developer.ecoflow.com"
        )
        info = DeviceInfo(
            identifiers={(DOMAIN, self.device_sn)},
            manufacturer="EcoFlow",
            model=self.product_name,
            name=f"EcoFlow {self.device_name}",
            configuration_url=config_url,
        )
        if self._sw_version:
            info["sw_version"] = self._sw_version
        return info
