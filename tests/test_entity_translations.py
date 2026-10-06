"""Entity translation completeness tests.

Guards the contract between the entity definitions in const.py and the
translation files:

1. Every entity key defined in const.py has a translation entry in BOTH
   en.json and de.json under the matching platform.
2. Every enum options entry (sensor + select) has a state translation in
   both languages.
3. No orphan translation keys: a translation entry without an entity
   definition is flagged so leftovers of removed entities cannot creep
   back in.

The definition lists are discovered by naming convention, so new device
types are covered automatically.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any

import pytest
from ecoflow_energy import const as C

TRANSLATIONS_DIR = Path("custom_components/ecoflow_energy/translations")
LANGS = ("en", "de")

# Diagnostic sensors created directly in sensor.py (not definition-driven)
DIAGNOSTIC_SENSOR_KEYS = {"mqtt_status", "connection_mode"}


# Runtime-discovered vehicle sensors in charging_history.py.
def _vehicle_sensor_keys(source: str | None = None) -> set[str]:
    tree = ast.parse(
        source
        if source is not None
        else Path("custom_components/ecoflow_energy/charging_history.py").read_text()
    )
    keys: set[str] = set()

    def values(node: ast.expr) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return values(node.body) | values(node.orelse)
        raise AssertionError("Unsupported translation-key expression")

    def is_key(target: ast.expr) -> bool:
        return (
            isinstance(target, ast.Attribute) and target.attr == "_attr_translation_key"
        ) or (isinstance(target, ast.Name) and target.id == "_attr_translation_key")

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(is_key(t) for t in node.targets)
            or isinstance(node, ast.AnnAssign)
            and is_key(node.target)
            and node.value
        ):
            assert node.value is not None
            keys.update(values(node.value))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "translation_key":
                    keys.update(values(keyword.value))
    assert keys, "No vehicle translation keys discovered"
    return keys


VEHICLE_SENSOR_KEYS = _vehicle_sensor_keys()


def _collect(pattern: str) -> dict[str, Any]:
    """Collect entity definitions from all const.py lists matching a pattern."""
    defs: dict[str, Any] = {}
    for name in dir(C):
        if re.fullmatch(pattern, name) and isinstance(getattr(C, name), list):
            for item in getattr(C, name):
                defs.setdefault(item.key, item)
    return defs


def _labels(pattern: str) -> set[str]:
    """The names the translation file must carry for definitions matching `pattern`.

    A definition may point its label at a different entry than its key, so
    that one reading can be named differently on different devices without
    changing the key that builds its unique_id. Both tests below follow the
    label rather than the key, or a split definition would read as a missing
    translation on one side and an orphan entry on the other.

    This walks every list rather than reusing the deduplicated `_collect`
    output: a key shared by several device types keeps only its first
    definition there, which is exactly the one that has no split.
    """
    labels: set[str] = set()
    for name in dir(C):
        if re.fullmatch(pattern, name) and isinstance(getattr(C, name), list):
            for item in getattr(C, name):
                labels.add(getattr(item, "translation_key", None) or item.key)
    return labels


SENSOR_DEFS = _collect(r"[A-Z0-9]+_SENSORS")
BINARY_SENSOR_DEFS = _collect(r"[A-Z0-9]+_BINARY_SENSORS")
SWITCH_DEFS = _collect(r"[A-Z0-9]+_SWITCHES")
NUMBER_DEFS = _collect(r"[A-Z0-9]+_NUMBERS")
SELECT_DEFS = _collect(r"[A-Z0-9]+_SELECTS")
BUTTON_DEFS = _collect(r"[A-Z0-9]+_BUTTONS")

PLATFORM_KEYS = {
    "sensor": _labels(r"[A-Z0-9]+_SENSORS")
    | DIAGNOSTIC_SENSOR_KEYS
    | VEHICLE_SENSOR_KEYS,
    "binary_sensor": _labels(r"[A-Z0-9]+_BINARY_SENSORS"),
    "switch": _labels(r"[A-Z0-9]+_SWITCHES"),
    "number": _labels(r"[A-Z0-9]+_NUMBERS"),
    "select": _labels(r"[A-Z0-9]+_SELECTS"),
    "button": _labels(r"[A-Z0-9]+_BUTTONS"),
}


def _load_entity_translations(lang: str) -> dict:
    path = TRANSLATIONS_DIR / f"{lang}.json"
    return json.loads(path.read_text())["entity"]


class TestEntityTranslationCompleteness:
    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("platform", sorted(PLATFORM_KEYS))
    def test_every_entity_key_has_translation(self, lang: str, platform: str) -> None:
        """Every defined entity key exists in the translation file."""
        translations = _load_entity_translations(lang).get(platform, {})
        missing = PLATFORM_KEYS[platform] - set(translations)
        assert not missing, (
            f"[{lang}] {platform} keys without translation: {sorted(missing)}"
        )

    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("platform", sorted(PLATFORM_KEYS))
    def test_no_orphan_translation_keys(self, lang: str, platform: str) -> None:
        """Every translation key maps back to an entity definition."""
        translations = _load_entity_translations(lang).get(platform, {})
        orphans = set(translations) - PLATFORM_KEYS[platform]
        assert not orphans, (
            f"[{lang}] {platform} translations without entity definition: "
            f"{sorted(orphans)} - remove them or add the definition"
        )

    @pytest.mark.parametrize("lang", LANGS)
    def test_every_sensor_enum_option_has_state_translation(self, lang: str) -> None:
        """Each sensor options entry has a state translation."""
        translations = _load_entity_translations(lang).get("sensor", {})
        problems: list[str] = []
        for key, definition in SENSOR_DEFS.items():
            options = getattr(definition, "options", None)
            if not options:
                continue
            states = translations.get(key, {}).get("state", {})
            missing = [opt for opt in options if opt not in states]
            if missing:
                problems.append(f"{key}: {missing}")
        assert not problems, (
            f"[{lang}] sensor enum options without state translation: {problems}"
        )

    @pytest.mark.parametrize("lang", LANGS)
    def test_every_select_option_has_state_translation(self, lang: str) -> None:
        """Each select options entry has a state translation."""
        translations = _load_entity_translations(lang).get("select", {})
        problems: list[str] = []
        for key, definition in SELECT_DEFS.items():
            states = translations.get(key, {}).get("state", {})
            missing = [opt for opt in definition.options if opt not in states]
            if missing:
                problems.append(f"{key}: {missing}")
        assert not problems, (
            f"[{lang}] select options without state translation: {problems}"
        )


def test_charging_current_setpoint_is_named_as_a_setpoint() -> None:
    """The PowerPulse 2 session setpoint is not named like a measured current.

    `ev_charge_current_a` is the heartbeat's charging current setpoint (it can
    read 6 A on an idle wallbox), shown by a sensor and written by a number on
    the wallbox's own channel. Both carry the same name in the definitions and
    in each language, so a rename in one place cannot drift from the others.
    """
    names = {
        "en": "Wallbox Charging Current Setpoint",
        "de": "Wallbox-Ladestrom-Sollwert",
    }
    sensor_def = next(
        d for d in C.POWERPULSE2_SENSORS if d.key == "ev_charge_current_a"
    )
    number_def = next(
        d for d in C.POWERPULSE2_NUMBERS if d.key == "ev_charge_current_a"
    )
    assert sensor_def.name == names["en"]
    assert number_def.name == names["en"]
    for lang, expected in names.items():
        entity = json.loads(
            (TRANSLATIONS_DIR / f"{lang}.json").read_text(encoding="utf-8")
        )["entity"]
        assert entity["sensor"]["ev_charge_current_a"]["name"] == expected
        assert entity["number"]["ev_charge_current_a"]["name"] == expected


STRINGS_PATH = Path("custom_components/ecoflow_energy/strings.json")

# The wallbox energy sensors are created at runtime from three translation
# keys. The names are pinned here as literals, not read back from the files
# under test, so a rename in one file cannot pass by agreeing with itself.
VEHICLE_ENERGY_NAMES = {
    "en": {
        "vehicle_energy": "Wallbox Completed Charging Energy {vehicle}",
        "unnamed_vehicle_energy": "Wallbox Completed Charging Energy Unnamed Vehicle",
        "other_vehicle_energy": "Wallbox Completed Charging Energy Unassigned",
    },
    "de": {
        "vehicle_energy": "Wallbox-Energie abgeschlossener Ladevorgänge {vehicle}",
        "unnamed_vehicle_energy": (
            "Wallbox-Energie abgeschlossener Ladevorgänge Unbenanntes Fahrzeug"
        ),
        "other_vehicle_energy": (
            "Wallbox-Energie abgeschlossener Ladevorgänge nicht zugeordnet"
        ),
    },
}


def test_vehicle_energy_names_match_across_files() -> None:
    """The three runtime wallbox energy names agree in every shipped file.

    `strings.json` is the English source and must read exactly like
    `en.json`; `de.json` carries the German wording of the same three keys.
    The key set is compared with what `charging_history.py` actually uses, so
    a fourth dynamic key without a pinned name fails here instead of shipping
    unnamed.
    """
    assert set(VEHICLE_ENERGY_NAMES["en"]) == VEHICLE_SENSOR_KEYS
    assert set(VEHICLE_ENERGY_NAMES["de"]) == set(VEHICLE_ENERGY_NAMES["en"])
    strings = json.loads(STRINGS_PATH.read_text(encoding="utf-8"))["entity"]["sensor"]
    assert {k: strings[k]["name"] for k in VEHICLE_ENERGY_NAMES["en"]} == (
        VEHICLE_ENERGY_NAMES["en"]
    )
    for lang, expected in VEHICLE_ENERGY_NAMES.items():
        sensors = _load_entity_translations(lang)["sensor"]
        assert {k: sensors[k]["name"] for k in expected} == expected, lang


@pytest.mark.parametrize(
    "source",
    [
        'class Sensor: _attr_translation_key = "new_key"',
        'class Sensor: _attr_translation_key: str = "new_key"',
        'self._attr_translation_key: str = "new_key"',
        'self._attr_translation_key = "new_key"',
        'SensorEntityDescription(key="energy", translation_key="new_key")',
    ],
)
def test_vehicle_translation_discovery_shapes(source):
    assert _vehicle_sensor_keys(source) == {"new_key"}
