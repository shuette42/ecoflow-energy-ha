"""Tests for translation file consistency with config_flow.py.

Validates that:
1. Every {placeholder} in translation descriptions has a matching
   description_placeholders entry in the config flow code.
2. Every step_id used in config_flow.py exists in both en.json and de.json.
3. Every data_schema field in a step has a matching translation data key.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

TRANSLATIONS_DIR = Path("custom_components/ecoflow_energy/translations")
CONFIG_FLOW_PATH = Path("custom_components/ecoflow_energy/config_flow.py")

EN_PATH = TRANSLATIONS_DIR / "en.json"
DE_PATH = TRANSLATIONS_DIR / "de.json"

TRANSLATION_FILES = {"en": EN_PATH, "de": DE_PATH}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parse_config_flow() -> ast.Module:
    """Parse config_flow.py into an AST."""
    return ast.parse(CONFIG_FLOW_PATH.read_text())


def _get_string_value(node: ast.expr) -> str | None:
    """Extract a plain string from an AST node."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _get_dict_keys(node: ast.expr) -> set[str]:
    """Extract string keys from an AST Dict node."""
    keys: set[str] = set()
    if isinstance(node, ast.Dict):
        for k in node.keys:
            val = _get_string_value(k) if k else None
            if val:
                keys.add(val)
    return keys


def _find_async_show_form_calls(tree: ast.Module) -> list[dict]:
    """Find all self.async_show_form() calls and extract step_id, placeholders, schema fields.

    Returns a list of dicts:
        {
            "step_id": str,
            "placeholders": set[str],  # keys from description_placeholders={}
            "schema_fields": set[str], # CONF_* keys from data_schema vol.Required/Optional
            "flow_type": "config" | "options",
            "class_name": str,
        }
    """
    results = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue

        class_name = node.name
        if "ConfigFlow" in class_name:
            flow_type = "config"
        elif "OptionsFlow" in class_name or "Options" in class_name:
            flow_type = "options"
        else:
            continue

        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            # Match self.async_show_form(...)
            if not (
                isinstance(child.func, ast.Attribute)
                and child.func.attr == "async_show_form"
            ):
                continue

            step_id = None
            placeholders: set[str] = set()
            schema_fields: set[str] = set()

            for kw in child.keywords:
                if kw.arg == "step_id":
                    step_id = _get_string_value(kw.value)

                elif kw.arg == "description_placeholders":
                    placeholders = _get_dict_keys(kw.value)

                elif kw.arg == "data_schema":
                    # Walk into the vol.Schema(...) to find Required/Optional keys
                    schema_fields = _extract_schema_keys(kw.value)

            if step_id:
                results.append(
                    {
                        "step_id": step_id,
                        "placeholders": placeholders,
                        "flow_type": flow_type,
                        "schema_fields": schema_fields,
                        "class_name": class_name,
                    }
                )

    return results


def _extract_schema_keys(node: ast.expr) -> set[str]:
    """Extract field keys from a vol.Schema({vol.Required(KEY): ...}) AST node.

    Handles both string literals and CONF_* name references.
    """
    keys: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        # Match vol.Required(...) or vol.Optional(...)
        if isinstance(child.func, ast.Attribute) and child.func.attr in (
            "Required",
            "Optional",
        ):
            if child.args:
                arg = child.args[0]
                # Direct string literal
                s = _get_string_value(arg)
                if s:
                    keys.add(s)
                # CONF_* name reference - resolve from const.py
                elif isinstance(arg, ast.Name):
                    resolved = _resolve_const(arg.id)
                    if resolved:
                        keys.add(resolved)
    return keys


def _resolve_const(name: str) -> str | None:
    """Resolve a CONF_* constant name to its string value by parsing const.py."""
    const_path = Path("custom_components/ecoflow_energy/const.py")
    tree = ast.parse(const_path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return _get_string_value(node.value)
    return None


def _find_placeholders_in_text(text: str) -> set[str]:
    """Find all {placeholder} patterns in a translation string."""
    return set(re.findall(r"\{(\w+)\}", text))


def _collect_translation_placeholders(
    step_data: dict,
) -> dict[str, set[str]]:
    """For each step_id in translation data, collect all {placeholder} references.

    Scans title, description, and data label values.
    """
    result: dict[str, set[str]] = {}
    for step_id, step_content in step_data.items():
        placeholders: set[str] = set()
        if isinstance(step_content, dict):
            for field in ("title", "description"):
                text = step_content.get(field, "")
                if isinstance(text, str):
                    placeholders |= _find_placeholders_in_text(text)
            # Also check data labels (unlikely but thorough)
            data = step_content.get("data", {})
            if isinstance(data, dict):
                for label in data.values():
                    if isinstance(label, str):
                        placeholders |= _find_placeholders_in_text(label)
        result[step_id] = placeholders
    return result


# ---------------------------------------------------------------------------
# Load helpers
# ---------------------------------------------------------------------------


def _load_translations(path: Path) -> dict:
    return json.loads(path.read_text())


def _get_config_steps(translations: dict) -> dict:
    return translations.get("config", {}).get("step", {})


def _get_options_steps(translations: dict) -> dict:
    return translations.get("options", {}).get("step", {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPlaceholderConsistency:
    """Every {placeholder} in translation descriptions must be provided by code."""

    def test_config_flow_placeholders_provided(self):
        """Config flow: translation placeholders are a subset of code placeholders."""
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        config_calls = [c for c in form_calls if c["flow_type"] == "config"]

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            config_steps = _get_config_steps(translations)
            trans_placeholders = _collect_translation_placeholders(config_steps)

            for call in config_calls:
                step_id = call["step_id"]
                code_placeholders = call["placeholders"]
                needed = trans_placeholders.get(step_id, set())

                missing = needed - code_placeholders
                assert not missing, (
                    f"[{lang}] config step '{step_id}': translation uses "
                    f"placeholders {missing} but code does not provide them "
                    f"in description_placeholders. "
                    f"Code provides: {code_placeholders or '{}'}"
                )

    def test_options_flow_placeholders_provided(self):
        """Options flow: translation placeholders are a subset of code placeholders."""
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        options_calls = [c for c in form_calls if c["flow_type"] == "options"]

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            options_steps = _get_options_steps(translations)
            trans_placeholders = _collect_translation_placeholders(options_steps)

            for call in options_calls:
                step_id = call["step_id"]
                code_placeholders = call["placeholders"]
                needed = trans_placeholders.get(step_id, set())

                missing = needed - code_placeholders
                assert not missing, (
                    f"[{lang}] options step '{step_id}': translation uses "
                    f"placeholders {missing} but code does not provide them "
                    f"in description_placeholders. "
                    f"Code provides: {code_placeholders or '{}'}"
                )


class TestTranslationCompleteness:
    """Every step_id in config_flow.py must exist in all translation files."""

    def test_config_steps_present_in_all_languages(self):
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        config_step_ids = {
            c["step_id"] for c in form_calls if c["flow_type"] == "config"
        }

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            config_steps = _get_config_steps(translations)

            for step_id in config_step_ids:
                assert step_id in config_steps, (
                    f"[{lang}] config step '{step_id}' used in code "
                    f"but missing from {path.name}"
                )

    def test_options_steps_present_in_all_languages(self):
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        options_step_ids = {
            c["step_id"] for c in form_calls if c["flow_type"] == "options"
        }

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            options_steps = _get_options_steps(translations)

            for step_id in options_step_ids:
                assert step_id in options_steps, (
                    f"[{lang}] options step '{step_id}' used in code "
                    f"but missing from {path.name}"
                )

    def test_no_orphan_translation_steps(self):
        """Translation files should not have steps that exist in no flow at all.

        Note: HA allows config.step entries to be used by OptionsFlow as well
        (legacy pattern). A step is only orphaned if it appears in neither the
        ConfigFlow nor the OptionsFlow code.
        """
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        all_step_ids = {c["step_id"] for c in form_calls}

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)

            config_steps = set(_get_config_steps(translations).keys())
            orphan_config = config_steps - all_step_ids
            assert not orphan_config, (
                f"[{lang}] config steps {orphan_config} exist in {path.name} "
                f"but have no matching async_show_form() in any flow class"
            )

            options_steps = set(_get_options_steps(translations).keys())
            orphan_options = options_steps - all_step_ids
            assert not orphan_options, (
                f"[{lang}] options steps {orphan_options} exist in {path.name} "
                f"but have no matching async_show_form() in any flow class"
            )


class TestDataSchemaMatch:
    """Every schema field in async_show_form data_schema should have a translation."""

    def test_config_schema_fields_have_translations(self):
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        config_calls = [c for c in form_calls if c["flow_type"] == "config"]

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            config_steps = _get_config_steps(translations)

            for call in config_calls:
                step_id = call["step_id"]
                schema_fields = call["schema_fields"]

                if not schema_fields:
                    continue

                step_trans = config_steps.get(step_id, {})
                data_keys = set(step_trans.get("data", {}).keys())

                missing = schema_fields - data_keys
                assert not missing, (
                    f"[{lang}] config step '{step_id}': schema fields {missing} "
                    f"have no translation in data dict. "
                    f"Translation data keys: {data_keys}"
                )

    def test_options_schema_fields_have_translations(self):
        tree = _parse_config_flow()
        form_calls = _find_async_show_form_calls(tree)

        options_calls = [c for c in form_calls if c["flow_type"] == "options"]

        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            options_steps = _get_options_steps(translations)

            for call in options_calls:
                step_id = call["step_id"]
                schema_fields = call["schema_fields"]

                if not schema_fields:
                    continue

                step_trans = options_steps.get(step_id, {})
                data_keys = set(step_trans.get("data", {}).keys())

                missing = schema_fields - data_keys
                assert not missing, (
                    f"[{lang}] options step '{step_id}': schema fields {missing} "
                    f"have no translation in data dict. "
                    f"Translation data keys: {data_keys}"
                )


class TestLanguageConsistency:
    """All translation files must have the same step structure."""

    def test_config_step_ids_match_across_languages(self):
        all_step_ids: dict[str, set[str]] = {}
        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            all_step_ids[lang] = set(_get_config_steps(translations).keys())

        langs = list(all_step_ids.keys())
        for i in range(len(langs) - 1):
            a, b = langs[i], langs[i + 1]
            assert all_step_ids[a] == all_step_ids[b], (
                f"Config step mismatch between {a} and {b}: "
                f"only in {a}: {all_step_ids[a] - all_step_ids[b]}, "
                f"only in {b}: {all_step_ids[b] - all_step_ids[a]}"
            )

    def test_options_step_ids_match_across_languages(self):
        all_step_ids: dict[str, set[str]] = {}
        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            all_step_ids[lang] = set(_get_options_steps(translations).keys())

        langs = list(all_step_ids.keys())
        for i in range(len(langs) - 1):
            a, b = langs[i], langs[i + 1]
            assert all_step_ids[a] == all_step_ids[b], (
                f"Options step mismatch between {a} and {b}: "
                f"only in {a}: {all_step_ids[a] - all_step_ids[b]}, "
                f"only in {b}: {all_step_ids[b] - all_step_ids[a]}"
            )

    def test_config_data_keys_match_across_languages(self):
        """Each config step's data dict keys should match across languages."""
        all_data: dict[str, dict[str, set[str]]] = {}
        for lang, path in TRANSLATION_FILES.items():
            translations = _load_translations(path)
            steps = _get_config_steps(translations)
            all_data[lang] = {
                sid: set(content.get("data", {}).keys())
                for sid, content in steps.items()
                if isinstance(content, dict)
            }

        langs = list(all_data.keys())
        for i in range(len(langs) - 1):
            a, b = langs[i], langs[i + 1]
            for step_id in all_data[a]:
                if step_id not in all_data[b]:
                    continue  # covered by step_ids_match test
                assert all_data[a][step_id] == all_data[b][step_id], (
                    f"Config step '{step_id}' data keys differ between {a} and {b}: "
                    f"only in {a}: {all_data[a][step_id] - all_data[b][step_id]}, "
                    f"only in {b}: {all_data[b][step_id] - all_data[a][step_id]}"
                )
