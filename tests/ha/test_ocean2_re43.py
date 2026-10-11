"""RE43: the 12 kW Ocean 2 Plus reads on the RE11 path with its AC power sign corrected.

The two payloads below are real: the telemetry (`254/39`) and one module
(`254/46`) of a single get_reply bundle from an owner's diagnostics download
on #145, taken while the unit supplied 7,800 W to the house from its battery.
Serial numbers and the time zone were masked in the download (protobuf carries
a length before every field, so the mask keeps the length), and no serial
survives in either payload.

`4.1` (total AC power) and `4.3.1[a].3` (phase A active power) read negative on
this unit in that state, where an RE11 and an RE41 read positive. Battery,
grid, solar, home load and apparent power carry the same sign on all of them.
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

RE43_SERIAL = "RE43TEST00000001"

TELEMETRY = bytes.fromhex(
    "1a250800100118072000283830043800400148005000581e600168107001780080018104"
    "88011e22bf020ddb60e5c515a3c847421a6d0a140d000000001d0000000025000000002d"
    "000000000a1b0d0e11714315cd7bf2411d9201e5c525dbbd98432d7c34e54530010a1b0d"
    "0000000015000000001d0000000025000000002d0000000030020a1b0d00000000150000"
    "00001d0000000025000000002d00000000300322590a0f0d000000001d00000000250000"
    "00000a160d97297143159b0e18401da3c847422500923e4128010a160d00000000150000"
    "00001d00000000250000000028020a160d0000000015000000001d000000002500000000"
    "28032d54a844443516d0893a3d6ad1673d4550032ebc48015000580060006d000040c072"
    "400a0515000000000a11080115bd44ba411d0000000025000000000a11080215f0bbcf41"
    "1d0000000025000000000a11080315eb8dfe411d0000000025000000007d000000008001"
    "0130073a200d0070f34515000000001d00000000250070f3c5283835000000003d000000"
    "004000488480800850365a0210017090037801820104080010009201720a1a0d00004845"
    "1500704745180120042d00008841350000704138010a1a0d005047451500f04645180120"
    "042d00007041350000604138040a1a0d00104a451500904945180120042d000080413500"
    "00704138030a1a0d00b04f451500a04f45180120042d0000884135000070413802100198"
    "0100aa0106080010001800fa020f0d9c0d052015980d05201d00000000b00301b80301c2"
    "0306080110011800d003f01582040a0d89701b4615523261468a04440800100018002500"
    "0000002d0050e445350030e5c53d00803b464001480155000000005d0000000060006800"
    "700078a4588001368801389001a458980101a50100d8f3459a04200803150000e1431d00"
    "00614425000096432d0000164435000096433d00001644a00400aa0406080010001800b8"
    "0400d00400e80400f204021001fa0404120210019205021001a00500b00501ba05200d00"
    "c0f34515000000001d000000002500c0f3c5283835000000003d00000000ca0504120210"
    "01da054c0a0808011001180020010a0808021000180020010a0808031000180020010a08"
    "08041000180020010a0808051000180020010a04080610000a0808071000180020010a08"
    "0808100018002001e00550fa05180a140800121058585858585858585858585858585858"
    "10018a06021001980600a00600a80600b00600ba06021000c00600f806018007c8018a07"
    "0d4575726f70652f5858585858589007009a07190a0a08011090a9f5d60618000a0b0802"
    "1090b7a1dd0618c801a00764aa07021001b2073e0d00401c461500d084461d72f97f3f25"
    "a4707d3f2d00009d4635000060423d0000c84245000058424d00803b465500803b465d80"
    "b6a1476580b6a1476801"
)

MODULE = bytes.fromhex(
    "081e2ae6020d00000000103e18642a140000884100008041000080410000704100007041"
    "3500604e453d00404e4540014d8195834155e0568cbe5d786a4644650000000068007214"
    "00604e4500404e4500504e4500504e4500504e4578018201105858585858585858585858"
    "58585858588801049001009d0100800945a50100400345ad0100008041b5010000b841bd"
    "010000a041c50100009841cd010000e041d001b6e65dd80182d057e001909513e801b094"
    "13f50100008841fd010000704185020000b0418d020000a8419002009802ffff03a00221"
    "a802c8ecaad606b5021d1f7b42bd020000c842c00200c80205d00264d80201e00200e802"
    "83d001f00201f80293888408800385808408880302900301980301a0039701a8038102b5"
    "0357014345bd033d197242c50300000000cd031d1f7b42d5031d1f7b42dd036edbb640e5"
    "030cd3c742ed0366f6c742f00300f803008004008804beca01900480b601a00402ad0400"
    "504e45a201db020a04e864714312040ce5a23f1a0485e33c432204b84472432a04d59999"
    "433204043871433a04b151833f4204796478424a04568e6f435204477977435d00004842"
    "620483f518406a04dc2d484272040068d9bf7df81e484485010b2d8d3a8d0171cc613d95"
    "014a926b3d9d01f444fb41a5013a5ada41ad0146872d40b00100b80100c00100ca010300"
    "0000d2010100d80120e80100f00100f8010b8002f883828002880280019002009802c401"
    "a00240a80201b50280b7a147bd0200000000c50200803b46cd0200000000d002858440dd"
    "02000030c1e50285e33c43ed02f5023143f5020068d9bffd027f188c43850300803b468d"
    "0300803b469503008040449d0300005744a50300004844aa030c477fba41359bc9417c91"
    "fe41b2030c7580843d7fe29cbd4019823dba030c875264bea62dddbcc75a85bec503d130"
    "c848c80300d2030400020202d80301e00306e80306f00306f80300800401"
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
        self._bp_sn_to_index: dict[str, int] = {}

    @property
    def device_tag(self) -> str:
        return device_log_tag(self.device_sn)


def _parsed(serial: str, topic: str) -> dict[str, Any]:
    parsed = _Ocean2Host(serial)._parse_message(topic, _bundle())
    assert parsed is not None
    return parsed


def test_re43_routes_to_the_ocean_2_read_path_as_an_ocean_2_plus() -> None:
    assert get_device_type("", RE43_SERIAL) == DEVICE_TYPE_OCEAN2
    assert get_device_name("", "RE43TEST00001234") == "Ocean 2 Plus (1234)"


def test_the_real_frame_reads_ac_power_positive_once_corrected() -> None:
    parsed = parse_ocean2_proto_message(
        _bundle(), invert_ac_sign=ocean2_ac_sign_inverted(RE43_SERIAL)
    )
    assert parsed is not None
    assert parsed["pcs_ac_power_w"] == pytest.approx(7340.11, abs=0.01)
    assert parsed["inv_phase_a_active_power_w"] == pytest.approx(7328.20, abs=0.01)
    # Not part of the correction: same sign on every unit measured.
    assert parsed["home_w"] == 7800.0
    assert parsed["batt_w"] == -7800.0
    assert parsed["grid_w"] == 0.0
    assert parsed["solar_w"] == 0.0
    assert parsed["soc_pct"] == 56.0
    assert parsed["inv_phase_a_apparent_power_va"] == pytest.approx(7334.56, abs=0.01)
    # An unused phase stays a plain zero, not -0.0.
    assert str(parsed["inv_phase_b_active_power_w"]) == "0.0"
    # The module of the bundle decodes on the same field numbers.
    assert parsed["module1_soc_pct"] == pytest.approx(62.78, abs=0.01)
    assert parsed["module1_soh_pct"] == 100.0


def test_without_the_flag_the_device_sign_is_kept() -> None:
    # Control for the test above: the flag is what changes the sign.
    parsed = parse_ocean2_proto_message(_bundle())
    assert parsed is not None
    assert parsed["pcs_ac_power_w"] == pytest.approx(-7340.11, abs=0.01)
    assert parsed["inv_phase_a_active_power_w"] == pytest.approx(-7328.20, abs=0.01)


def test_the_frequency_of_a_single_phase_frame_is_not_zero() -> None:
    parsed = parse_ocean2_proto_message(_bundle())
    assert parsed is not None
    assert parsed["pcs_ac_freq_hz"] == pytest.approx(49.95, abs=0.01)


@pytest.mark.parametrize(
    "topic",
    [
        f"/app/device/property/{RE43_SERIAL}",
        f"/app/user/{RE43_SERIAL}/thing/property/get_reply",
    ],
)
def test_ingest_corrects_the_re43_and_leaves_the_re41(topic: str) -> None:
    re43 = _parsed(RE43_SERIAL, topic)
    assert re43["pcs_ac_power_w"] == pytest.approx(7340.11, abs=0.01)
    assert re43["inv_phase_a_active_power_w"] == pytest.approx(7328.20, abs=0.01)

    re41 = _parsed("RE41TEST00000001", topic.replace(RE43_SERIAL, "RE41TEST00000001"))
    assert re41["pcs_ac_power_w"] == pytest.approx(-7340.11, abs=0.01)
