"""Diagnostics support for EcoFlow Energy.

Exposes device status, MQTT connectivity, and data freshness.
NEVER exposes credentials (access_key, secret_key, email, password, certificates).
"""

from __future__ import annotations

import base64
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
    DEVICE_TYPE_POWERSTREAM,
    DEVICE_TYPE_STREAM,
    DOMAIN,
    RAW_FRAME_BUNDLE_MAX_BYTES,
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

# EcoFlow's own app base64-encodes some fields that carry a raw serial
# rather than sending it as plain text - found in the raw quota of two of
# the maintainer's own downloads from 2026-08-12: four PowerOcean serials
# under `moduleSn` keys inside the skipped-device quota, one of them
# `SEozN1pESDVaRzdBMDI5Ng==` decoding to a system serial. The pattern above
# never sees those on its own: a base64 string does not read as sixteen
# consecutive uppercase-alphanumeric characters.
#
# The decode has to run ahead of the plain pass on the same string, not
# after it. Base64-encoding a sixteen-character serial produces a run of
# fifteen or more uppercase-and-digit characters often enough to matter: of
# 200,000 randomly generated sixteen-character serials, 13,064 (6.5%) encode
# to a base64 form the plain pattern above already matches on its own. Run
# the plain pass first and that fraction never reaches the decode step at
# all - the substitution has already cut the string into a fragment no
# decode recognises by the time anything tries to base64-decode it.
#
# The step that asserts the decoded bytes are ASCII is the one doing the
# real work, not a formality. Any hex string of even length is already
# valid base64 - its alphabet is a subset - so the shape check alone rejects
# nothing that looks like a captured protobuf frame or a raw quota hex blob.
# Measured over every string this pass actually reaches in the fixtures and
# in the local downloads, so outside the pre-sanitised frame keys below:
# 7,196 values pass the shape check, none fails the decode, 7,188 fail on
# ASCII, and the 8 that survive are the encoded serials this pass exists
# for. Counting the skipped frame values too gives 7,980 and 7,188, which
# is the same finding over a population this pass never judges.
#
# The full match below covers the same 7,188 on today's data, because
# replacing an undecodable byte leaves a character no uppercase-alphanumeric
# run accepts. That redundancy does not hold for every lenient decoding:
# dropping such bytes instead of replacing them turns a serial with one
# stray byte in front of it into a clean match, so the ASCII step is what
# keeps this pass to whole encoded serials rather than to serials found
# inside other bytes. It is also the cheaper of the two. What the full match is NOT redundant for is a
# decoded value that is ASCII and contains a serial among other text: it
# must be rejected rather than replaced whole, and only an anchored match
# does that. No such value is on file; a test pins it.
_BASE64_MIN_LEN = 20
_BASE64_SHAPE_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")


def _looks_like_base64(value: str) -> bool:
    """Cheap shape pre-check: alphabet, padding position, and length only.

    Run before the real decode below so a plain string never pays for a
    decode attempt it cannot pass. Length must be at least twenty and a
    multiple of four, and twenty is the shortest form any value this pass
    can accept: the serial pattern starts at fifteen characters, and
    fifteen bytes encode to exactly twenty. Raising this floor would
    therefore stop masking the shortest serials the pattern recognises,
    silently, which is why a test pins the fifteen-character case. Padding,
    if present, is only valid at the very end and at most two characters,
    which is what the trailing `={0,2}` enforces.
    """
    if len(value) < _BASE64_MIN_LEN or len(value) % 4 != 0:
        return False
    return bool(_BASE64_SHAPE_RE.fullmatch(value))


def _decode_base64_serial(value: str) -> str | None:
    """Return the serial ``value`` base64-encodes, or ``None``.

    Four ordered gates, each cheaper than the next: shape, then the real
    decode, then an ASCII check on the decoded bytes, then a full match
    against ``_SERIAL_RE`` so decoded text that merely happens to be ASCII
    (a stray word, a snippet of JSON) is not mistaken for a serial.

    One consequence, since both forms of a serial share one marker by
    design: a dict that keys one entry by a serial and another by that
    same serial encoded collapses to a single entry, because the two keys
    now redact to the same string. No section builds keys that way - the
    values found were values, and the per-pack keys carry a prefix - but
    it is a real narrowing and a test pins it.
    """
    if not _looks_like_base64(value):
        return None
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        return None
    try:
        text = decoded.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not _SERIAL_RE.fullmatch(text):
        return None
    return text


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

# EcoFlow names a device after its model plus the last four characters of its
# serial, so "Stream AC Pro (0362)" carries the end of a serial whose start is
# published one line above it as the SN prefix. The pattern above never sees
# that: four characters do not reach fifteen. Values under these keys are
# therefore masked a second time, against the serials this entry actually
# holds. Keying on the field rather than on the section keeps the single pass
# single: a section added later that reuses these two field names is covered
# whether or not its author thought about it. It does not cover a section that
# dumps the stored device dict, where the same value is spelled ``name`` - a
# word too generic to mask everywhere it occurs.
_NAME_KEYS = frozenset({"device_name", "product_name"})

# How much of a serial a generated name carries.
_NAME_SERIAL_TAIL = 4

# The markers this module writes. The serial pass runs before the tail pass on
# the same string, so a tail spelled like four characters of a marker would
# chew a marker the first pass had just written. The tail pattern matches the
# markers first and leaves them alone.
_MARKER_PATTERN = r"\*\*REDACTED(?:-\d+)?\*\*"

# How much of a frame is kept, by what the frame carries: a bundle of several
# messages gets the larger budget, everything else the smaller one. Reported
# as a pair rather than a single number because one number would describe
# neither kind of frame correctly.
#
# The shape also dates the capture. Downloads up to v1.16.0 carry a plain
# integer here and mark nothing per frame, so a cut frame in one of those can
# only be found by comparing `size` against the length of `hex`. From here on
# a cut frame says `truncated` itself.
_FRAME_BUDGETS = {
    "message": RAW_FRAME_MAX_BYTES,
    "bundle": RAW_FRAME_BUNDLE_MAX_BYTES,
}

# What the three numbers behind each energy counter mean, and how to read
# them together. Written for whoever opens the download - a maintainer months
# from now, or a reporter who was asked to attach it.
_ENERGY_NOTE = (
    "Energy counters this integration derives from power readings, as they "
    "stand now rather than as last written to disk. `total_kwh` is the "
    "running total, `last_power_w` the reading last integrated into it, and "
    "`age_s` how long ago that happened. A total near zero with a small "
    "`age_s` means the device reports little or no power; the same total with "
    "a large `age_s` means the power reading stopped arriving. An age counts "
    "from the last reading or from the last host reboot, whichever is later, "
    "since the clock behind it restarts with the host. Counters the device "
    "reports as a total of its own appear here too, and for those "
    "`last_power_w` says nothing."
)

# Six decimal places, and not fewer. The question this section answers is
# whether a counter is at a true zero (seeded once, never integrated again) or
# creeping up in tiny steps, and one integration step at 150 W over three
# seconds is 0.000125 kWh. Rounding to the two places used elsewhere would
# collapse exactly the distinction the section exists for.
_ENERGY_TOTAL_DIGITS = 6


def _redact_serials(
    value: Any,
    aliases: dict[str, str] | None = None,
    tails: tuple[str, ...] = (),
) -> Any:
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

    ``tails`` carries the serial endings a generated device name can hold; the
    fields listed in ``_NAME_KEYS`` are masked against them as well, since a
    four-character ending is far below what the serial pattern matches.
    """
    if aliases is None:
        aliases = {}
    if isinstance(value, str):

        def _alias(serial: str) -> str:
            if serial not in aliases:
                aliases[serial] = (
                    REDACTED if not aliases else f"**REDACTED-{len(aliases) + 1}**"
                )
            return aliases[serial]

        decoded_serial = _decode_base64_serial(value)
        if decoded_serial is not None:
            return _alias(decoded_serial)

        return _SERIAL_RE.sub(lambda match: _alias(match.group(0)), value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            redacted_key = _redact_serials(key, aliases, tails)
            if key in _PRE_SANITIZED_KEYS:
                redacted[redacted_key] = item
                continue
            redacted_item = _redact_serials(item, aliases, tails)
            if key in _NAME_KEYS and isinstance(redacted_item, str):
                redacted_item = _mask_serial_tails(redacted_item, tails)
            redacted[redacted_key] = redacted_item
        return redacted
    if isinstance(value, list):
        return [_redact_serials(item, aliases, tails) for item in value]
    return value


def _serial_tails(serials: list[str]) -> tuple[str, ...]:
    """Return the serial endings a generated device name can carry.

    The threshold is the published prefix length plus the tail length, so
    only serials whose two ends do not overlap are used. Nothing observed is
    shorter than sixteen characters, so this guard is about a shape that could
    arrive rather than one that has.
    """
    tails: list[str] = []
    for serial in serials:
        if not serial or len(serial) < _NAME_SERIAL_TAIL * 2:
            continue
        tail = serial[-_NAME_SERIAL_TAIL:]
        if tail not in tails:
            tails.append(tail)
    return tuple(tails)


def _mask_serial_tails(text: str, tails: tuple[str, ...]) -> str:
    """Replace every known serial ending in one pass.

    One alternation rather than a substitution per tail, so a marker written
    for the first tail cannot be matched again by the second. The markers the
    serial pass may already have written are matched first and returned
    untouched, for the same reason.

    An empty set of tails returns immediately, since there is nothing to mask.
    That is a short cut rather than a safeguard: the marker alternative is
    always present, so the pattern is never the empty alternation that would
    match at every position.
    """
    if not tails:
        return text
    pattern = re.compile(
        "|".join([_MARKER_PATTERN, *(re.escape(tail) for tail in tails)]),
        re.IGNORECASE,
    )

    def _keep_markers(match: re.Match[str]) -> str:
        found = match.group(0)
        return found if found.startswith("**") else REDACTED

    return pattern.sub(_keep_markers, text)


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
    serials = [coordinator.device_sn for coordinator in coordinators.values()]
    serials += [item.get("sn", "") for item in skipped_devices]
    return _redact_serials(diagnostics, tails=_serial_tails(serials))


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
    included in the output - only the SN prefix is exposed. Serial-looking
    values inside the quota are redacted by the single pass on the way out
    of ``async_get_config_entry_diagnostics``.

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

        probe_eligible = item.get("probe_eligible", True)
        probe = probe_by_sn.get(item.get("sn")) if probe_eligible else None
        if not probe_eligible:
            out["raw_capture"] = {
                "status": "not attempted",
                "hint": "device requires Standard Mode; app-auth probe not started",
            }
        elif probe is not None:
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
                "truncated_at_bytes": _FRAME_BUDGETS,
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
            if not probe_eligible:
                out["quota_note"] = (
                    "not attempted: device requires Standard Mode"
                )
            else:
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
                response = await client.get_quota_all(diagnostic=True)
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
            # unwraps the API envelope), so expose it directly. Serials in it
            # are caught by the single redaction pass on the way out.
            out["raw_quota"] = response

        result.append(out)

    return result


def _energy_integrator_diagnostics(
    coordinator: EcoFlowDeviceCoordinator, now: float
) -> dict[str, Any]:
    """Report every energy counter the integrator holds, with its age.

    Every metric in the state is listed, including one whose total is still
    zero. A metric that was seeded once and never integrated again is the
    interesting case - a section showing only counters that are running would
    hide precisely the ones somebody is asking about.

    `age_s` replaces the stored timestamp rather than accompanying it. That
    timestamp comes from `time.monotonic()`, whose epoch is arbitrary, so the
    raw value tells a reader nothing on its own and invites being misread as
    a wall clock. Every other age in this file is reported the same way.

    Never raises: a diagnostics download is what a reporter has been asked for
    when something is already wrong, and losing the whole file over one
    unreadable counter would be the worst possible moment for it.
    """
    try:
        state = coordinator.energy_state
        items = sorted(state.items())
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Energy integrator state unavailable for diagnostics")
        return {"note": _ENERGY_NOTE, "metrics": {}}

    metrics: dict[str, Any] = {}
    for metric, values in items:
        try:
            total_kwh, last_ts, last_power_w = values
            metrics[metric] = {
                "total_kwh": round(float(total_kwh), _ENERGY_TOTAL_DIGITS),
                "last_power_w": round(float(last_power_w), 1),
                "age_s": round(now - float(last_ts), 1),
            }
        except (TypeError, ValueError):
            # Name the metric anyway. Dropping it would read as "this counter
            # does not exist", which is a different answer entirely.
            metrics[metric] = {"error": "state entry could not be read"}

    return {"note": _ENERGY_NOTE, "metrics": metrics}


def _device_diagnostics(coordinator: EcoFlowDeviceCoordinator) -> dict[str, Any]:
    """Build diagnostics dict for one device - no credentials."""
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

    last_value_change_age_s: float | None = None
    if coordinator.last_value_change_ts > 0:
        last_value_change_age_s = round(now - coordinator.last_value_change_ts, 1)

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
            # Which server this device is actually dialling. The address
            # comes from the account's own credentials, so an account
            # served from another region uses a different one - and when
            # that goes wrong the device simply never comes online, which
            # looks the same from the outside as a device with nothing to
            # say (issue #184). The unsupported-device capture reports the
            # same field; a supported device needs it just as much.
            "broker": mqtt_client.broker if mqtt_client else None,
        },
        "data_freshness": {
            "last_mqtt_age_s": last_mqtt_age_s,
            # How often we ask. On its own this says nothing about whether the
            # answers carry anything new, which is why the two fields below
            # exist: a Standard Mode poll returning the cloud's stored copy
            # looks identical here to one returning a fresh reading (#267).
            "update_interval": str(coordinator.update_interval) if coordinator.update_interval else None,
            "last_value_change_age_s": last_value_change_age_s,
            "unchanged_updates": coordinator.unchanged_updates,
            # A get-all reply repeats the scheduled-task list under one
            # sequence number, and the copies can disagree: in the capture
            # this was read from, the later copy was 96 s behind the first.
            # The first copy is taken as current, and this counts how often
            # that decision was load-bearing. Reported at zero as well - a
            # zero on a device that has a schedule is the evidence that the
            # copies agree there, which is the answer the rule was missing.
            "schedule_divergent_bundles": coordinator.schedule_divergent_bundles,
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
        # The PowerOcean quota addresses battery packs by serial in the key
        # itself, and a subsystem revision could arrive under such a key -
        # covered, like every quota-derived section, by the single redaction
        # pass on the way out.
        "firmware": coordinator.firmware,
        # Always present, even with no metrics at all. An absent section
        # cannot be told apart from a version that never had one, and "this
        # device integrates nothing" is itself the answer to a question about
        # a kWh sensor that will not move.
        "energy_integrator": _energy_integrator_diagnostics(coordinator, now),
        # Always present too (ADR-013): null on every device that never
        # runs the PowerOcean surplus auto-sync, and equally null once a
        # divergent pair it tracked has no record - see the property's own
        # docstring for why those two cases share one representation.
        "surplus_auto_sync": coordinator.surplus_auto_sync_diagnostics,
        "event_log": _format_event_log(coordinator.event_log),
    }

    # STREAM units linked on one account report every figure for the system
    # and the per-unit readings underneath, each stamped with a serial. Two
    # different situations leave the per-unit sensor empty - the device sends
    # no such block, or it sends one that never names this unit - and they
    # need opposite fixes. The counters name which one it is. No serial is
    # carried here, only how many the block held.
    linked_units = coordinator.linked_unit_stats
    if linked_units is not None:
        diag["linked_units"] = linked_units

    # Enhanced Mode: the raw push frames the device sent, with the serial
    # masked and each frame truncated. Device variants within a serial family
    # do not always share a field layout, so a mis-decoded variant can only be
    # diagnosed against the bytes it actually sends.
    # One read for both halves: the counts only reconcile against the frame
    # list if they describe the same state of the buffer.
    raw_frames, raw_sampling = coordinator.raw_frame_capture()
    # Whether the vendor app's own writes were being watched while this ran.
    # A capture carrying no write frame says nothing on its own: the app may
    # have sent none, or this version may never have subscribed to the topic
    # they arrive on. Both look like an empty result, and telling them apart
    # from the outside took a round trip with a reporter before this was
    # written down.
    #
    # Reported even when nothing was captured at all, which is the case where
    # the question is sharpest. Putting it inside the frame list would answer
    # it only for the recordings that already have an answer in them.
    app_writes_watched = coordinator.app_writes_watched
    if app_writes_watched:
        diag["app_writes_watched"] = True
    if raw_frames:
        diag["raw_frames"] = {
            "count": len(raw_frames),
            "app_writes_watched": app_writes_watched,
            "truncated_at_bytes": _FRAME_BUDGETS,
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
    # their own.
    if coordinator.device_type in (
        DEVICE_TYPE_DELTA3,
        DEVICE_TYPE_STREAM,
        DEVICE_TYPE_POWEROCEAN,
        DEVICE_TYPE_POWERSTREAM,
    ):
        raw_quota = coordinator.raw_quota
        raw_age_s: float | None = None
        if coordinator.raw_quota_captured_at > 0:
            raw_age_s = round(now - coordinator.raw_quota_captured_at, 1)
        diag["raw_quota"] = {
            "captured": bool(raw_quota),
            "age_s": raw_age_s,
            "key_count": len(raw_quota),
            # Deliberately not redacted here: a per-key call hands every
            # serial a fresh alias map, so two battery pack keys collapsed
            # onto the same placeholder and the comprehension silently kept
            # only the last pack. The pass on the way out threads one alias
            # map through the whole dump instead.
            "values": dict(sorted(raw_quota.items())),
        }

    # Enhanced Mode: the field numbers the device sent that our protobuf
    # binding does not declare. A field the binding does not know is dropped
    # silently before it reaches any sensor, so without this there is no way
    # to tell a device that does not report a value from one that reports it
    # into a gap in our schema - and that is the question every "can this be
    # controlled from Home Assistant" request runs into first.
    #
    # Raw frames answer the same question in principle, but a frame can
    # outgrow its byte budget, and reading field numbers out of a hex dump
    # is not something to ask of a reporter.
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
