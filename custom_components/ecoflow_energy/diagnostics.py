"""Diagnostics support for EcoFlow Energy.

Exposes device status, MQTT connectivity, and data freshness.
NEVER exposes credentials (access_key, secret_key, email, password, certificates).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTH_METHOD_DEVELOPER,
    CONF_ACCESS_KEY,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_MODE,
    CONF_SECRET_KEY,
    DATA_DEVICE_PROBES,
    DATA_SKIPPED_DEVICES,
    DEVICE_TYPE_DELTA3,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    RAW_FRAME_MAX_BYTES,
)
from .coordinator import EcoFlowDeviceCoordinator
from .ecoflow.cloud_http import EcoFlowHTTPQuota

_LOGGER = logging.getLogger(__name__)

REDACTED = "**REDACTED**"

# EcoFlow serial numbers are 16-char alphanumeric strings (e.g. D3M1TEST...).
# Any quota value containing a run matching this shape is redacted so a
# diagnostics dump never leaks a full device serial. The pattern is applied
# unanchored so serials embedded in longer strings are caught too.
_SERIAL_RE = re.compile(r"[A-Z0-9]{15,}")

# Values under these keys are captured protobuf frames, hex-encoded. They are
# masked at capture time by sanitize_frame, which preserves length so the
# frame stays decodable, and they must not be run through the pattern above a
# second time. Hex digits 0-9 are inside [A-Z0-9], so any run of fifteen or
# more hex characters that happens to carry no a-f matches it. Measured
# against this repo's own fixtures when the whole payload was first passed
# through redaction: 25 of 25 captured frames came out corrupted. That is the
# artefact device support is built from, so destroying it is worse than the
# leak the pass exists to prevent.
_PRE_SANITIZED_KEYS = frozenset({"hex", "frame_hex"})


def _redact_serials(value: Any, aliases: dict[str, str] | None = None) -> Any:
    """Redact values that look like EcoFlow serial numbers.

    Recurses into dict and list values so nested quota structures (e.g.
    ``powGetAcOutList``) cannot smuggle a serial past redaction. Over-redaction
    of long alphanumeric tokens is accepted by design: a diagnostics dump must
    never leak a device serial.

    Dictionary **keys** are redacted as well. The PowerOcean quota addresses
    battery packs by serial in the key itself (``bp_addr.<SN>``), so redacting
    values alone would still publish a serial in a dump users are asked to
    attach to a public issue.

    Distinct serials get distinct placeholders. Replacing all of them with one
    constant made two battery packs collapse onto the same redacted key, and a
    dict comprehension keeps only the last one - so a system with two packs
    reported one, silently, in the artefact used to answer questions about how
    many packs it has. The first serial seen keeps the bare marker so the
    common single-serial case reads as before; every further distinct serial
    is numbered. The mapping lives for one redaction pass, so the same serial
    reads the same throughout a dump and nothing carries over between dumps.

    Captured frames are the one exception, see ``_PRE_SANITIZED_KEYS``.
    """
    if aliases is None:
        aliases = {}
    if isinstance(value, str):

        def _alias(match: re.Match[str]) -> str:
            serial = match.group(0)
            if serial not in aliases:
                aliases[serial] = (
                    REDACTED if not aliases else f"**REDACTED-{len(aliases) + 1}**"
                )
            return aliases[serial]

        return _SERIAL_RE.sub(_alias, value)
    if isinstance(value, dict):
        return {
            _redact_serials(key, aliases): (
                item if key in _PRE_SANITIZED_KEYS else _redact_serials(item, aliases)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_serials(item, aliases) for item in value]
    return value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinators: dict[str, EcoFlowDeviceCoordinator] = hass.data.get(DOMAIN, {}).get(
        entry.entry_id, {}
    )

    devices_diag: list[dict[str, Any]] = []
    for coordinator in coordinators.values():
        devices_diag.append(_device_diagnostics(coordinator))

    skipped_devices = hass.data.get(DATA_SKIPPED_DEVICES, {}).get(entry.entry_id, [])
    probes = hass.data.get(DATA_DEVICE_PROBES, {}).get(entry.entry_id, [])

    diagnostics = {
        "config_entry": {
            "auth_method": entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_DEVELOPER),
            "mode": entry.data.get(CONF_MODE, "standard"),
            "device_count": len(entry.data.get(CONF_DEVICES, [])),
            "access_key": REDACTED,
            "secret_key": REDACTED,
            "email": REDACTED,
            "password": REDACTED,
        },
        "devices": devices_diag,
        "skipped_devices": await _skipped_devices_diagnostics(
            hass, entry, skipped_devices, probes
        ),
    }
    # One redaction pass over everything, on the way out. Each section used to
    # redact itself, which works only for the paths somebody remembered: three
    # leaks in one release cycle were all in a section next to the one being
    # changed, and the last of them published the full serial and the account
    # id of every device write through the event log. A section added later
    # is covered here whether or not its author thought about it.
    return _redact_serials(diagnostics)


async def _skipped_devices_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    skipped_devices: list[dict[str, Any]],
    probes: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build diagnostics for unsupported/skipped devices.

    For a device we do not yet parse, capture its raw HTTP quota so a parser
    can be built from real API fields without owning the hardware. The full
    serial is used only to sign the read-only quota request and is never
    included in the output — only the SN prefix is exposed. Serial-looking
    values inside the quota are redacted.

    Requires developer credentials (access key + secret key). In enhanced /
    app-auth mode those are absent, so the quota is omitted with a note.
    """
    access_key = entry.data.get(CONF_ACCESS_KEY)
    secret_key = entry.data.get(CONF_SECRET_KEY)
    has_dev_creds = bool(access_key and secret_key)
    session = async_get_clientsession(hass) if has_dev_creds else None

    probe_by_sn = {probe.device_sn: probe for probe in (probes or [])}

    result: list[dict[str, Any]] = []
    for item in skipped_devices:
        out: dict[str, Any] = {
            "sn_prefix": item.get("sn_prefix"),
            "product_name": item.get("product_name"),
            "reason": item.get("reason"),
        }

        probe = probe_by_sn.get(item.get("sn"))
        if probe is not None:
            # The app channel is the only route for models the Developer API
            # refuses (error 1006), so what the probe heard is the whole
            # evidence base for adding support.
            #
            # `sampling` is what makes the frame list readable: the frames are
            # a sample per message type, not everything that arrived, and
            # without the counts a thinned six-hour capture and a nearly silent
            # device look the same.
            frames = probe.frames
            out["raw_capture"] = {
                # `connection` carries the whole story of the link, including
                # why it is down. A bare "connected: false" was the entire
                # report before, and an empty capture then had no readable
                # cause - the recording looked like a device that says
                # nothing, which a listen-only session demonstrably is not
                # (a device at rest sends a frame every couple of seconds).
                **probe.connection,
                "frame_count": len(frames),
                "topics": probe.topics,
                "truncated_at_bytes": RAW_FRAME_MAX_BYTES,
                "sampling": probe.sampling,
                "frames": _format_event_log(frames),
            }
        else:
            # Say so explicitly. A missing section would be indistinguishable
            # from a version that has no capture at all, and the reader would
            # have no way to tell that the login or the connection was what
            # failed.
            out["raw_capture"] = {
                "status": "no probe running for this device",
                # A probe that failed to connect is kept and reports its own
                # reason above, so this branch now means one thing only: the
                # account login never got far enough to start one.
                "hint": (
                    "requires EcoFlow account login; signing in or fetching "
                    "the connection credentials did not succeed"
                ),
            }

        if not has_dev_creds:
            out["quota_note"] = (
                "developer credentials required to capture raw quota "
                "(device is in enhanced/app-auth mode)"
            )
            result.append(out)
            continue

        sn = item.get("sn")
        response: dict | None = None
        if sn:
            try:
                client = EcoFlowHTTPQuota(session, access_key, secret_key, sn)
                response = await client.get_quota_all()
            except Exception:  # noqa: BLE001
                # A skipped-device quota fetch failing is expected and not
                # actionable (unsupported model, transient API error), so it
                # stays at debug level and never breaks the download. The SN
                # and any exception detail are deliberately kept out of logs.
                _LOGGER.debug("Skipped-device quota fetch failed")
                response = None

        if response is None:
            out["quota_note"] = "quota fetch unavailable"
        else:
            # get_quota_all() already returns the flat quota dict (the client
            # unwraps the API envelope), so redact and expose it directly.
            out["raw_quota"] = _redact_serials(response)

        result.append(out)

    return result


def _device_diagnostics(coordinator: EcoFlowDeviceCoordinator) -> dict[str, Any]:
    """Build diagnostics dict for one device — no credentials."""
    now = time.monotonic()

    mqtt_client = coordinator.mqtt_client
    mqtt_connected = False
    mqtt_uptime_s: float | None = None
    mqtt_reconnect_attempts = 0
    if mqtt_client is not None:
        mqtt_connected = mqtt_client.is_connected()
        if mqtt_connected and mqtt_client.last_connect_time > 0:
            mqtt_uptime_s = round(now - mqtt_client.last_connect_time, 1)
        mqtt_reconnect_attempts = mqtt_client.reconnect_attempts

    last_mqtt_age_s: float | None = None
    if coordinator.last_mqtt_ts > 0:
        last_mqtt_age_s = round(now - coordinator.last_mqtt_ts, 1)

    data_keys = sorted(coordinator.device_data.keys()) if coordinator.device_data else []

    snapshot = coordinator.snapshot
    snapshot_age_s: float | None = None
    if snapshot.captured_at > 0:
        snapshot_age_s = round(now - snapshot.captured_at, 1)

    diag: dict[str, Any] = {
        # SN prefix only (privacy) - mirrors the skipped-device convention
        "device_sn": coordinator.device_sn[:4] + "...",
        "device_name": coordinator.device_name,
        "product_name": coordinator.product_name,
        "enhanced_mode": coordinator.enhanced_mode,
        "availability_stage": coordinator.availability_stage,
        "mqtt_status": {
            "status": coordinator.mqtt_status,
            "connected": mqtt_connected,
            "data_receiving": coordinator.data_receiving,
            "uptime_s": mqtt_uptime_s,
            "reconnect_attempts": mqtt_reconnect_attempts,
            "wss_mode": mqtt_client.wss_mode if mqtt_client else False,
        },
        "data_freshness": {
            "last_mqtt_age_s": last_mqtt_age_s,
            "update_interval": str(coordinator.update_interval) if coordinator.update_interval else None,
            "http_fallback_active": bool(
                coordinator.enhanced_mode and coordinator.update_interval is not None
            ),
        },
        "snapshot": {
            "source": snapshot.source or "none",
            "age_s": snapshot_age_s,
            "key_count": snapshot.key_count,
            "captured": snapshot.captured_at > 0,
        },
        "data_keys": data_keys,
        "data_key_count": len(data_keys),
        # Firmware revisions the quota reported, per subsystem. Empty in
        # Enhanced Mode (no quota poll) and on device families that report no
        # revision at all - PowerOcean sends 347 quota keys and none is a
        # version. Those owners have to read it off the EcoFlow app, which is
        # what the bug report template asks for.
        # Redacted like every other quota-derived section: the PowerOcean quota
        # addresses battery packs by serial in the key itself, and a subsystem
        # revision could arrive under such a key.
        "firmware": _redact_serials(coordinator.firmware),
        "event_log": _format_event_log(coordinator.event_log),
    }

    # Enhanced Mode: the raw push frames the device sent, with the serial
    # masked and each frame truncated. Device variants within a serial family
    # do not always share a field layout, so a mis-decoded variant can only be
    # diagnosed against the bytes it actually sends.
    # One read for both halves: the counts only reconcile against the frame
    # list if they describe the same state of the buffer.
    raw_frames, raw_sampling = coordinator.raw_frame_capture()
    if raw_frames:
        diag["raw_frames"] = {
            "count": len(raw_frames),
            "truncated_at_bytes": RAW_FRAME_MAX_BYTES,
            # Frames are sampled per message type, so the list is a selection
            # rather than a tail. Without this a reader cannot tell a quiet
            # device from a thinned-out capture.
            "sampling": raw_sampling,
            "frames": _format_event_log(raw_frames),
        }

    # Delta 3, Stream and PowerOcean: the quota field maps are
    # community-researched but not yet hardware-verified for every key. Expose
    # the raw quota key/value snapshot so a diagnostics dump can confirm
    # existing mappings and reveal keys still to be added. For PowerOcean this
    # is also the only way to see what an attached accessory contributes, since
    # accessories report through the PowerOcean quota instead of as devices of
    # their own. Serial-looking values are redacted.
    if coordinator.device_type in (
        DEVICE_TYPE_DELTA3,
        DEVICE_TYPE_STREAM,
        DEVICE_TYPE_POWEROCEAN,
    ):
        raw_quota = coordinator.raw_quota
        raw_age_s: float | None = None
        if coordinator.raw_quota_captured_at > 0:
            raw_age_s = round(now - coordinator.raw_quota_captured_at, 1)
        diag["raw_quota"] = {
            "captured": bool(raw_quota),
            "age_s": raw_age_s,
            "key_count": len(raw_quota),
            "values": {
                _redact_serials(key): _redact_serials(value)
                for key, value in sorted(raw_quota.items())
            },
        }

    # Enhanced Mode: the field numbers the device sent that our protobuf
    # binding does not declare. A field the binding does not know is dropped
    # silently before it reaches any sensor, so without this there is no way
    # to tell a device that does not report a value from one that reports it
    # into a gap in our schema - and that is the question every "can this be
    # controlled from Home Assistant" request runs into first.
    #
    # Raw frames answer the same question in principle, but they are cut at
    # 512 bytes and reading field numbers out of a hex dump is not something
    # to ask of a reporter.
    unknown_fields = coordinator.unknown_proto_fields
    if unknown_fields:
        diag["unknown_proto_fields"] = {
            "note": (
                "Field numbers this device sent that the integration does not "
                "decode, keyed by cmd_func/cmd_id. Scalar values are shown as "
                "sent; other fields are shown by byte length only."
            ),
            "commands": unknown_fields,
        }

    return diag


def _format_event_log(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format event log with ISO timestamps for human readability."""
    formatted = []
    for event in events:
        entry = dict(event)
        ts = entry.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            entry["ts_iso"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        formatted.append(entry)
    return formatted
