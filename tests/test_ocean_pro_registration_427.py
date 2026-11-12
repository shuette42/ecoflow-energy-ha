"""Registration checks for the OCEAN Pro (`HR51`, issue #427).

The parser is covered in test_ocean_pro_proto_427.py and the coordinator
dispatch in tests/ha/test_ocean_pro_dispatch_427.py. These tests hold the
registrations together: the serial prefix, the display name, and the sensor
definitions against what the parser actually emits on the owner's recorded
frames. The serials are fictional (the real one is masked in the fixture).
"""

from __future__ import annotations

import json
from pathlib import Path

from ecoflow_energy.const import (
    _SN_PREFIX_EXCLUDED_KEYS,
    CMSCORE_SENSORS,
    DEVICE_TYPE_DISPLAY_NAMES,
    ENHANCED_ONLY_DEVICE_TYPES,
    OCEANPRO_SENSORS,
    SMARTPANEL40_SENSORS,
)
from ecoflow_energy.ecoflow.const import (
    DEVICE_TYPE_OCEAN_PRO,
    DEVICE_TYPE_SMART_PANEL_40,
    get_device_name,
    get_device_type,
)
from ecoflow_energy.ecoflow.parsers.ocean_pro_proto import parse_hr51_proto_message
from ecoflow_energy.ecoflow.parsers.smart_panel_40_proto import (
    _HR61_FIELD_MAP,
    CMS_CORE_FIELD_MAP,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "ocean_pro" / "hr51_frames_427.json"
_FRAMES = [bytes.fromhex(f["hex"]) for f in json.loads(_FIXTURE.read_text())["frames"]]

# Fictional serials. The tail is letters so the display name stays bare.
_HR51_SN = "HR51TESTDUMMYAAA"
_HR61_SN = "HR61TESTDUMMYAAA"


def _emitted_keys() -> set[str]:
    keys: set[str] = set()
    for payload in _FRAMES:
        parsed = parse_hr51_proto_message(payload)
        if parsed:
            keys.update(parsed)
    return keys


def test_hr51_serial_maps_to_its_own_type_and_the_panel_is_unchanged() -> None:
    assert get_device_type("", _HR51_SN) == DEVICE_TYPE_OCEAN_PRO
    assert get_device_type("", _HR61_SN) == DEVICE_TYPE_SMART_PANEL_40
    assert DEVICE_TYPE_OCEAN_PRO != DEVICE_TYPE_SMART_PANEL_40
    assert DEVICE_TYPE_OCEAN_PRO in ENHANCED_ONLY_DEVICE_TYPES


def test_hr51_display_name_and_single_registration() -> None:
    assert get_device_name("", _HR51_SN) == "OCEAN Pro"
    assert DEVICE_TYPE_DISPLAY_NAMES[DEVICE_TYPE_OCEAN_PRO] == "OCEAN Pro"
    # One registration per distinction: the type carries the difference to the
    # panel, so no excluded-keys entry is added for the prefix.
    assert "HR51" not in _SN_PREFIX_EXCLUDED_KEYS


def test_every_key_the_parser_emits_has_a_sensor_definition() -> None:
    emitted = _emitted_keys()
    assert len(emitted) > 20, f"the fixture frames yield too little: {sorted(emitted)}"
    defined = {d.key for d in OCEANPRO_SENSORS}
    missing = emitted - defined
    assert not missing, f"parser keys without a definition: {sorted(missing)}"


def test_every_definition_is_enhanced_only() -> None:
    not_enhanced = [d.key for d in OCEANPRO_SENSORS if not d.enhanced_only]
    assert not not_enhanced, f"definitions not enhanced_only: {not_enhanced}"


def test_single_battery_class_and_energy_counters() -> None:
    battery = [d.key for d in OCEANPRO_SENSORS if d.device_class == "battery"]
    assert battery == ["battery_soc_pct"]
    by_key = {d.key: d for d in OCEANPRO_SENSORS}
    for key in ("batt_charge_energy_kwh", "batt_discharge_energy_kwh"):
        definition = by_key[key]
        assert definition.device_class == "energy", key
        assert definition.state_class == "total_increasing", key
        assert definition.unit == "kWh", key


def test_keys_are_unique_across_the_ocean_pro_definitions() -> None:
    # The entity unique_id is the serial plus the key, so a repeated key is a
    # repeated unique_id.
    keys = [d.key for d in OCEANPRO_SENSORS]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert not duplicates, f"duplicate keys: {duplicates}"


def test_the_panel_definitions_and_field_table_are_untouched() -> None:
    core_keys = [d.key for d in CMSCORE_SENSORS]
    assert core_keys, "CMSCORE_SENSORS is empty"
    assert [d.key for d in SMARTPANEL40_SENSORS][: len(core_keys)] == core_keys
    assert len(CMS_CORE_FIELD_MAP) == 6
    for field, entry in CMS_CORE_FIELD_MAP.items():
        assert _HR61_FIELD_MAP[field] == entry, field
