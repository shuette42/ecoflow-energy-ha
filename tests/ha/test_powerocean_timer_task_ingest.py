"""The schedule list reaches the coordinator, and the right copy of it does.

`tests/test_powerocean_timer_task.py` proves the mapping. This proves the two
things only the ingest loop can get wrong: which of the two copies a bundle
carries is taken as the read, and whether a schedule that disappears is
retracted rather than left standing.

The get-all reply carries the `96/10` header twice under one sequence number.
In nine of the ten bundles of the reporter capture on #328 the copies are
byte-identical. In the tenth, recorded at 19:11:37 UTC, they disagree: the
first has no armed flag and the second still has one, 96 s after the schedule
was disarmed from the app. The first copy is the current one, so the first
copy wins - and header order alone would have decided it the other way round.
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

from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    _header_carries_no_pdata,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)

from tests.test_powerocean_timer_task import (
    LIST_ARMED_1000W,
    LIST_ARMED_1500W,
    LIST_DISARMED_1500W,
    LIST_EMPTY,
    LIST_STALE_ARMED_1500W,
    _header,
)

POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HJ31TEST00000001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

GET_REPLY = f"/app/user123/{POWEROCEAN_DEVICE['sn']}/thing/property/get_reply"


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


def _bundle(*payloads: bytes) -> bytes:
    return b"".join(_header(96, 10, payload) for payload in payloads)


async def test_the_divergent_bundle_reads_as_disarmed(hass: HomeAssistant) -> None:
    """The 19:11:37 bundle, both copies, in the order the device sent them.

    Taking the last copy would report the schedule as still armed, which is
    what it was 96 s earlier. Remove the first-copy rule from the ingest loop
    and this assertion fails.
    """
    coordinator = _coordinator(hass)

    parsed = coordinator._parse_message(
        GET_REPLY, _bundle(LIST_DISARMED_1500W, LIST_STALE_ARMED_1500W)
    )

    assert parsed is not None
    assert parsed["schedule_1_enabled"] is False
    assert parsed["schedule_1_power_w"] == 1500
    assert coordinator._schedule_divergent_bundles == 1


async def test_two_identical_copies_are_not_counted_as_a_divergence(
    hass: HomeAssistant,
) -> None:
    """Nine of the capture's ten bundles are this case."""
    coordinator = _coordinator(hass)

    parsed = coordinator._parse_message(
        GET_REPLY, _bundle(LIST_ARMED_1500W, LIST_ARMED_1500W)
    )

    assert parsed is not None
    assert parsed["schedule_1_enabled"] is True
    assert coordinator._schedule_divergent_bundles == 0


async def test_a_deleted_schedule_is_retracted_on_the_next_bundle(
    hass: HomeAssistant,
) -> None:
    """The delete at 19:12:36 is answered by an empty list at 19:12:42.

    An empty payload is the message rather than an absent one, which is why
    this command decodes an empty payload at all.
    """
    coordinator = _coordinator(hass)

    first = coordinator._parse_message(
        GET_REPLY, _bundle(LIST_ARMED_1500W, LIST_ARMED_1500W)
    )
    assert first is not None and first["schedule_1_enabled"] is True

    second = coordinator._parse_message(GET_REPLY, _bundle(LIST_EMPTY, LIST_EMPTY))

    assert second is not None
    assert second["schedule_1_enabled"] is None
    assert second["schedule_1_power_w"] is None
    assert second["schedule_1_window"] is None


async def test_a_powerocean_without_a_schedule_publishes_no_schedule_keys(
    hass: HomeAssistant,
) -> None:
    """The majority case: the first four bundles of the capture."""
    coordinator = _coordinator(hass)

    parsed = coordinator._parse_message(GET_REPLY, _bundle(LIST_EMPTY, LIST_EMPTY))

    assert not parsed or not any(k.startswith("schedule_") for k in parsed)


def _masked_header_without_seq(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    """A header that says its payload is masked, but carries no sequence.

    `Enc_type=1` means the payload is XORed with the low byte of the sequence
    number, and a sequence of zero is not serialised at all. The payload is
    right there and fully readable; what is missing is the key that says how
    to read it. This is the reachable shape of "present but unreadable" - the
    others cannot come off the wire, because the frame decoder renders every
    payload as hex before anything here sees it.
    """
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(6, 1))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _header_without_pdata(cmd_func: int, cmd_id: int) -> bytes:
    """A header with no payload field at all, as the device sends an empty list."""
    header = bytearray()
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


async def test_three_copies_that_disagree_count_as_one_bundle(
    hass: HomeAssistant,
) -> None:
    """Three copies, two disagreements, one decision.

    The count exists to answer how often taking the first copy mattered, and
    a bundle makes that choice once however many later copies it carries.
    Counting per copy would report this bundle twice and quietly inflate the
    evidence the rule is meant to be re-judged on.
    """
    coordinator = _coordinator(hass)

    parsed = coordinator._parse_message(
        GET_REPLY,
        _bundle(LIST_DISARMED_1500W, LIST_STALE_ARMED_1500W, LIST_ARMED_1000W),
    )

    assert parsed is not None
    assert parsed["schedule_1_enabled"] is False
    assert parsed["schedule_1_power_w"] == 1500
    assert coordinator._schedule_divergent_bundles == 1


async def test_an_unreadable_payload_does_not_retract_the_schedule(
    hass: HomeAssistant,
) -> None:
    """A payload this decoder cannot read is not the device reporting nothing.

    This command treats an empty payload as the message, because that is the
    one frame saying a schedule was deleted. A header whose payload is present
    but unreadable looks the same from the outside and means the opposite: fed
    through the empty-payload path it would publish an empty list the device
    never sent, and every key of a schedule the device still holds would be
    retracted on the strength of one frame nobody could read.
    """
    coordinator = _coordinator(hass)
    first = coordinator._parse_message(
        GET_REPLY, _bundle(LIST_ARMED_1500W, LIST_ARMED_1500W)
    )
    assert first is not None and first["schedule_1_enabled"] is True
    assert coordinator._schedule_indices == {1}

    parsed = coordinator._parse_message(
        GET_REPLY, _masked_header_without_seq(96, 10, LIST_ARMED_1500W)
    )

    assert parsed is None
    assert coordinator._schedule_indices == {1}


async def test_a_header_with_no_payload_field_does_retract_the_schedule(
    hass: HomeAssistant,
) -> None:
    """The other half of the same distinction, so neither can drift alone.

    Absence has to keep retracting: a guard drawn so tightly that no frame
    counts as empty would leave a deleted schedule standing forever, which is
    the failure the empty-payload path was added to prevent.
    """
    coordinator = _coordinator(hass)
    first = coordinator._parse_message(
        GET_REPLY, _bundle(LIST_ARMED_1500W, LIST_ARMED_1500W)
    )
    assert first is not None and first["schedule_1_enabled"] is True

    parsed = coordinator._parse_message(GET_REPLY, _header_without_pdata(96, 10))

    assert parsed is not None
    assert parsed["schedule_1_enabled"] is None
    assert parsed["schedule_1_window"] is None
    assert coordinator._schedule_indices == set()


def test_only_a_missing_or_empty_payload_counts_as_carrying_nothing() -> None:
    """The classifier behind both tests above, at the level it decides on.

    Corrupt hex and a payload of the wrong type cannot arrive from a real
    frame - the frame decoder hands every payload on as hex - so they are
    pinned here rather than through a bundle. They still have to classify as
    "carries something", because the moment any of them reads as absence, one
    bad frame retracts a schedule the device holds.
    """
    assert _header_carries_no_pdata({}) is True
    assert _header_carries_no_pdata({"pdata": None}) is True
    assert _header_carries_no_pdata({"pdata": ""}) is True

    assert _header_carries_no_pdata({"pdata": "zz"}) is False
    assert _header_carries_no_pdata({"pdata": "0a1"}) is False
    assert _header_carries_no_pdata({"pdata": b"\x0a"}) is False
    assert _header_carries_no_pdata({"pdata": 0}) is False
    assert _header_carries_no_pdata({"pdata": "0a19"}) is False
