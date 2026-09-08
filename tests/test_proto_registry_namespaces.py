"""Tests for the per-device-type command registry (ADR-024, PLAN-131).

The registry used to be one flat ``dict[tuple[int, int], CmdConfig]`` shared
by every device type. ADR-024 splits it into one table per device type,
keyed on the same ``DEVICE_TYPE_*`` constant ``get_device_type()`` already
returns, so a command tuple that means one thing for PowerOcean and another
for the Delta 3 generation can never be looked up in the wrong table.

These two tests guard the two ways that split could quietly come undone:
a caller passing the wrong (or no) device type getting a hit anyway, and a
table being keyed on something that is not a real device type.
"""

from __future__ import annotations

from ecoflow_energy.ecoflow import const as ef_const
from ecoflow_energy.ecoflow.const import DEVICE_TYPE_DELTA3, DEVICE_TYPE_POWEROCEAN
from ecoflow_energy.ecoflow.proto.ecocharge_pb2 import (
    Delta3DisplayProperty,
    JTS1EnergyStreamReport,
)
from ecoflow_energy.ecoflow.proto.runtime import (
    CmdConfig,
    _build_cmd_registry,
    decode_proto_runtime_headers,
)
from ecoflow_energy.ecoflow.proto_encoding import (
    encode_field_bytes,
    encode_field_varint,
)


def _build_frame(cmd_func: int, cmd_id: int, inner: bytes) -> bytes:
    """Build a minimal HeaderMessage frame carrying one pdata payload."""
    header = bytearray()
    header.extend(encode_field_bytes(1, inner))  # pdata
    header.extend(encode_field_varint(8, cmd_func))  # cmd_func
    header.extend(encode_field_varint(9, cmd_id))  # cmd_id
    return encode_field_bytes(1, bytes(header))


def _powerocean_frame() -> bytes:
    """A real PowerOcean energy-stream report, (cmd_func, cmd_id) = (96, 33)."""
    msg = JTS1EnergyStreamReport()
    msg.mppt_pwr = 1200.0
    msg.bp_soc = 60
    return _build_frame(96, 33, msg.SerializeToString())


def _delta3_frame() -> bytes:
    """A real Delta 3 status frame, (cmd_func, cmd_id) = (254, 21)."""
    msg = Delta3DisplayProperty()
    msg.pow_in_sum_w = 500.0
    return _build_frame(254, 21, msg.SerializeToString())


class TestPairedControl:
    """A frame decodes under its own device type and nowhere else.

    This is the alarm ADR-024 asks for: if the two tables were ever merged,
    flattened, or given a union default, the "foreign device type" half of
    each pair below would start succeeding, and would do so silently. Each
    frame here has a real command tuple in the *other* family's table too
    ((96, 33) is absent from Delta 3's table, (254, 21) is absent from
    PowerOcean's), so a passing "foreign" assertion is not merely "no table
    exists for this type" - it is "a real table exists and does not claim
    this frame".
    """

    def test_powerocean_frame_decodes_only_under_its_own_device_type(self):
        frame = _powerocean_frame()

        own = decode_proto_runtime_headers(frame, device_type=DEVICE_TYPE_POWEROCEAN)
        assert len(own) == 1
        assert own[0].parse_path == "typed_runtime:energy_stream_report"

        foreign = decode_proto_runtime_headers(frame, device_type=DEVICE_TYPE_DELTA3)
        assert foreign == []

    def test_delta3_frame_decodes_only_under_its_own_device_type(self):
        frame = _delta3_frame()

        own = decode_proto_runtime_headers(frame, device_type=DEVICE_TYPE_DELTA3)
        assert len(own) == 1
        assert own[0].parse_path == "typed_runtime:delta3_display_property"

        foreign = decode_proto_runtime_headers(
            frame, device_type=DEVICE_TYPE_POWEROCEAN
        )
        assert foreign == []


class TestRegistryNamespaces:
    """Every outer key is a real device type, and no tuple is claimed twice."""

    def test_every_table_key_is_a_real_device_type(self):
        registry = _build_cmd_registry()
        # Sanity: the registry actually built (the pb2 import succeeded), so
        # an empty dict here would hide the whole test behind a false pass.
        assert registry

        valid_device_types = {
            value
            for name, value in vars(ef_const).items()
            if name.startswith("DEVICE_TYPE_")
            and isinstance(value, str)
            and value != ef_const.DEVICE_TYPE_UNKNOWN
        }
        for device_type in registry:
            assert device_type in valid_device_types

    def test_no_tuple_is_claimed_by_two_tables_with_different_configs(self):
        registry = _build_cmd_registry()
        assert registry

        seen: dict[tuple[int, int], tuple[str, CmdConfig]] = {}
        for device_type, table in registry.items():
            for key, config in table.items():
                if key in seen:
                    other_type, other_config = seen[key]
                    # Two families are allowed to mean the same message by the
                    # same tuple only if they share the identical CmdConfig
                    # object - a deliberate shared constant, not a coincidence
                    # of two tables independently defining the same key.
                    assert config is other_config, (
                        f"{key} is claimed by both {other_type} and "
                        f"{device_type} with two different CmdConfig objects"
                    )
                else:
                    seen[key] = (device_type, config)
