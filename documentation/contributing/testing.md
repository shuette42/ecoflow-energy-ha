# Testing

What your pull request must prove, and how to run exactly what CI runs.

## What a pull request must prove

- Ship every code change with a test that fails without the change. Reason: a test that passes either way proves nothing. Revert your change locally once and watch the test fail before you push.
- Test the behaviour a user sees, not only the helper you wrote. Reason: a helper can be right while the entity built on it shows the wrong value.
- State in the pull request description how you observed the change working: the command you ran, or the device you tried it on. Reason: a change nobody observed is a hypothesis.
- For a switch, number or select, state how the write was confirmed on a real unit. Reason: a control without read-back is not accepted (see [Design Principles](design-principles.md)).
- For a scaled value, state the raw value and what the EcoFlow app shows for it. Reason: scaling is per field and cannot be inferred from the name.

## Run what CI runs

Install what CI installs, in a clean virtual environment. Create it with Python 3.14, because the pinned Home Assistant test package requires it:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements_test.txt
pip install "paho-mqtt>=2.1.0" "protobuf>=4.21.0" "cryptography>=42.0.0"
pip install -r requirements-dev.txt
```

Run these five commands before you push. CI runs the same five, and a pull request needs all of them green:

```bash
pytest tests/ -v
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
mypy custom_components/ecoflow_energy
mypy tests/
```

- Run `mypy` twice, once per path, as above. Reason: the tests import the integration under two module names (see below), and mypy refuses one run that reaches the same file under both.
- Use the pinned versions from `requirements-dev.txt` (ruff, mypy) and `requirements_test.txt` (the Home Assistant test package). Reason: a newer tool can report findings that CI does not, and the other way round.
- Run `ruff format custom_components/ tests/` to fix a failing format check. Reason: the format check is a gate of its own.
- Annotate every function in the integration with parameter and return types. Reason: `mypy` runs with `disallow_untyped_defs = true` on the integration, because it skips the body of an unannotated function without saying so. Test functions need no annotations, but their bodies are still checked.

## Where a test goes

- Put a test that needs Home Assistant under `tests/ha/`. Reason: those tests start an in-memory Home Assistant through the `hass` fixture.
- Do not import `homeassistant` directly in a top-level test, and import core code there as `ecoflow_energy.ecoflow.*`. `tests/test_layer_boundary.py` enforces the direct-import rule. Reason: the core library is tested without Home Assistant, and a direct import ties a core test to it.
- Keep a top-level test independent of Home Assistant. `tests/test_ha_independence.py` runs the top-level tests in a process where every Home Assistant import fails, and lists the files that still need Home Assistant. The list can only shrink, so do not add a file to it: write the test under `tests/ha/` instead. Reason: a test that only needs the core library should not need a Home Assistant install to run.
- Import the integration under one name per test file. Tests under `tests/` import core code as `ecoflow_energy.ecoflow.*` (the constants module as `ecoflow_energy.const`), tests under `tests/ha/` as `custom_components.ecoflow_energy.*`. Never use both in one file, and never import a Home Assistant module such as `config_flow` or `sensor` under the short name. `tests/test_import_names.py` enforces this. Reason: the two names load two module objects, so a patch on one misses the other. A platform module loaded under the short name registers a second copy of itself and breaks the config flow tests.
- Name the file as described in [Naming](naming.md).

## Writing the test

- Never write `time.monotonic() - <constant>` in a test. Mock the clock and return a fixed value: set the reference to a fixed positive number (for example `1000.0`) and return that number plus the elapsed time you want. Reason: `time.monotonic()` counts from system boot, and a fresh CI runner can have an uptime of under two minutes, so the subtraction goes negative on CI and never on your machine.
- Apply the same rule to `time.time()`.
- Patch a function that is imported inside a method at the module that defines it, not at the module that calls it. Reason: the name is never an attribute of the calling module, so patching there raises `AttributeError`.
- Assert exact values, not ranges, where the input is fixed.
- Do not publish a placeholder zero on a `total_increasing` sensor. Leave the value absent until a real reading arrives, and assert in the test that the key is absent. Reason: Home Assistant reads a fall in a `total_increasing` value as a meter reset, so a zero ruins the long-term statistics.

## Fixtures

- Build a fixture by hand or from a masked recording. Use dummy serials such as `HJ31TEST0001` in hand-built fixtures and in test code. Never commit a real serial, a key, a token or an unedited diagnostics download. Reason: the repository is public.
- In a fixture made from a recording, replace each serial with a run of `X` of the same length and do not type a made-up serial into the frames. Reason: the identifier check reads any run of twelve or more letters and digits that is not mostly `X` as a leaked identifier.
- Run the full suite after you add a fixture. Reason: `tests/test_fixture_identifiers.py` walks the whole fixture folder, so a new file is checked without anyone adding it to a list.
- Put fixtures in `tests/fixtures/<family>/` (see [Naming](naming.md)).

## Checks that already exist

You do not need to write these again. Run the full suite and read the failure.

- `tests/test_translations.py`: every language file has the same keys as the others.
- `tests/test_fixture_identifiers.py`: no real identifier in any fixture.
- `tests/test_entity_documentation.py`: the entity pages under `documentation/entities/` agree with the code.
- `tests/test_layer_boundary.py`: the core library imports no `homeassistant` and nothing from the layer above it, and no top-level test imports `homeassistant` directly.
- `tests/test_import_names.py`: each test file uses one import name, the right one for its folder.
- `tests/test_ha_independence.py`: the top-level tests that do not need Home Assistant run without it.
- `tests/test_naming_rules.py`: parsers carry a transport suffix, command builders end in `_payload` or `_command`, and no new module name exists in both layers.
- `tests/test_public_paths.py`: no tracked path carries an internal plan number.
- `tests/test_public_docs_boundary.py`: the public documentation, including these pages, stays free of private paths, internal names, real serials, unresolved `ADR-NNN` references and en or em dashes.
- `ruff` and `mypy`: style, imports and types.
