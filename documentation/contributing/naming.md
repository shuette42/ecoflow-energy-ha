# Naming

One naming scheme for new files, functions and entity keys. Follow it for new code, and check the [Known exceptions](#known-exceptions) before you copy the name of an older file.

## Files and modules

- Name Python modules in `snake_case.py`. Reason: every existing module is named that way.
- Name parsers `ecoflow/parsers/<family>_<transport>.py`, where the transport is `proto` (push over MQTT), `http` (IoT Developer API quota) or `modbus` (Local mode). Examples: `ocean2_proto.py`, `stream_http.py`, `powerocean_modbus.py`. Reason: one family can have several routes, and the suffix says which one the file reads.
- Name command builders `ecoflow/<family>_commands.py`. Examples: `ocean2_commands.py`, `stream_commands.py`. Reason: a reader finds every write for a family in one predictable place.
- Name config flow steps `config_flow_<step>.py` (`config_flow_setup.py`, `config_flow_options.py`). Reason: the existing steps already follow it.
- Use the lowercase product family for `<family>`, as the existing parser names do (`ocean2`, `stream`, `delta3`, `wave3`, `stream_ac5000`). Use a serial prefix (`hr61`) only when the device has no family name. Reason: a prefix tells nothing to a reader who does not know the serial scheme.
- Keep a name unique across layers. Do not give a new root module the name of a core module. Reason: see [Architecture and Structure](architecture-and-structure.md).

## Functions

- Name a command builder that returns bytes `build_<what>_payload`. Example: `build_ocean2_backup_reserve_payload`.
- Name a command builder that returns a JSON dict `build_<what>_command`. Example: `build_feed_in_control_command`.
- Name a parser of the device's reply to a write `parse_<what>_ack`. Example: `parse_ocean2_property_ack`.
- Reason for all three: the return type is visible at the call site, without opening the function.
- Annotate every function in the integration with its types. Reason: the type checker skips an unannotated function entirely (see [Testing](testing.md)).
- Create the logger as `_LOGGER = logging.getLogger(__name__)`. Reason: Home Assistant filters log output by module name.

## Entity keys

- Write an entity key in `snake_case` and name the reading, with the unit as a suffix where the neighbouring keys have one (`grid_power_w`). Reason: keys are read in logs, diagnostics and translations.
- Treat a key as permanent once released. Do not rename it. Reason: the unique ID of an entity is the device serial plus the key, so a rename creates a new entity and orphans the history of the old one.
- Add a translation for every new key to `strings.json` and to every file under `translations/`. Reason: the tests hold all six language files to the same key set.

## Tests and fixtures

- Name a test for code that runs without Home Assistant `tests/test_<module>.py`, or `tests/test_<behaviour>.py` when it covers one behaviour across modules. Examples: `test_ocean2_commands.py`, `test_translations.py`.
- Name the test that replays a device family's recorded frames through its parser `tests/test_<family>_parser.py`. Example: `test_wave3_parser.py`. Reason: [Adding Your Device](../add-your-device.md) uses that name, and the existing tests already follow it.
- Put a test that needs Home Assistant under `tests/ha/`, named `tests/ha/test_<...>.py`. Reason: see [Testing](testing.md).
- Name a fixture `tests/fixtures/<family>/<what>_frames.json`, with a date suffix such as `_20261001` when you add a second recording of the same thing. Example: `tests/fixtures/ocean2/storm_guard_frames.json`. Reason: the folder is the device family, the file name is the situation that was recorded.
- Use dummy serials such as `HJ31TEST0001` in hand-built fixtures and tests, and a run of `X` of the same length in frames taken from a recording. Never use a real serial. Reason: the repository is public.

## Documentation

- Name documentation files in kebab-case under `documentation/` (`add-your-device.md`, `wave-3.md`). Reason: the existing pages follow it and the links stay valid on case-sensitive systems.
- Root files that GitHub recognizes by name stay in capitals: `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`.
- Start every page with a `#` title and one line that says what the page is for.

## Known exceptions

Older names that do not follow the scheme. They stay because renaming them changes patch paths in the tests, import paths and the line history for no gain to users. New code follows the scheme above.

| Existing name | Why it stays | What new code does |
|---|---|---|
| Parsers without a transport suffix: `delta.py`, `powerocean.py`, `smartplug.py` | Tests and imports use these paths | Name new parsers `<family>_<transport>.py` |
| `hr61_proto.py` is named by serial prefix, not by family | Same | Use the family name |
| `wave3_commands.build_write`, `build_power_write`, `build_band_write` return bytes without `_payload` in the name | Callers and patch strings use them | Name new bytes builders `build_<what>_payload` |
| `delta3_commands.build_proto_command` returns bytes or `None` under a `_command` name | Same | Use `_payload` for bytes, `_command` for a JSON dict |
| `energy_stream.py` holds the PowerOcean, PowerGlow and PowerPulse builders, not a `<family>_commands.py` file | It predates the pattern | Put new builders in `ecoflow/<family>_commands.py` |
