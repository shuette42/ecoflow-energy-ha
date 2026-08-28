"""PowerPulse 2 (C376) telemetry on the PowerOcean's accessory relay, 241/3 (#247).

Every frame below is a real push taken byte for byte out of a reporter's
diagnostics downloads (22.08., 24.08., 27.08. and 28.08.2026). The serial is
masked in the export; the vehicle id the charger reports is four digits and
stays.

The 209 family the earlier PowerPulse uses never appears on this hardware, so
the same five sensors were empty for months while the README claimed them.
The captures carry the session progression that makes the mapping checkable:
an idle report still holding the previous order, a live one twelve minutes
in, the first report after a new order opened with both counters at zero, and
a later report of the next night's session.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_pile_charging_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto import ecocharge_pb2 as pb2
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)
from custom_components.ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
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

# The heating rod's report on the same tuple, lifted verbatim out of the
# 22.08. get-all reply (`docs/captures/hj31-powerglow-20260822.json`): the
# rod's address and masked serial in field 1 - the serial wins the oneof, so
# the address is not in the decoded dict - and its two setpoints in field 3
# (1998 W drawn, 2500 W target). No wallbox part at all.
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


def _header(cmd_func: int, cmd_id: int, pdata: bytes) -> bytes:
    header = bytearray()
    header.extend(encode_field_bytes(1, pdata))
    header.extend(encode_field_varint(8, cmd_func))
    header.extend(encode_field_varint(9, cmd_id))
    return encode_field_bytes(1, bytes(header))


def _from_frame(frame: bytes) -> dict:
    """Run a complete MQTT frame through the real decode path."""
    results = decode_proto_runtime_headers(frame)
    mapped = [r.mapped for r in results if r.mapped.get("_is_pile_charging_param")]
    assert mapped, "the 241/3 header was not routed"
    raw = {k: v for k, v in mapped[0].items() if not k.startswith("_")}
    return remap_pile_charging_keys(raw)


def _from_payload(payload: bytes) -> dict:
    from google.protobuf.json_format import MessageToDict

    msg = pb2.EDevParamReport()
    msg.ParseFromString(payload)
    return remap_pile_charging_keys(
        MessageToDict(msg, preserving_proto_field_name=True)
    )


class TestSessionReadings:
    def test_charging_mid_order(self) -> None:
        parsed = _from_frame(FRAME_CHARGING_MID_ORDER)
        assert parsed["ev_charge_power_w"] == 1355.0
        assert parsed["ev_session_energy_wh"] == 364.0
        assert parsed["ev_session_duration_s"] == 1080.0
        assert parsed["ev_charge_status"] == "charging"
        assert parsed["ev_vehicle_id"] == "5274"

    def test_power_matches_the_six_amp_setting(self) -> None:
        """1355 W is 230 V times 6 A, the user_current_set the same frame carries.

        That is what pins charging_pwr as whole watts rather than a scaled
        value: no other scale puts the reading at the setting.
        """
        parsed = _from_frame(FRAME_CHARGING_MID_ORDER)
        assert parsed["ev_charge_power_w"] == pytest.approx(230 * 6, rel=0.03)

    def test_energy_over_duration_matches_the_power(self) -> None:
        parsed = _from_frame(FRAME_CHARGING_MID_ORDER)
        implied = parsed["ev_session_energy_wh"] / (parsed["ev_session_duration_s"] / 3600)
        # 364 Wh in 18 min is 1213 W, against 1355 W now; the session
        # started below the setting. Same order of magnitude, same unit.
        assert 1000 < implied < parsed["ev_charge_power_w"]

    def test_a_new_order_resets_energy_and_duration_together(self) -> None:
        parsed = _from_frame(FRAME_NEW_ORDER_JUST_OPENED)
        assert parsed["ev_session_energy_wh"] == 0.0
        assert parsed["ev_session_duration_s"] == 0.0
        assert parsed["ev_charge_power_w"] == 1355.0
        assert parsed["ev_charge_status"] == "charging"

    def test_later_session(self) -> None:
        parsed = _from_frame(FRAME_CHARGING_LATER_SESSION)
        assert parsed["ev_charge_power_w"] == 1373.0
        assert parsed["ev_session_energy_wh"] == 2111.0
        assert parsed["ev_session_duration_s"] == 5880.0

    def test_three_phase_charging(self) -> None:
        parsed = _from_frame(FRAME_THREE_PHASE)
        assert parsed["ev_charge_power_w"] == 4011.0
        assert parsed["ev_session_energy_wh"] == 10548.0
        assert parsed["ev_session_duration_s"] == 6420.0
        assert parsed["ev_charge_status"] == "charging"
        # 10548 Wh in 6420 s is 5914 W on average against 4011 W now: the
        # session ran higher earlier. Same unit either way.
        assert 3000 < parsed["ev_session_energy_wh"] / (parsed["ev_session_duration_s"] / 3600) < 7000

    def test_suspended_by_the_vehicle(self) -> None:
        parsed = _from_frame(FRAME_SUSPENDED_BY_VEHICLE)
        assert parsed["ev_charge_status"] == "suspended_vehicle"
        assert parsed["ev_charge_power_w"] == 0.0
        assert parsed["ev_session_energy_wh"] == 1783.0
        assert parsed["ev_session_duration_s"] == 11194.0

    def test_idle_report_keeps_the_previous_order(self) -> None:
        parsed = _from_frame(FRAME_IDLE_PREVIOUS_ORDER)
        assert parsed["ev_charge_power_w"] == 0.0
        assert parsed["ev_charge_status"] == "available"
        assert parsed["ev_session_duration_s"] == 276.0
        assert parsed["ev_session_energy_wh"] == 0.0
        # The charger keeps naming the vehicle it last charged while idle.
        assert parsed["ev_vehicle_id"] == "5274"


class TestOtherAccessoriesAndEdges:
    def test_a_heating_rod_report_on_the_same_tuple_maps_to_nothing(self) -> None:
        """Not 0 W - nothing. The rod's frame must leave the wallbox alone."""
        assert _from_payload(HEATING_ROD_REPORT) == {}

    def test_a_rod_only_frame_still_routes_but_yields_nothing(self) -> None:
        frame = _header(241, 3, HEATING_ROD_REPORT)
        results = decode_proto_runtime_headers(frame)
        mapped = [r.mapped for r in results if r.mapped.get("_is_pile_charging_param")]
        assert mapped, "the tuple is registered regardless of the accessory"
        raw = {k: v for k, v in mapped[0].items() if not k.startswith("_")}
        assert remap_pile_charging_keys(raw) == {}

    def test_a_rod_report_is_not_an_unknown_field_in_the_diagnostics(self) -> None:
        """Fields 2 and 3 are declared as opaque bytes for exactly this.

        Undeclared, every rod report would put "241/3 field 3" into the
        diagnostics of every system with a rod - noise that would bury a
        real unknown field the next time one appears.
        """
        (result,) = decode_proto_runtime_headers(_header(241, 3, HEATING_ROD_REPORT))
        assert "_unknown_fields" not in result.mapped
        assert "third_plug_param_report" in result.mapped

    def test_unknown_status_number_is_dropped(self) -> None:
        raw = {"pile_charging_param_report": {"charging_status": 7, "charging_pwr": 12}}
        parsed = remap_pile_charging_keys(raw)
        assert "ev_charge_status" not in parsed
        assert parsed["ev_charge_power_w"] == 12.0

    def test_every_documented_status_number_maps_to_the_209_name(self) -> None:
        names = {
            0: "none", 1: "available", 2: "preparing", 3: "charging",
            4: "suspended_charger", 5: "suspended_vehicle", 6: "finishing", 9: "faulted",
        }
        for number, name in names.items():
            raw = {"pile_charging_param_report": {"charging_status": number}}
            assert remap_pile_charging_keys(raw) == {"ev_charge_status": name}

    def test_placeholder_and_empty_vehicle_are_absent(self) -> None:
        for text in ("-1", ""):
            raw = {"pile_charging_param_report": {"vehicle_info": {"charge_vehicle_id": text}}}
            assert remap_pile_charging_keys(raw) == {"ev_vehicle_id": None}

    def test_nothing_is_invented_for_a_missing_order(self) -> None:
        raw = {"pile_charging_param_report": {"charging_pwr": 0}}
        assert remap_pile_charging_keys(raw) == {"ev_charge_power_w": 0.0}

    def test_the_command_tuple_is_routed_with_its_own_flag(self) -> None:
        results = decode_proto_runtime_headers(FRAME_CHARGING_MID_ORDER)
        flags = {k for r in results for k, v in r.mapped.items() if k.startswith("_is_") and v}
        assert "_is_pile_charging_param" in flags
        assert "_is_ev_charging_param" not in flags
