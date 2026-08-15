"""PowerPulse wallbox telemetry on the PowerOcean's 209 command family (#245).

Both payloads below are real frames from a reporter's charging session on
2026-08-12, taken straight out of his diagnostics download. The serial was
already masked in the export, so nothing here identifies his hardware.

The session is what makes these two frames worth keeping: the first still
describes the *previous* charge, the second sits in the middle of the live one.
A parser that quietly reported lifetime totals would pass a synthetic test and
fail on exactly this pair.
"""

from __future__ import annotations

import pytest

from custom_components.ecoflow_energy.ecoflow.parsers.powerocean_proto import (
    remap_ev_charging_keys,
)
from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
    decode_proto_runtime_headers,
)

# 17:37:58 UTC - no session running, the frame still carries the charge that
# ended on 11.08. The vehicle id reads "-1", the placeholder for "no car".
FRAME_IDLE = bytes.fromhex(
    "0a1058585858585858585858585858585858150000000018ff012001280030023a0800000000"
    "0000000045000000004d00b29846500258a8940160036a022d3172022d317d59e27a6a850181"
    "2c7b6a880100900102f00100f801a001800200880200900203980202"
)

# 17:50:24 UTC - charging three-phase, twelve minutes into the session. This
# one is the complete MQTT frame including its envelope, so it exercises the
# header decode and the command routing as well as the field mapping.
FRAME_CHARGING = bytes.fromhex(
    "0a8d010a600a10585858585858585858585858585858582001280130033a0800000000000000"
    "0045009075454d0080e043500258d00560026a0336353372033635337d82af7c6a850152b27c"
    "6a880101900105f00100f8013c800200880200900203980202106018202001 40d10148085060"
    "580178d101800103880101c20110585858585858585858585858585858580a00".replace(" ", "")
)


def _from_frame(frame: bytes) -> dict:
    """Run a complete MQTT frame through the real decode path."""
    results = decode_proto_runtime_headers(frame)
    mapped = [r.mapped for r in results if r.mapped.get("_is_ev_charging_param")]
    assert mapped, "the 209/8 header was not routed"
    raw = {k: v for k, v in mapped[0].items() if not k.startswith("_")}
    return remap_ev_charging_keys(raw)


def _from_payload(payload: bytes) -> dict:
    """Same mapping for a bare 209/8 payload lifted out of a bundle."""
    from google.protobuf.json_format import MessageToDict

    from custom_components.ecoflow_energy.ecoflow.proto import ecocharge_pb2 as pb2

    msg = pb2.EVChargingParamReport()
    msg.ParseFromString(payload)
    return remap_ev_charging_keys(
        MessageToDict(msg, preserving_proto_field_name=True)
    )


class TestChargingFrame:
    def test_power_energy_and_duration(self) -> None:
        result = _from_frame(FRAME_CHARGING)

        assert result["ev_charge_power_w"] == pytest.approx(3929.0, abs=1.0)
        assert result["ev_session_energy_wh"] == pytest.approx(449.0, abs=1.0)
        assert result["ev_session_duration_s"] == 720.0

    def test_energy_over_duration_matches_the_reported_power(self) -> None:
        """The check that decided the unit of every one of these three.

        Between the two frames the previous session's numbers are useless, so
        this uses the live one against itself: 449 Wh in 720 s is an average
        power in the same band as the 3929 W being reported, and would be off
        by a factor of 1000 if the energy were kWh or the duration minutes.
        """
        result = _from_frame(FRAME_CHARGING)

        average_w = result["ev_session_energy_wh"] * 3600 / result["ev_session_duration_s"]

        # The average sits below the instantaneous reading because the session
        # started single-phase at about 1200 W and stepped up later.
        assert 1200 < average_w < result["ev_charge_power_w"]

    def test_status_and_vehicle(self) -> None:
        result = _from_frame(FRAME_CHARGING)

        assert result["ev_charge_status"] == "charging"
        assert result["ev_vehicle_id"] == "653"


class TestIdleFrame:
    def test_previous_session_is_reported_not_a_lifetime_total(self) -> None:
        """19545 Wh over 18984 s is the 11.08. charge, not a meter reading.

        If a later change ever points these keys at a total_increasing sensor,
        this is the frame that shows why that is wrong: the very next session
        resets all of it to zero.
        """
        result = _from_payload(FRAME_IDLE)

        assert result["ev_session_energy_wh"] == pytest.approx(19545.0, abs=1.0)
        assert result["ev_session_duration_s"] == 18984.0
        assert result["ev_charge_power_w"] == 0.0

    def test_no_vehicle_is_reported_as_absent(self) -> None:
        """The charger sends "-1" rather than omitting the field. Passing that
        through would put a literal -1 in front of the user."""
        result = _from_payload(FRAME_IDLE)

        assert result["ev_vehicle_id"] is None

    def test_status_is_preparing(self) -> None:
        result = _from_payload(FRAME_IDLE)

        assert result["ev_charge_status"] == "preparing"


class TestRegistry:
    def test_the_command_tuple_is_routed(self) -> None:
        """Without the registry entry the frame decodes to nothing at all,
        which is what the integration did with it before PLAN-079."""
        from custom_components.ecoflow_energy.ecoflow.proto.runtime import (
            _build_cmd_registry,
        )

        registry = _build_cmd_registry()

        assert (209, 8) in registry
        assert registry[(209, 8)].flags == {"_is_ev_charging_param": True}

    def test_unknown_status_value_is_dropped_not_passed_through(self) -> None:
        """An enum sensor raises on any value outside its options list, so a
        firmware adding a state must cost the reading, not the entity."""
        result = remap_ev_charging_keys({"charging_status": "EV_CHG_STS_FUTURE"})

        assert "ev_charge_status" not in result


def test_a_bundled_frame_still_reaches_the_mapping() -> None:
    """The idle payload arrived inside a get-reply bundle rather than on its
    own, which is the path the coupled and uncoupled captures both use."""
    assert _from_payload(FRAME_IDLE)
