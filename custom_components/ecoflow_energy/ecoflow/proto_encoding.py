"""Shared protobuf encoding primitives.

Used by energy_stream.py (PowerOcean) and smartplug.py (SmartPlug SET commands).
"""


def encode_varint(value: int) -> bytes:
    """Encode an int as a protobuf unsigned varint."""
    if value < 0:
        value = value + (1 << 64)
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def encode_field_varint(field_number: int, value: int) -> bytes:
    """Encode a varint field (wire type 0)."""
    tag = (field_number << 3) | 0
    return encode_varint(tag) + encode_varint(value)


def encode_field_bytes(field_number: int, data: bytes) -> bytes:
    """Encode a length-delimited field (wire type 2)."""
    tag = (field_number << 3) | 2
    return encode_varint(tag) + encode_varint(len(data)) + data
