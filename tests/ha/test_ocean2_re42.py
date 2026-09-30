"""RE42: an Ocean 2 that reads on the RE11 path with its AC power sign corrected.

The two payloads below are real: the telemetry (`254/39`) and one module
(`254/46`) header of a single get_reply bundle from an owner's diagnostics
download on #145, taken while the unit supplied 4,570 W to the house from its
battery. Serial numbers were masked length-preservingly in the download
(protobuf carries a length before every field, so a shorter replacement would
make everything after it unreadable), and no serial survives in either one.

`4.1` (total AC power) and `4.3.1[a].3` (phase A active power) read negative on
this unit in that state, where an RE11 and an RE41 read positive. Battery,
grid, solar, home load and apparent power carry the same sign on all three.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ecoflow_energy.coordinator.mqtt_ingest import MqttIngestMixin
from custom_components.ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_OCEAN2,
    device_log_tag,
    get_device_name,
    get_device_type,
    ocean2_ac_sign_inverted,
)
from custom_components.ecoflow_energy.ecoflow.parsers.ocean2_proto import (
    parse_ocean2_proto_message,
)

RE42_SERIAL = "RE42TEST00000001"

TELEMETRY = bytes.fromhex(
    "1a2508001001180720002851300338004001480050005821600168107001780080018104"
    "88011e22bf020d1f5985c5155e9a47421a6d0a140d000000001d0000000025000000002d"
    "000000000a1b0d13c4714315fc088f411d604386c525f99584432dc784864530010a1b0d"
    "0000000015000000001d0000000025000000002d0000000030020a1b0d00000000150000"
    "00001d0000000025000000002d00000000300322590a0f0d000000001d00000000250000"
    "00000a160d24af7143153a771a401d5e9a4742250041eac128010a160d00000000150000"
    "00001d00000000250000000028020a160d0000000015000000001d000000002500000000"
    "28032d7ef34544353ab71d3b3d235fb13d45b49c00bc48015000580060006d0000004072"
    "400a0515000000000a11080115f7e107421d0000000025000000000a11080215d883e541"
    "1d0000000025000000000a11080315a48bfb411d0000000025000000007d000000008001"
    "0030a7013a200d00d08e4515000000001d000000002500d08ec5285135000000003d0000"
    "000040004884808008500a5a021001703c7800820104080010009201560a1a0d00404f45"
    "1500404f451801200b2d0000a841350000984138010a1a0d00804a451500304a45180120"
    "0b2d0000b041350000a04138020a1a0d00b04a451500704a451801200b2d0000a0413500"
    "00984138031001980100aa0106080010001800fa020f0d9c0d052015980d05201d000000"
    "00b00300b80300c20306080110011800d003cc3a82040a0dd408ea4515621529468a0444"
    "08001000180025000000002d00c885453500f886c53d00401c464001480155000000005d"
    "0000000060006800700078df5f80010a8801519001df5f980101a50100208f459a042008"
    "00150000e1431d0000614425000096432d0000164435000096433d00001644a00400aa04"
    "06080010001800b80400d00400e80400f204021001fa04041202100192051b0a170a1058"
    "58585858585858585858585858585815000040c01001a00500b00501ba05200d00d08e45"
    "15000000001d000000002500d08ec5285135000000003d00000000ca050412021001da05"
    "4c0a0808011001180020010a0808021000180020010a0808031000180020010a08080410"
    "00180020010a0808051000180020010a04080610000a0808071000180020010a08080810"
    "0018002001e00550fa05180a14080012105858585858585858585858585858585810018a"
    "06230a1f0a1708d70110021a105858585858585858585858585858585810011801200110"
    "01980600a00600a80600b00600ba06021000c00600f806018007c8018a070d4575726f70"
    "652f5858585858589007009a07190a0a08011090a9f5d60618000a0b08021090b7a1dd06"
    "18c801a00764aa07230a1f0a1908d70110e4ed0d1a105858585858585858585858585858"
    "5858100118001001b2073e0d0060ea4515003847461d72f97f3f25a4707d3f2d00806b46"
    "350000a2423d0000c84245000020414d00401c465500401c465d00cc41466500cc414668"
    "01"
)

MODULE = bytes.fromhex(
    "081e2ae8020dfd88fcc3105e18642a140000984100009841000098410000904100009041"
    "3500604e453d00404e4540014d2731834155aed6f3c15d23494744652335f5c168007214"
    "00604e4500504e4500404e4500504e4500404e4578038201105858585858585858585858"
    "585858585888010b9001009d0100401c45a50100800945ad010000a041b5010000b841bd"
    "010000b041c5010000a841cd010000f041d001ad9bde01d801aba6d201e001909513e801"
    "b09313f50100009841fd010000904185020000c8418d020000b0419002009802ffff03a0"
    "0221a802e4f7efd506b502240abe42bd020000c842c00200c80205d00264d80201e00200"
    "e80283d001f00201f80293888408800385808408880302900301980301a0039701a80381"
    "02b50357ee9445bd03f797bd42c50300000000cd03240abe42d503240abe42dd03922489"
    "40e503cca8c742ed039ae5c742f00300f80300800400880480e003900495b503a00402ad"
    "0400404e45a201db020a04db31704312043accfe3f1a044d52cd4322048bea74432a0415"
    "11ef4332046b1e70433a04e630923f42046bd104434a047eef6f435204361f89435d0000"
    "48426204ec2c18406a0492834842720400c812c17d861648448501df77623a8d01b4ff7b"
    "3d9501dc663fbf9d01990e0d42a5011bb7dc41ad01eb784e40b00100b80100c00100ca01"
    "03000000d2010100d80120e80100f00100f8010b8002f883828003880280019002009802"
    "c001a00240a80201b50200c44146bd0200000000c50200401c46cd0200000000d0028584"
    "40dd020000a841e5024d52cd43ed02e4e1ca43f50200c812c1fd029cbf2343850300e02b"
    "468d0300e02b469503008040449d0300005744a50300004844aa030c16dcbb4369ef2f43"
    "34960242b2030ce064393f1a571c3fb1c0db3dba030c3d68614375b4fe42df741cbfc503"
    "00000000c80300d2030400000002d80301e00305e80305f00306f80300800401"
)


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _msg(field: int, body: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(body)) + body


def _frame(pdata: bytes, cmd_id: int) -> bytes:
    """Wrap a payload in the EcoFlow outer frame, cmd_func 254."""
    header = (
        _msg(1, pdata)
        + _varint(8 << 3)
        + _varint(254)
        + _varint(9 << 3)
        + _varint(cmd_id)
    )
    return _msg(1, header)


def _bundle() -> bytes:
    return _frame(TELEMETRY, 39) + _frame(MODULE, 46)


class _Ocean2Host(MqttIngestMixin):
    """Minimal ingest host: the coordinator's serial and device type."""

    device_type = DEVICE_TYPE_OCEAN2

    def __init__(self, serial: str) -> None:
        self.device_sn = serial
        self.device_tag = device_log_tag(serial)
        self._bp_sn_to_index: dict[str, int] = {}


def _parsed(serial: str, topic: str) -> dict[str, Any]:
    parsed = _Ocean2Host(serial)._parse_message(topic, _bundle())
    assert parsed is not None
    return parsed


def test_re42_routes_to_the_ocean_2_read_path_under_a_plain_name() -> None:
    assert get_device_type("", RE42_SERIAL) == DEVICE_TYPE_OCEAN2
    assert get_device_name("", "RE42TEST00001234") == "Ocean 2 (1234)"


def test_the_real_frame_reads_ac_power_positive_once_corrected() -> None:
    parsed = parse_ocean2_proto_message(
        _bundle(), invert_ac_sign=ocean2_ac_sign_inverted(RE42_SERIAL)
    )
    assert parsed is not None
    assert parsed["pcs_ac_power_w"] == pytest.approx(4267.14, abs=0.01)
    assert parsed["inv_phase_a_active_power_w"] == pytest.approx(4296.42, abs=0.01)
    # Not part of the correction: same sign on every unit measured.
    assert parsed["home_w"] == 4570.0
    assert parsed["batt_w"] == -4570.0
    assert parsed["batt_discharge_power_w"] == 4570.0
    assert parsed["grid_w"] == 0.0
    assert parsed["solar_w"] == 0.0
    assert parsed["inv_phase_a_apparent_power_va"] == pytest.approx(4304.60, abs=0.01)
    # A module discharging keeps its own negative power.
    assert parsed["module3_power_w"] == pytest.approx(-505.07, abs=0.01)
    # An unused phase stays a plain zero, not -0.0.
    assert str(parsed["inv_phase_b_active_power_w"]) == "0.0"


def test_without_the_flag_the_device_sign_is_kept() -> None:
    # Control for the test above: the flag is what changes the sign.
    parsed = parse_ocean2_proto_message(_bundle())
    assert parsed is not None
    assert parsed["pcs_ac_power_w"] == pytest.approx(-4267.14, abs=0.01)
    assert parsed["inv_phase_a_active_power_w"] == pytest.approx(-4296.42, abs=0.01)


def test_only_the_re42_prefix_is_corrected() -> None:
    assert ocean2_ac_sign_inverted("RE42TEST00000001")
    assert ocean2_ac_sign_inverted("re42test00000001")
    for other in ("RE11TEST00000001", "RE17TEST00000001", "RE41TEST00000001", ""):
        assert not ocean2_ac_sign_inverted(other), other


def test_the_frequency_of_a_single_phase_frame_is_not_zero() -> None:
    # Record order on this unit: [no index: 0.0, A: ~49.9, B: 0.0, C: 0.0].
    parsed = parse_ocean2_proto_message(_bundle())
    assert parsed is not None
    assert parsed["pcs_ac_freq_hz"] == pytest.approx(49.90, abs=0.01)


@pytest.mark.parametrize(
    "topic",
    [
        f"/app/device/property/{RE42_SERIAL}",
        f"/app/user/{RE42_SERIAL}/thing/property/get_reply",
    ],
)
def test_ingest_corrects_the_re42_and_leaves_the_re41(topic: str) -> None:
    re42 = _parsed(RE42_SERIAL, topic)
    assert re42["pcs_ac_power_w"] == pytest.approx(4267.14, abs=0.01)
    assert re42["inv_phase_a_active_power_w"] == pytest.approx(4296.42, abs=0.01)

    re41 = _parsed("RE41TEST00000001", topic.replace(RE42_SERIAL, "RE41TEST00000001"))
    assert re41["pcs_ac_power_w"] == pytest.approx(-4267.14, abs=0.01)
