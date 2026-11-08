"""ES81 native readings from hand-built numeric packet fixtures."""

import json
import struct
from pathlib import Path

import pytest
from ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_SMART_METER,
    DEVICE_TYPE_SMART_METER_DUAL,
    DEVICE_TYPE_UNKNOWN,
    get_device_name,
    get_device_type,
)
from ecoflow_energy.ecoflow.parsers.smart_meter_dual_proto import (
    parse_smart_meter_dual_message,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
    encode_varint,
)

FIXTURE = Path(__file__).parent / "fixtures/smart_meter_dual/es81_power_frames.json"
FRAMES = json.loads(FIXTURE.read_text())["frames"]
KEYS = {"grid_w", "grid_l1_w", "grid_l2_w", "grid_l3_w"}
EXTENDED = json.loads(FIXTURE.with_name("es81_extended_frames.json").read_text())[
    "frames"
]
ALL_KEYS = (
    KEYS
    | {
        f"grid_l{phase}_{quantity}_{suffix}"
        for phase in (1, 2, 3)
        for quantity, suffix in (
            ("voltage", "v"),
            ("current", "a"),
            ("net_energy", "wh"),
        )
    }
    | {f"grid_{direction}_energy_wh" for direction in ("import", "export", "net")}
)


def _frame(data: bytes, *, extra: bytes = b"", command: int = 21) -> bytes:
    header = (
        encode_field_bytes(1, data)
        + encode_field_varint(8, 254)
        + encode_field_varint(9, command)
        + encode_field_varint(10, len(data))
        + extra
    )
    return encode_field_bytes(1, header)


def _power(number: int, value: float) -> bytes:
    return encode_varint(number * 8 + 5) + struct.pack("<f", value)


@pytest.mark.parametrize("number", [6, 8, 9, 10, 12, 15, 16, 17])
@pytest.mark.parametrize("wire", [1, 2, 5])
def test_header_metadata_requires_varints(number, wire):
    """Equal numeric floats are not valid protobuf command metadata."""
    data = _power(515, 100)
    values = {6: 0, 8: 254, 9: 21, 10: len(data), 12: 0, 15: 42241, 16: 3, 17: 1}
    header = encode_field_bytes(1, data)
    for field, value in values.items():
        if field != number:
            header += encode_field_varint(field, value)
        elif wire == 2:
            header += encode_field_bytes(field, encode_varint(value))
        else:
            header += encode_varint(field * 8 + wire) + struct.pack(
                "<d" if wire == 1 else "<f", value
            )
    assert parse_smart_meter_dual_message(encode_field_bytes(1, header)) is None


@pytest.mark.parametrize("row", FRAMES, ids=[r["at"] for r in FRAMES])
def test_every_packet_preserves_existing_four_power_values(row):
    parsed = parse_smart_meter_dual_message(bytes.fromhex(row["hex"]))
    assert {key: value for key, value in (parsed or {}).items() if key in KEYS} == row[
        "expected"
    ]
    assert parsed is None or set(parsed) <= ALL_KEYS


@pytest.mark.parametrize("row", EXTENDED, ids=[r["at"] for r in EXTENDED])
def test_extended_packets_keep_exact_native_values_and_presence(row):
    assert parse_smart_meter_dual_message(bytes.fromhex(row["hex"])) == row["expected"]


def test_exact_prefix_is_separate_from_bk21_and_unknown_es_variants():
    assert get_device_type("Dual", "ES81TEST00000001") == DEVICE_TYPE_SMART_METER_DUAL
    assert get_device_type("", "BK21TEST00000001") == DEVICE_TYPE_SMART_METER
    assert get_device_type("Dual", "ES82TEST00000001") == DEVICE_TYPE_UNKNOWN
    assert (
        get_device_type("Smart Meter Dual", "ES99TEST00000001") == DEVICE_TYPE_UNKNOWN
    )
    assert get_device_name("", "ES81TEST00000001") == "Smart Meter Dual (0001)"
    assert get_device_name("Dual", "ES81TEST00000001") == "Dual"


def test_missing_power_is_absent_and_explicit_zero_is_a_reading():
    assert parse_smart_meter_dual_message(_frame(_power(515, 0))) == {"grid_w": 0}
    assert parse_smart_meter_dual_message(_frame(_power(962, -100))) == {
        "grid_l1_w": -100
    }
    # Counters are native fields, never a derived net or a power reading.
    nested = encode_field_bytes(773, _power(4, 663) + _power(6, 466))
    assert parse_smart_meter_dual_message(_frame(nested)) == {
        "grid_import_energy_wh": 663,
        "grid_export_energy_wh": 466,
    }


@pytest.mark.parametrize(
    "extra",
    [
        encode_field_varint(6, 1),
        encode_field_varint(12, 1),
        encode_field_varint(15, 123),
        encode_field_varint(16, 99),
        encode_field_varint(17, 99),
    ],
)
def test_unobserved_encryption_ack_or_explicit_variant_metadata_refuses(extra):
    assert parse_smart_meter_dual_message(_frame(_power(515, 100), extra=extra)) is None


@pytest.mark.parametrize(
    "data",
    [
        b"\x00",
        b"\xff",
        _power(515, 1)[:-1],
        _power(515, 1) + _power(515, 2),
        _power(515, float("nan")),
        _power(515, float("inf")),
    ],
)
def test_invalid_or_nonfinite_power_is_not_published(data):
    assert parse_smart_meter_dual_message(_frame(data)) is None


def test_runtime_packet_does_not_become_a_power_or_counter_reading():
    assert parse_smart_meter_dual_message(_frame(_power(515, 100), command=22)) is None


def test_native_energy_missing_child_is_absent_without_bk21_export_fill():
    assert parse_smart_meter_dual_message(
        _frame(encode_field_bytes(773, _power(4, 12)))
    ) == {"grid_import_energy_wh": 12}
    assert parse_smart_meter_dual_message(_frame(encode_field_bytes(773, b""))) is None
    assert (
        parse_smart_meter_dual_message(_frame(encode_field_bytes(773, _power(5, 12))))
        is None
    )
    assert parse_smart_meter_dual_message(
        _frame(encode_field_bytes(773, _power(1, -3.5) + _power(6, 0)))
    ) == {"grid_l1_net_energy_wh": -3.5, "grid_export_energy_wh": 0}


@pytest.mark.parametrize("number", [956, 957, 771, 958, 959, 784])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_electrical_fields_never_become_readings(number, value):
    assert parse_smart_meter_dual_message(_frame(_power(number, value))) is None


@pytest.mark.parametrize("number", [1, 2, 3, 4, 6, 7])
def test_nonfinite_or_wrongwire_counter_is_absent_but_valid_counter_survives(number):
    for bad in (_power(number, float("nan")), encode_field_varint(number, 12)):
        other = 7 if number != 7 else 1
        parsed = parse_smart_meter_dual_message(
            _frame(encode_field_bytes(773, bad + _power(other, -2)))
        )
        assert parsed == {
            "grid_net_energy_wh" if other == 7 else "grid_l1_net_energy_wh": -2
        }


@pytest.mark.parametrize("number", [956, 957, 771, 958, 959, 784])
def test_wrongwire_electrical_field_is_not_coerced(number):
    assert (
        parse_smart_meter_dual_message(_frame(encode_field_varint(number, 240))) is None
    )
    assert (
        parse_smart_meter_dual_message(
            _frame(encode_varint(number * 8 + 1) + struct.pack("<d", 240))
        )
        is None
    )


@pytest.mark.parametrize(
    "data",
    [
        _power(958, 1) + _power(958, 2),
        encode_field_bytes(773, _power(1, 1) + _power(1, 2)),
        encode_field_bytes(773, _power(1, 1)) * 2,
        encode_field_bytes(773, _power(4, 1)[:-1]),
    ],
)
def test_ambiguous_or_truncated_mapped_energy_is_refused(data):
    assert parse_smart_meter_dual_message(_frame(data)) is None


@pytest.mark.parametrize(
    "payload",
    [
        _frame(_power(515, 1)) + b"\xff",
        _frame(_power(515, 1)) + b"\x0a\x08\xff",
        _frame(_power(515, 1), extra=encode_field_varint(9, 21)),
        _frame(
            _power(515, 1),
            extra=encode_field_varint(15, 42241) + encode_field_varint(15, 123),
        ),
        encode_field_varint(1, 1),
    ],
)
def test_malformed_or_duplicate_envelope_refuses_entire_reading(payload):
    assert parse_smart_meter_dual_message(payload) is None


@pytest.mark.parametrize("value", [-40.0, -40.25, -40.123456, 0.0])
def test_reported_wifi_signal_is_native_optional_and_unitless(value):
    expected = struct.unpack("<f", struct.pack("<f", value))[0]
    assert parse_smart_meter_dual_message(_frame(_power(602, value))) == {
        "wifi_signal_reported": expected
    }
    assert parse_smart_meter_dual_message(_frame(_power(515, 12))) == {"grid_w": 12}
    assert parse_smart_meter_dual_message(
        _frame(_power(602, float("nan")) + _power(515, 12))
    ) == {"grid_w": 12}
    assert parse_smart_meter_dual_message(_frame(encode_field_varint(602, 40))) is None


def test_ambiguous_wifi_signal_does_not_discard_grid_measurements():
    payload = _power(602, -40) + _power(602, -41) + _power(515, 12)
    assert parse_smart_meter_dual_message(_frame(payload)) == {"grid_w": 12}
