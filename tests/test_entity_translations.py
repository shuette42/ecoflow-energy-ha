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

import json
import re
from pathlib import Path

import pytest

from ecoflow_energy import const as C

TRANSLATIONS_DIR = Path("custom_components/ecoflow_energy/translations")
LANGS = ("en", "de")

# Diagnostic sensors created directly in sensor.py (not definition-driven)
DIAGNOSTIC_SENSOR_KEYS = {"mqtt_status", "connection_mode"}


def _collect(pattern: str) -> dict[str, object]:
    """Collect entity definitions from all const.py lists matching a pattern."""
    defs: dict[str, object] = {}
    for name in dir(C):
        if re.fullmatch(pattern, name) and isinstance(getattr(C, name), list):
            for item in getattr(C, name):
                defs.setdefault(item.key, item)
    return defs


SENSOR_DEFS = _collect(r"[A-Z0-9]+_SENSORS")
BINARY_SENSOR_DEFS = _collect(r"[A-Z0-9]+_BINARY_SENSORS")
SWITCH_DEFS = _collect(r"[A-Z0-9]+_SWITCHES")
NUMBER_DEFS = _collect(r"[A-Z0-9]+_NUMBERS")
SELECT_DEFS = _collect(r"[A-Z0-9]+_SELECTS")

PLATFORM_KEYS = {
    "sensor": set(SENSOR_DEFS) | DIAGNOSTIC_SENSOR_KEYS,
    "binary_sensor": set(BINARY_SENSOR_DEFS),
    "switch": set(SWITCH_DEFS),
    "number": set(NUMBER_DEFS),
    "select": set(SELECT_DEFS),
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
