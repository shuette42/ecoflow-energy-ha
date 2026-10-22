# Contributing to EcoFlow Energy

Thanks for contributing to this Home Assistant custom integration for EcoFlow devices. Bug reports, feature requests, documentation improvements and code are all welcome.

## Development Setup

1. **Python 3.14** is required to run the test suite. The pinned Home Assistant test package needs it. Create the virtual environment with it (`python3.14 -m venv .venv`) before you install anything.

2. Clone the repository and install dependencies:
   ```bash
   git clone https://github.com/shuette42/ecoflow-energy-ha.git
   cd ecoflow-energy-ha
   pip install -r requirements_test.txt
   pip install "paho-mqtt>=2.1.0" "protobuf>=4.21.0" "cryptography>=42.0.0"
   pip install -r requirements-dev.txt
   ```

3. For end-to-end testing, you can also run a local Home Assistant development environment. To load the integration, symlink `custom_components/ecoflow_energy` into your HA config directory.

## Running Tests

CI runs the five commands below on every pull request. Run them locally before you open one, all from the repository root:

```bash
pytest tests/ -v
ruff check custom_components/ tests/
ruff format --check custom_components/ tests/
mypy custom_components/ecoflow_energy
mypy tests/
```

`pytest` covers parsers, protocol decoding, API client logic, energy integration and Home Assistant platform setup. `ruff check` and `ruff format --check` are the lint and formatting gates, and `mypy` runs twice, once over the integration and once over the tests. **All of them must pass before you open a pull request.** CI runs Python 3.14. The integration itself supports the Python versions of Home Assistant 2025.1.0 and newer.

## Code and Pull Requests in Short

The detailed rules live in `documentation/contributing/`. In short:

- Put each new file in the right layer: `ecoflow/` never imports Home Assistant. See [Architecture and Structure](documentation/contributing/architecture-and-structure.md).
- Name new files, functions and entity keys the way [Naming](documentation/contributing/naming.md) describes.
- Ship a test that fails without your change, and run the commands above. See [Testing](documentation/contributing/testing.md).
- Add one line to `CHANGELOG.md` and leave the version in `manifest.json` alone. See [Changelog and Versioning](documentation/contributing/changelog-and-versioning.md).
- Check your change against the [Design Principles](documentation/contributing/design-principles.md) before you write code.
- Fork the repository, branch from `main`, keep each pull request to one feature or fix, name the issue with `Ref #N` and write clear commit messages in English (for example `feat: add battery temperature sensor for Delta 2 Max`).

## Adding Device Support

Before writing code, decide whether the hardware is really a **new device** or an
**accessory of one that is already supported**. Getting this wrong is the most
common reason a device-support PR has to be rebuilt from scratch.

An accessory belongs in the existing parser when the cloud API only exposes its
data through the main unit. PowerOcean add-ons such as PowerGlow and PowerPulse
are the clearest example: they show up as separate entries in the account device
list, but a quota request against their own serial number is rejected with error
`1006`, and their telemetry appears as extra keys in the PowerOcean quota
response. Add those keys to the PowerOcean parsers (`ecoflow/parsers/powerocean_http.py` for the quota response, `ecoflow/parsers/powerocean_proto.py` for Enhanced Mode push data). They surface as
additional PowerOcean sensors. A new device type with its own coordinator would
have nothing to poll.

A genuinely new device type is one whose own serial number answers a quota
request, or that delivers its own push stream. Those get their own parser under
`ecoflow/parsers/` and their own entry in the serial-prefix map.

The file-by-file walk, with the WAVE 3 as the worked example, is in
[documentation/add-your-device.md](documentation/add-your-device.md).

### What a device-support PR must include

- **Read-only first.** Sensors and binary sensors only. Do not add switches,
  numbers, or selects unless the write command has been confirmed against real
  firmware, and say in the PR description how it was confirmed. A rejected
  command code is not a confirmed one.
- **A fixture test with dummy values.** Build the test payload by hand and use
  placeholder serials such as `HJ31TEST0001`. Never commit a real serial number,
  a real API key, or an unedited diagnostics dump.
- **Proof of scaling.** Do not infer the unit from the field name. The same API
  mixes watts with deciwatts and watt-hours with kilowatt-hours, sometimes within
  one response. State in the PR which raw value you saw and what the EcoFlow app
  displayed at the same moment. A wrong divisor on an energy sensor silently
  corrupts long-run statistics and cannot be repaired later.
- **`disabled_by_default=True` for hardware-conditional fields.** If a field only
  exists on some installations, for example when an optional accessory is
  attached, the sensor definition must set `disabled_by_default=True` (Home
  Assistant's `entity_registry_enabled_default=False`). Owners who have the
  hardware enable it in one click. Everyone else is spared a permanently
  unavailable entity.
- **No placeholder zeros.** If a field or its container is missing from the
  payload, emit no key at all. Never substitute `0.0`. On a `total_increasing`
  sensor Home Assistant reads a zero as a meter reset and adds the previous total
  again, which double-counts energy for every affected user. "No value yet" is
  the absence of the key.
- **Translations for every new entity.** Each entity key needs an entry in
  `strings.json` and in every file under `translations/` (English, German,
  French, Italian, Dutch, Polish). The test suite enforces this in both
  directions and for every language: a key without a translation fails, and a
  translation without a matching key fails as an orphan. Entities with enum
  states need their state translations too. Write the English and, if you can,
  the German. For the languages you do not speak, copy the English text into
  the file so the key sets stay identical, and say so in the PR. The maintainer
  translates those texts before the merge. Home Assistant renders English for
  any key a language file lacks, so a copied text is never worse than a missing
  one. The tests still reject a missing one.
- **Updated device counts.** The sensor counts in `README.md` and in
  `documentation/entities/` are per device type. If your PR adds sensors, adjust
  the numbers in the same PR.
- **A `CHANGELOG.md` entry** and a green test suite.

Adding one sensor therefore touches around eight files, not one. That is normal
here and not a sign you did something wrong.

## Reporting Issues

Please use [GitHub Issues](https://github.com/shuette42/ecoflow-energy-ha/issues) and include:

- **Home Assistant version** and **integration version**
- **Device model** (as listed under Supported Devices in the README) and firmware version if known
- **Relevant log entries** from Home Assistant (Settings > System > Logs)
- **Steps to reproduce** the issue
- **Expected vs. actual behavior**

## Questions

For general questions about setup or usage, open a [Discussion](https://github.com/shuette42/ecoflow-energy-ha/discussions) rather than an issue.
