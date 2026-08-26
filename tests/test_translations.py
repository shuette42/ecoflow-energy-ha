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

import pytest

TRANSLATIONS_DIR = Path("custom_components/ecoflow_energy/translations")
CONFIG_FLOW_PATHS = sorted(
    Path("custom_components/ecoflow_energy").glob("config_flow*.py")
)

EN_PATH = TRANSLATIONS_DIR / "en.json"
DE_PATH = TRANSLATIONS_DIR / "de.json"

TRANSLATION_FILES = {"en": EN_PATH, "de": DE_PATH}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parse_config_flow() -> ast.Module:
    """Parse config_flow.py and its sibling flow modules into one AST."""
    merged = ast.Module(body=[], type_ignores=[])
    for path in CONFIG_FLOW_PATHS:
        merged.body.extend(ast.parse(path.read_text()).body)
    return merged


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
        if "OptionsFlow" in class_name or "Options" in class_name:
            flow_type = "options"
        elif "ConfigFlow" in class_name or "FlowMixin" in class_name:
            flow_type = "config"
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

            step_ids: list[str] = []
            placeholders: set[str] = set()
            schema_fields: set[str] = set()

            for kw in child.keywords:
                if kw.arg == "step_id":
                    step_ids = _step_ids_of(kw.value)

                elif kw.arg == "description_placeholders":
                    placeholders = _get_dict_keys(kw.value)

                elif kw.arg == "data_schema":
                    # Walk into the vol.Schema(...) to find Required/Optional keys
                    schema_fields = _extract_schema_keys(kw.value)

            for step_id in step_ids:
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


def _step_ids_of(node: ast.expr) -> list[str]:
    """Return every step id a `step_id=` argument can evaluate to.

    A literal is one id. A conditional is two, and both halves are real
    steps that need their own translations: one form rendered under two
    keys is how a help text varies by sign-in method, since Home Assistant
    parses translations with `string.Formatter` and an ICU select in the
    string is a parse error (#296).

    Reading only literals made both branches look like steps nobody
    renders, which would have turned a correct change into a test failure
    and, worse, would have hidden a genuinely orphaned branch.
    """
    if isinstance(node, ast.IfExp):
        return _step_ids_of(node.body) + _step_ids_of(node.orelse)
    value = _get_string_value(node)
    return [value] if value else []


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

    def test_options_data_and_description_keys_match_across_languages(self):
        """A field's help text has to exist in every language or in none.

        `data` is already covered by the schema test, which asserts that every
        schema field has a label. Nothing covered `data_description`, so a
        help text added in English only would have shipped as a field that
        explains itself to some users and not to others.
        """
        for section in ("data", "data_description"):
            all_keys: dict[str, dict[str, set[str]]] = {}
            for lang, path in TRANSLATION_FILES.items():
                steps = _get_options_steps(_load_translations(path))
                all_keys[lang] = {
                    sid: set(content.get(section, {}).keys())
                    for sid, content in steps.items()
                    if isinstance(content, dict)
                }

            langs = list(all_keys.keys())
            for i in range(len(langs) - 1):
                a, b = langs[i], langs[i + 1]
                for step_id in all_keys[a]:
                    if step_id not in all_keys[b]:
                        continue  # covered by step_ids_match test
                    assert all_keys[a][step_id] == all_keys[b][step_id], (
                        f"Options step '{step_id}' {section} keys differ between "
                        f"{a} and {b}: only in {a}: "
                        f"{all_keys[a][step_id] - all_keys[b][step_id]}, "
                        f"only in {b}: "
                        f"{all_keys[b][step_id] - all_keys[a][step_id]}"
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


# ---------------------------------------------------------------------------
# Exception messages (raised via HomeAssistantError with a translation key)
# ---------------------------------------------------------------------------


STRINGS_PATH = Path("custom_components/ecoflow_energy/strings.json")
ENTITY_PATH = Path("custom_components/ecoflow_energy/entity.py")


def _raised_translation_keys() -> set[str]:
    """Collect translation_key values passed to HomeAssistantError in entity.py."""
    tree = ast.parse(ENTITY_PATH.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "HomeAssistantError":
            continue
        for kw in node.keywords:
            if kw.arg == "translation_key":
                value = _get_string_value(kw.value)
                if value:
                    keys.add(value)
    return keys


class TestExceptionTranslations:
    """A raised error the user cannot read is as silent as no error at all."""

    def test_every_raised_key_has_a_message(self):
        keys = _raised_translation_keys()
        assert keys, "No HomeAssistantError translation keys found in entity.py"

        for path in (STRINGS_PATH, EN_PATH, DE_PATH):
            exceptions = json.loads(path.read_text()).get("exceptions", {})
            missing = keys - set(exceptions)
            assert not missing, f"{path.name} is missing exception messages: {missing}"
            for key in keys:
                assert exceptions[key].get("message"), (
                    f"{path.name}: exception '{key}' has no message"
                )

    def test_exception_placeholders_match_across_languages(self):
        placeholder_re = re.compile(r"\{(\w+)\}")
        per_lang: dict[str, dict[str, set[str]]] = {}
        for lang, path in {"en": EN_PATH, "de": DE_PATH}.items():
            exceptions = json.loads(path.read_text()).get("exceptions", {})
            per_lang[lang] = {
                key: set(placeholder_re.findall(content["message"]))
                for key, content in exceptions.items()
            }

        for key, en_placeholders in per_lang["en"].items():
            assert per_lang["de"].get(key) == en_placeholders, (
                f"Exception '{key}' placeholders differ: "
                f"en={en_placeholders}, de={per_lang['de'].get(key)}"
            )


# ---------------------------------------------------------------------------
# Device picker: the marker and the text that explains it
# ---------------------------------------------------------------------------


class TestDevicePickerExplanation:
    """The device picker has to explain itself in both flows.

    #296: the marker on an unsupported device shipped with an explanation
    that only the initial setup step carried. A user reconfiguring an
    existing entry - which is the route the reporter took - saw the marker
    and no text saying what selecting a device is for, so the label read as
    a warning against a checkbox that was ticked anyway.
    """

    MARKER = "not supported yet"

    # The two facts the reporter needed and could not infer from the marker:
    # what an unsupported device costs him, and what he can do about it.
    # Asserting only that the text mentions the marker passes on a stub that
    # repeats the marker and explains nothing.
    CONSEQUENCE = {"en": "diagnostic sensors", "de": "Diagnose-Sensoren"}
    REMEDY = {"en": "diagnostics download", "de": "Diagnose-Download"}

    @staticmethod
    def _lang(path: Path) -> str:
        return "de" if path.name == "de.json" else "en"

    @classmethod
    def _assert_explains(cls, path: Path, text: str) -> None:
        """The text has to carry the marker, its cost and the way out."""
        lang = cls._lang(path)
        assert cls.MARKER in text, (
            f"{path.name}: text does not mention the '{cls.MARKER}' marker "
            f"it explains"
        )
        assert cls.CONSEQUENCE[lang] in text, (
            f"{path.name}: text names the marker without saying what an "
            f"unsupported device produces instead"
        )
        assert cls.REMEDY[lang] in text, (
            f"{path.name}: text names the problem without saying what the "
            f"owner can do about it"
        )

    @pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, DE_PATH])
    def test_config_devices_step_explains_the_marker(self, path: Path) -> None:
        """The fresh-setup picker explains what the marker costs."""
        step = _get_config_steps(_load_translations(path)).get("devices", {})
        text = step.get("description", "")
        assert text, f"{path.name}: config step 'devices' has no description"
        self._assert_explains(path, text)

    @pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, DE_PATH])
    def test_options_init_explains_the_marker(self, path: Path) -> None:
        """The options picker explains it too, not only the fresh setup."""
        step = _get_options_steps(_load_translations(path)).get("init", {})
        text = step.get("data_description", {}).get("devices", "")
        assert text, (
            f"{path.name}: options step 'init' has no help text for the "
            f"device selector, so a user reconfiguring an entry sees the "
            f"marker with nothing explaining it (#296)"
        )
        self._assert_explains(path, text)

    @pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, DE_PATH])
    def test_both_sign_in_methods_get_their_own_step(self, path: Path) -> None:
        """One form, two translation keys, and both must stay complete.

        The tail of this help text is an instruction only one sign-in method
        can act on, so the flow renders the form under `init` or `init_app`
        and each carries its own wording. A file that lost one of them, or
        let them drift apart in everything except the sentence that differs,
        would send half the users the other half's instruction (#296).
        """
        steps = _get_options_steps(_load_translations(path))
        assert "init_app" in steps, (
            f"{path.name}: no account sign-in rendering of the options step"
        )

        plain, app = steps["init"], steps["init_app"]
        assert set(plain) == set(app), (
            f"{path.name}: the two renderings carry different keys, "
            f"{sorted(set(plain) ^ set(app))}"
        )
        assert plain["title"] == app["title"]
        assert plain["data"] == app["data"]

        plain_help = plain["data_description"]["devices"]
        app_help = app["data_description"]["devices"]
        assert plain_help != app_help, (
            f"{path.name}: both renderings give the same help text, which is "
            f"the split not having happened"
        )
        # Everything except the closing instruction is shared, so a rewrite
        # of one cannot quietly drop the explanation from the other.
        shared = plain_help[: plain_help.rindex(".", 0, len(plain_help) - 200) + 1]
        assert app_help.startswith(shared[:80])

    @pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, DE_PATH])
    def test_only_the_account_rendering_mentions_the_recording(
        self, path: Path
    ) -> None:
        """The one sentence the reporter could not act on.

        He ran developer keys and was told to switch on a recording his
        dialog never shows. The instruction belongs to the account
        rendering alone, in every language.
        """
        phrase = "Aufzeichnung" if path is DE_PATH else "recording"
        steps = _get_options_steps(_load_translations(path))

        app_help = steps["init_app"]["data_description"]["devices"]
        plain_help = steps["init"]["data_description"]["devices"]

        assert phrase in app_help, (
            f"{path.name}: the account rendering no longer says what to "
            f"switch on"
        )
        assert phrase not in plain_help, (
            f"{path.name}: the developer-keys rendering mentions the "
            f"recording, and that mode has no such switch in its dialog "
            f"(#296)"
        )

    @pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, DE_PATH])
    def test_the_setup_step_is_split_the_same_way(self, path: Path) -> None:
        """The picker is reached from setup too, and was equally mode-blind."""
        steps = _get_config_steps(_load_translations(path))
        assert "devices_app" in steps
        assert (
            steps["devices"]["description"] != steps["devices_app"]["description"]
        )

    @pytest.mark.parametrize("path", [STRINGS_PATH, EN_PATH, DE_PATH])
    def test_no_translation_uses_syntax_home_assistant_cannot_parse(
        self, path: Path
    ) -> None:
        """Every string passes `string.Formatter`, because HA parses it.

        `homeassistant/helpers/translation.py` runs each localized string
        through `string.Formatter().parse()` and logs an error it cannot
        recover from when that raises. An ICU `select` is the obvious way to
        vary text by mode and is exactly what this rejects: it shipped to a
        Docker run on 2026-08-26 and produced two parse errors before any
        user saw it.
        """
        import string as _string

        failures = []

        def walk(node: object, path_parts: tuple[str, ...] = ()) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, path_parts + (key,))
            elif isinstance(node, str):
                try:
                    list(_string.Formatter().parse(node))
                except ValueError:
                    failures.append(".".join(path_parts))

        walk(_load_translations(path))

        assert not failures, (
            f"{path.name}: Home Assistant cannot parse {failures}"
        )

    def test_strings_and_en_are_the_same_text(self) -> None:
        """`strings.json` and `en.json` are one language, so they must agree.

        The three-way parametrisation above reads like a source-to-shipped
        symmetry check and is three independent substring checks: giving
        `en.json` entirely different wording, marker intact, left the whole
        suite green. `strings.json` is the file a reviewer reads and
        `en.json` is the file Home Assistant renders, so any divergence
        between them is a mistake rather than a translation.
        """
        strings = _load_translations(STRINGS_PATH)
        english = _load_translations(EN_PATH)
        for section in ("config", "options"):
            assert strings.get(section, {}).get("step") == english.get(
                section, {}
            ).get("step"), (
                f"strings.json and en.json disagree in the '{section}' steps; "
                f"the rendered text is en.json, so the difference ships"
            )

    def test_german_is_actually_translated(self) -> None:
        """The German help texts are German, not the English left in place.

        One English fragment is deliberate: the marker itself is built as a
        hardcoded English literal in `unsupported_suffix()` and never passes
        through the translation layer, so quoting it untranslated is what a
        German user actually sees. That is a phrase, not the whole text.
        """
        english = _load_translations(EN_PATH)
        german = _load_translations(DE_PATH)
        pairs = (
            (
                "config devices description",
                _get_config_steps(english)["devices"]["description"],
                _get_config_steps(german)["devices"]["description"],
            ),
            (
                "options devices help",
                _get_options_steps(english)["init"]["data_description"]["devices"],
                _get_options_steps(german)["init"]["data_description"]["devices"],
            ),
        )
        for label, en_text, de_text in pairs:
            assert en_text != de_text, f"{label}: de.json still carries the English text"

    def test_marker_names_the_consequence(self) -> None:
        """The marker says what it costs, not only that something is off.

        "Not supported" is a status. The reporter who asked for the marker
        could not infer from it that the device would expose nothing, which
        is the fact he needed (#296).
        """
        from custom_components.ecoflow_energy.config_flow_setup import (
            unsupported_suffix,
        )
        from custom_components.ecoflow_energy.const import DEVICE_TYPE_UNKNOWN

        marker = unsupported_suffix(DEVICE_TYPE_UNKNOWN)
        assert self.MARKER in marker
        assert "no data" in marker, (
            f"marker {marker!r} states the status without its consequence"
        )
