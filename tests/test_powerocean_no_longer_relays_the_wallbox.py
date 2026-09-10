"""The PowerOcean no longer relays the PowerPulse 2 wallbox session (#247).

Until ADR-008's addendum (2026-09-09), a PowerPulse 2 charger showed up on
the PowerOcean's accessory relay as an `EDevParamReport` on `(241, 3)`, and
`remap_pile_charging_keys` in `powerocean_proto.py` turned that report into
the `ev_*` sensor keys - this file, then named
`test_powerpulse_edev_wallbox.py`, proved that mapping against real reporter
captures. The wallbox is now its own device type (`DEVICE_TYPE_POWERPULSE2`,
SN prefix `C376`) with its own parser (`ecoflow/parsers/powerpulse_proto.py`,
tested in `tests/test_powerpulse_proto.py`) on its own channel, and the relay
path was removed as the second half of that move: `(241, 3)` is gone from
`_build_powerocean_table` in `ecoflow/proto/runtime.py`, and
`remap_pile_charging_keys` no longer exists anywhere in the tree.

This file now proves the opposite of what it used to: routed through the
same real PowerOcean decode path, these frames yield no wallbox reading at
all - not a zeroed one, none. `tests/test_powerpulse_proto.py` covers the
mapping now; `tests/ha/test_powerpulse_edev_ingest.py` covers the
coordinator routing for both the silent PowerOcean and the new PowerPulse 2
channel.

Every wallbox frame below is a real push taken byte for byte out of a
reporter's diagnostics downloads (22.08., 24.08., 27.08. and 28.08.2026).
The serial is masked in the export; the vehicle id the charger reports is
four digits and stays. They are kept - unlike the mapping they used to prove
- because a decode path that goes silent for a frame it use to parse is
worth pinning down for more than one shape of session.

The heating rod shares the old `(241, 3)` tuple in one of these captures and
is unaffected by the whole move: it is read through its own `(212, 8)`
tuple and `remap_heating_rod_keys`, which this move never touched. Both the
old, now-dead sharing of `(241, 3)` and the real, current `(212, 8)` path are
checked below, so a removal that took more than it should have would show up
here.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.ecoflow.const import DEVICE_TYPE_POWEROCEAN
from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_heating_rod_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_fixed32,
    encode_field_varint,
)

# 24.08. 13:44:29 UTC - nothing charging. The report still carries the order
# that ended earlier (276 s, 0 Wh), status 1, and the vehicle it last charged.
FRAME_IDLE_PREVIOUS_ORDER = bytes.fromhex(
    "0ac9010a9a010a1708d7011210585858585858585858585858585858581801227f0a30081010"
    "0218a001203c2800303c3a080800100018002000420e0a0012001a002200280032003800aa01"
    "06010119190000180220012a100000000000000000000000000000000030003a100a04353237"
    "34120435323734180f200f421708d88cadd40610031ddc301a0025f0311a0028003094024a00"
    "4a004a005003600010601820200140f1014803509a01580178f101800103880101c201105858"
    "5858585858585858585858585858"
)

# 27.08. 17:44:47 UTC - charging at the 6 A setting, eighteen minutes in:
# 1355 W, 364 Wh, 1080 s, status 3, vehicle "5274".
FRAME_CHARGING_MID_ORDER = bytes.fromhex(
    "0ad7010aa8010a1708d7011210585858585858585858585858585858581801228c010a300812"
    "100218a001203c2800303c3a080800100018002000420e0a0012001a002200280032003800aa"
    "0106010119190000180220032a100000000000000000000000000000000030cb0a3a100a0435"
    "323734120435323734180f200f421808aee6c1d40610021da25d1f0025da611f0028ec0230b8"
    "084a0508801210394a03088c124a030884125003600010601820200140f101480350a8015801"
    "78f101800103880101c2011058585858585858585858585858585858"
)

# 27.08. 17:47:25 UTC - the reporter stopped and restarted: a new order id,
# energy and duration back at 0, power already at 1355 W again.
FRAME_NEW_ORDER_JUST_OPENED = bytes.fromhex(
    "0ad5010aa6010a1708d7011210585858585858585858585858585858581801228a010a300812"
    "100218a001203c2800303c3a080800100018002000420e0a0012001a002200280032003800aa"
    "0106010119190000180220032a100000000000000000000000000000000030cb0a3a100a0435"
    "323734120435323734180f200f421608f6efc1d40610021d6b621f00256b621f00280030004a"
    "0508fe1110394a03088b124a0308ff115003600010601820200140f101480350a601580178f1"
    "01800103880101c2011058585858585858585858585858585858"
)

# 28.08. 00:43:09 UTC, the next night: 1373 W, 2111 Wh after 5880 s.
FRAME_CHARGING_LATER_SESSION = bytes.fromhex(
    "0ad7010aa8010a1708d7011210585858585858585858585858585858581801228c010a300812"
    "100218a001203c2800303c3a080800100018002000420e0a0012001a002200280032003800aa"
    "0106010119190000180220032a100000000000000000000000000000000030dd0a3a100a0435"
    "323734120435323734180f200f421808f184c3d40610021de5ac1f0025ddc31f0028bf1030f8"
    "2d4a0508911210394a03089f124a03089e125003600010601820200140f101480350a8015801"
    "78f101800103880101c2011058585858585858585858585858585858"
)

# The heating rod's report on the old (241, 3) tuple, lifted verbatim out of
# the 22.08. get-all reply (`docs/captures/hj31-powerglow-20260822.json`): the
# rod's address and masked serial in field 1 - the serial wins the oneof, so
# the address is not in the decoded dict - and its two setpoints in field 3
# (1998 W drawn, 2500 W target). No wallbox part at all. This is an
# `EDevParamReport` payload, the retired tuple's message; it does not parse
# as `HRChargingParamReport`, the rod's own `(212, 8)` message tested below.
HEATING_ROD_REPORT = bytes.fromhex(
    "0a1708d60112105858585858585858585858585858585818011a100d00c0f9441500401c45180020002800"
)

# 22.08. 10:23:56 UTC - the reporter's car charging three-phase at 4011 W,
# 10548 Wh after 6420 s. The only real frame at three-phase scale.
FRAME_THREE_PHASE = bytes.fromhex(
    "0adb010aac010a1708d70112105858585858585858585858585858585818012290010a300812"
    "100218a001203c2802303c3a080800100018002000420e0a0012001a002200280032003800aa"
    "0106010119190000180220032a100000000000000000000000000000000030ab1f3a100a0435"
    "323734120435323734180f200f421808f7bea5d40610021d004a1800251463180028b4523094"
    "324a0508db1e103b4a0508dd11103b4a0508e511103b5003600010601820200140f101480350"
    "ac01580178f101800103880101c2011058585858585858585858585858585858"
)

# 22.08. 17:01:39 UTC - status 5, the car declining the charge; 0 W with the
# finished order still attached (1783 Wh, 11194 s). Two headers in the frame,
# a 240/2 in front of the wallbox report - the only real frame with a status
# other than 1 or 3.
FRAME_SUSPENDED_BY_VEHICLE = bytes.fromhex(
    "0a790a420a200a1708d7011210585858585858585858585858585858581801100118012a0103"
    "0a1e0a1708d401121058585858585858585858585858585858180110022a0101106018202001"
    "2801380340f00148025042580170b4b6b5347881a601800103880101c2011058585858585858"
    "5858585858585858580ac4010a95010a1708d701121058585858585858585858585858585858"
    "1801227a0a300802100218a001203c2800303c3a080800100018002000420e0a0012001a0022"
    "00280032003800aa0106010119190000180220052a1000000080000000000000000000000000"
    "30003a100a0435323734120435323734180f200f421808b7d4a6d40610031dc0941800257ac0"
    "180028f70d30ba575003600010601820200140f1014803509501580178f101800103880101c2"
    "011058585858585858585858585858585858"
)

# Every real capture of the retired (241, 3) wallbox report, across the
# session shapes this file used to check the field mapping for: idle
# with a stale order, mid-session, freshly reset, a later night, three-phase,
# and suspended by the vehicle.
_RELAY_FRAMES: tuple[bytes, ...] = (
    FRAME_IDLE_PREVIOUS_ORDER,
    FRAME_CHARGING_MID_ORDER,
    FRAME_NEW_ORDER_JUST_OPENED,
    FRAME_CHARGING_LATER_SESSION,
    FRAME_THREE_PHASE,
    FRAME_SUSPENDED_BY_VEHICLE,
)


def _header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _decoded_tuples(frame: bytes) -> set[tuple[int | None, int | None]]:
    """Every (cmd_func, cmd_id) tuple the real PowerOcean decode path
    recognized in this frame."""
    results = decode_proto_runtime_headers(frame, device_type=DEVICE_TYPE_POWEROCEAN)
    return {(h.get("cmd_func"), h.get("cmd_id")) for r in results for h in r.headers}


def _mapped_keys(frame: bytes) -> set[str]:
    """Every key the real PowerOcean decode path mapped from this frame."""
    results = decode_proto_runtime_headers(frame, device_type=DEVICE_TYPE_POWEROCEAN)
    return {key for r in results for key in r.mapped}


class TestPowerOceanNoLongerRelaysTheWallbox:
    @pytest.mark.parametrize("frame", _RELAY_FRAMES)
    def test_the_retired_tuple_is_not_recognized(self, frame: bytes) -> None:
        assert (241, 3) not in _decoded_tuples(frame)

    @pytest.mark.parametrize("frame", _RELAY_FRAMES)
    def test_no_wallbox_key_survives_the_powerocean_decode(self, frame: bytes) -> None:
        assert not any(key.startswith("ev_") for key in _mapped_keys(frame))

    def test_a_rod_report_riding_the_retired_tuple_is_also_unrecognized(self) -> None:
        """The rod used to share (241, 3) with the wallbox via a oneof; both
        sides of that oneof are gone with the tuple, not just the wallbox
        half."""
        frame = _header(241, 3, HEATING_ROD_REPORT)
        assert (241, 3) not in _decoded_tuples(frame)
        assert _mapped_keys(frame) == set()


class TestTheHeatingRodsOwnTupleIsUnaffected:
    def test_the_heating_rod_still_reads_through_212_8_unchanged(self) -> None:
        """`(212, 8)` was never part of the relay removal.

        Built rather than captured: no `HRChargingParamReport` frame is on
        hand in this file - `HEATING_ROD_REPORT` above is an
        `EDevParamReport` payload for the retired (241, 3) tuple, a
        different message entirely (it fails to parse as
        `HRChargingParamReport`). The field numbers come straight off
        `HRChargingParamReport`'s own descriptor, and the values match the
        pair `remap_heating_rod_keys`'s own docstring quotes as a reporter
        capture: 2159 W drawn against a 2160 W target at 69 of a requested
        80 degrees.
        """
        pdata = bytearray()
        pdata.extend(encode_field_varint(4, 2159))  # hr_heating_power
        pdata.extend(encode_field_varint(5, 2160))  # hr_target_power
        pdata.extend(encode_field_fixed32(6, 69.0))  # hr_temp
        pdata.extend(encode_field_fixed32(7, 80.0))  # hr_target_temp

        frame = _header(212, 8, bytes(pdata))
        (result,) = decode_proto_runtime_headers(
            frame, device_type=DEVICE_TYPE_POWEROCEAN
        )
        assert result.mapped.get("_is_heating_rod_param") is True
        raw = {
            key: value
            for key, value in result.mapped.items()
            if not key.startswith("_")
        }
        assert remap_heating_rod_keys(raw) == {
            "heating_rod_power_w": 2159.0,
            "heating_rod_target_power_w": 2160.0,
            "heating_rod_water_temp_c": 69.0,
            "heating_rod_target_temp_c": 80.0,
        }
