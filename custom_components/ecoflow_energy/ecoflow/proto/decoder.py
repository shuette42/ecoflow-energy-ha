"""EcoFlow protobuf header decoder.

Decodes the outer EcoFlow frame header (wrapper structure).
Inner payload fields are decoded via the generated pb2 classes.
"""

import struct

_HEADER_FIELDS = {
    1: ("pdata", "bytes"), 2: ("src", "i32"), 3: ("dest", "i32"),
    4: ("d_src", "i32"), 5: ("d_dest", "i32"), 6: ("enc_type", "i32"),
    7: ("check_type", "i32"), 8: ("cmd_func", "i32"), 9: ("cmd_id", "i32"),
    10: ("data_len", "i32"), 11: ("need_ack", "i32"), 12: ("is_ack", "i32"),
    14: ("seq", "i32"), 15: ("product_id", "i32"), 16: ("version", "i32"),
    17: ("payload_ver", "i32"), 18: ("time_snap", "i32"),
    19: ("is_rw_cmd", "i32"), 20: ("is_queue", "i32"),
    21: ("ack_type", "i32"), 22: ("code", "str"), 23: ("from", "str"),
    24: ("module_sn", "str"), 25: ("device_sn", "str"),
}


def _read_varint(mv, i):
    shift = 0
    res = 0
    while True:
        b = mv[i]
        i += 1
        res |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, i


def _try_utf8(b: bytes):
    try:
        return b.decode("utf-8")
    except Exception:
        return b.hex()


def _decode_single_header(hdr: bytes) -> dict:
    mv = memoryview(hdr)
    i = 0
    out = {}
    while i < len(mv):
        key, i = _read_varint(mv, i)
        fn, wt = key >> 3, key & 0x07
        name, typ = _HEADER_FIELDS.get(fn, (f"f{fn}", None))
        if wt == 0:
            val, i = _read_varint(mv, i)
            out[name] = int(val)
        elif wt == 2:
            length, i = _read_varint(mv, i)
            sub = mv[i:i + length].tobytes()
            i += length
            if typ == "str":
                out[name] = _try_utf8(sub)
            elif typ == "bytes":
                out[name] = sub.hex()
            else:
                out[name] = sub.hex()
        elif wt == 5:
            f = struct.unpack("<f", mv[i:i + 4])[0]
            i += 4
            out[name] = f
        elif wt == 1:
            out[name] = int.from_bytes(mv[i:i + 8], "little")
            i += 8
        else:
            break
    return out


def decode_header_message(b: bytes):
    """Decode an EcoFlow frame: returns (headers, payload)."""
    headers = []
    payload = None
    mv = memoryview(b)
    i = 0
    while i < len(mv):
        key, i = _read_varint(mv, i)
        fn, wt = key >> 3, key & 0x07
        if fn == 1 and wt == 2:
            length, i = _read_varint(mv, i)
            hdr = mv[i:i + length].tobytes()
            i += length
            headers.append(_decode_single_header(hdr))
        elif fn == 2 and wt == 2:
            length, i = _read_varint(mv, i)
            payload = mv[i:i + length].tobytes()
            i += length
        else:
            if wt == 0:
                _, i = _read_varint(mv, i)
            elif wt == 1:
                i += 8
            elif wt == 2:
                length, i = _read_varint(mv, i)
                i += length
            elif wt == 5:
                i += 4
            else:
                break
    return headers, payload
