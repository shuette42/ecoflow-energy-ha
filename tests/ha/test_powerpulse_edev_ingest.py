"""The relay wallbox report reaches the coordinator's device data (#247).

`tests/test_powerpulse_edev_wallbox.py` proves the mapping; this proves the
routing inside the coordinator. Every PowerOcean frame, a single property push
as much as a get-all reply, goes through the header loop in
`_parse_powerocean_proto_frame`; the single-frame branches further down in
`_parse_message` are for other device types. A mutation that registers the
tuple but forgets the branch in that loop is invisible to the mapping tests
and caught here, on both topics.
"""

from __future__ import annotations

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
    DOMAIN,
    MODE_ENHANCED,
)
from custom_components.ecoflow_energy.coordinator import EcoFlowDeviceCoordinator
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

from tests.test_powerpulse_edev_wallbox import (
    FRAME_CHARGING_MID_ORDER,
    HEATING_ROD_REPORT,
)

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

WALLBOX = {
    "ev_charge_power_w": 1355.0,
    "ev_session_energy_wh": 364.0,
    "ev_session_duration_s": 1080.0,
    "ev_charge_status": "charging",
    "ev_vehicle_id": "5274",
}


def _coordinator(hass: HomeAssistant) -> EcoFlowDeviceCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_AUTH_METHOD: AUTH_METHOD_APP,
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [POWEROCEAN_DEVICE],
        },
        unique_id="test@example.com",
    )
    entry.add_to_hass(hass)
    return EcoFlowDeviceCoordinator(hass, entry, POWEROCEAN_DEVICE)


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


async def test_a_property_push_lands_in_device_data(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property",
        FRAME_CHARGING_MID_ORDER,
    )
    assert parsed is not None
    assert {k: parsed[k] for k in WALLBOX} == WALLBOX


async def test_a_get_all_reply_with_both_accessories_keeps_the_wallbox(
    hass: HomeAssistant,
) -> None:
    """The rod's report comes AFTER the wallbox's in the real bundle.

    The bundle path merges header by header; a rod report that mapped to a
    zeroed wallbox would overwrite the live session one header later.
    """
    coordinator = _coordinator(hass)
    bundle = _header(241, 3, _pile_pdata()) + _header(241, 3, HEATING_ROD_REPORT)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply",
        bundle,
    )
    assert parsed is not None
    assert {k: parsed[k] for k in WALLBOX} == WALLBOX


async def test_a_rod_only_push_does_not_touch_the_wallbox(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    parsed = coordinator._parse_message(
        f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property",
        _header(241, 3, HEATING_ROD_REPORT),
    )
    assert not parsed or not any(k.startswith("ev_") for k in parsed)
