"""The wallbox session's routing inside the coordinator, after the move off
the PowerOcean relay (#247, PLAN-132).

`tests/test_powerocean_no_longer_relays_the_wallbox.py` proves the retired
`(241, 3)` tuple is gone from the real PowerOcean decode path; this proves
the two coordinator-level consequences of that:

1. A PowerOcean coordinator stays silent. The same frames that used to land
   `ev_*` keys in `_device_data` through `_parse_powerocean_proto_frame`
   produce none now, on both the plain `property` push and the bundled
   `get_reply`, whether the bundle carries the wallbox report alone or next
   to the heating rod's.
2. The PowerPulse 2's own channel carries. A coordinator for a device with
   `device_type` `DEVICE_TYPE_POWERPULSE2` reaches `parse_powerpulse_message`
   through `_parse_message` and lands the wallbox keys, on both topics, from
   the real capture in `tests/fixtures/powerpulse/c376_frames_plan132.json`
   (`tests/test_powerpulse_proto.py` tests that parser directly; this only
   proves the coordinator actually calls it for this device type).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ecoflow_energy.const import (
    AUTH_METHOD_APP,
    CONF_AUTH_METHOD,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_USER_ID,
    DEVICE_TYPE_POWEROCEAN,
    DEVICE_TYPE_POWERPULSE2,
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)
from tests.test_powerocean_no_longer_relays_the_wallbox import (
    FRAME_CHARGING_MID_ORDER,
    HEATING_ROD_REPORT,
)

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "powerpulse"
    / "c376_frames_plan132.json"
)

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

POWERPULSE2_DEVICE: dict[str, Any] = {
    "sn": "C376ZE1TEST00001",
    "name": "PowerPulse 2",
    "product_name": "PowerPulse 2",
    "device_type": DEVICE_TYPE_POWERPULSE2,
    "online": 1,
}


def _coordinator(
    hass: HomeAssistant, device: dict[str, Any]
) -> EcoFlowDeviceCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [device],
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, device)


def _header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _pile_pdata() -> bytes:
    """The wallbox report's own payload, lifted out of the real frame."""
    from custom_components.ecoflow_energy.ecoflow.proto.decoder import (
        decode_header_message,
    )

    headers, _ = decode_header_message(FRAME_CHARGING_MID_ORDER)
    (header,) = [h for h in headers if h.get("cmd_func") == 241]
    return bytes.fromhex(header["pdata"])


def _powerpulse_frame(ts_iso: str) -> dict[str, Any]:
    """The captured PowerPulse 2 frame at this instant.

    Addressed by timestamp, never by position - same reasoning as
    `tests/test_powerpulse_proto.py`'s `_parse`: a position is not stable
    across a fixture rebuild.
    """
    frames = json.loads(FIXTURE.read_text())["frames"]
    for frame in frames:
        if frame["ts_iso"].startswith(ts_iso):
            return frame
    raise AssertionError(f"no frame captured at {ts_iso}")


def _powerpulse_topic(sn: str, frame_topic: str) -> str:
    if frame_topic == "get_reply":
        return f"/app/user123/{sn}/thing/property/get_reply"
    return f"/app/user123/{sn}/thing/property"


async def test_a_property_push_no_longer_yields_wallbox_keys(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property",
        FRAME_CHARGING_MID_ORDER,
    )
    assert not parsed or not any(key.startswith("ev_") for key in parsed)


async def test_a_get_all_reply_no_longer_yields_wallbox_keys(
    hass: HomeAssistant,
) -> None:
    """The rod's report comes AFTER the wallbox's in the real bundle.

    Both headers ride the now-retired (241, 3) tuple; neither is recognized
    any more, so neither can leave a stale or a fresh wallbox key behind.
    """
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    bundle = _header(241, 3, _pile_pdata()) + _header(241, 3, HEATING_ROD_REPORT)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply",
        bundle,
    )
    assert not parsed or not any(key.startswith("ev_") for key in parsed)


async def test_a_rod_only_push_does_not_touch_the_wallbox(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass, POWEROCEAN_DEVICE)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property",
        _header(241, 3, HEATING_ROD_REPORT),
    )
    assert not parsed or not any(key.startswith("ev_") for key in parsed)


async def test_powerpulse2_property_push_lands_in_device_data(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWERPULSE2_DEVICE)
    frame = _powerpulse_frame("2026-09-07T13:11:27")
    topic = _powerpulse_topic(POWERPULSE2_DEVICE["sn"], frame["topic"])
    parsed = coordinator._parse_message(topic, bytes.fromhex(frame["hex"]))
    assert parsed is not None
    assert parsed["ev_charge_status"] == "charging"
    assert parsed["ev_charge_power_w"] == 6599.2
    assert parsed["ev_max_current_a"] == 10.0
    assert parsed["ev_phase_mode"] == "three_phase"
    assert parsed["ev_total_energy_wh"] == 102266


async def test_powerpulse2_get_reply_bundle_lands_in_device_data(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass, POWERPULSE2_DEVICE)
    frame = _powerpulse_frame("2026-09-07T12:23:58")
    topic = _powerpulse_topic(POWERPULSE2_DEVICE["sn"], frame["topic"])
    parsed = coordinator._parse_message(topic, bytes.fromhex(frame["hex"]))
    assert parsed is not None
    assert parsed["ev_charge_status"] == "finishing"
    assert parsed["ev_charge_power_w"] == 0.0
    assert parsed["ev_cable_lock_enabled"] is True
    assert parsed["ev_total_energy_wh"] == 95537
