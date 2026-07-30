# Contributing to EcoFlow Energy

Thank you for your interest in contributing to this Home Assistant custom integration for EcoFlow devices. Contributions of all kinds are welcome -- bug reports, feature requests, documentation improvements, and code.

## Development Setup

1. **Python 3.12+** is required.

2. Clone the repository and install dependencies:
   ```bash
   git clone https://github.com/shuette42/ecoflow-energy-ha.git
   cd ecoflow-energy-ha
   pip install paho-mqtt protobuf pytest pytest-homeassistant-custom-component
   ```

3. Optionally, set up a local Home Assistant development environment for end-to-end testing. The integration can be loaded by symlinking `custom_components/ecoflow_energy` into your HA config directory.

## Running Tests

```bash
python3 -m pytest
```

The test suite contains 550+ tests covering parsers, protocol decoding, API client logic, energy integration, and Home Assistant platform setup. **All tests must pass before submitting a pull request.**

## Code Style

- Follow **PEP 8** conventions.
- Use `from __future__ import annotations` at the top of every Python file.
- Add **type hints** to all function signatures.
- Use `_LOGGER = logging.getLogger(__name__)` for logging (Home Assistant convention).
- Keep log messages actionable and include context (device SN, error reason). Transient errors should use `DEBUG` level, not `WARNING` or `ERROR`.

## Architecture Overview

All integration code lives under `custom_components/ecoflow_energy/`:

```
custom_components/ecoflow_energy/
    __init__.py          # HA setup and teardown
    coordinator/         # DataUpdateCoordinator (HTTP polling + MQTT push), split into focused modules
    config_flow.py       # ConfigFlow and OptionsFlow
    sensor.py            # Sensor entities
    binary_sensor.py     # Binary sensor entities
    switch.py            # Switch entities
    number.py            # Number entities
    const.py             # Entity definitions per device type
    ecoflow/             # Core library (no HA dependencies)
        iot_api.py       # IoT Developer API client
        cloud_http.py    # HTTP quota polling
        cloud_mqtt.py    # MQTT client (TCP + WSS)
        parsers/         # Device-specific parsers
        proto/           # Protobuf decoder and bindings
```

The `ecoflow/` subdirectory contains the core library with no Home Assistant dependencies, making it independently testable. Entity platforms use the `CoordinatorEntity` pattern.

## Adding Device Support

Before writing code, decide whether the hardware is really a **new device** or an
**accessory of one that is already supported**. Getting this wrong is the most
common reason a device-support PR has to be rebuilt from scratch.

An accessory belongs in the existing parser when the cloud API only exposes its
data through the main unit. PowerOcean add-ons such as PowerGlow and PowerPulse
are the clearest example: they show up as separate entries in the account device
list, but a quota request against their own serial number is rejected with error
`1006`, and their telemetry appears as extra keys in the PowerOcean quota
response. Those keys are added to `ecoflow/parsers/powerocean.py` and surfaced as
additional PowerOcean sensors. A new device type with its own coordinator would
have nothing to poll.

A genuinely new device type is one whose own serial number answers a quota
request, or that delivers its own push stream. Those get their own parser under
`ecoflow/parsers/` and their own entry in the serial-prefix map.

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
  hardware enable it in one click; everyone else is not left with a permanently
  unavailable entity.
- **No placeholder zeros.** If a field or its container is missing from the
  payload, emit no key at all. Never substitute `0.0`. On a `total_increasing`
  sensor Home Assistant reads a zero as a meter reset and adds the previous total
  again, which double-counts energy for every affected user. "No value yet" is
  the absence of the key.
- **Translations for every new entity.** Each entity key needs an entry in
  `strings.json` and in both `translations/en.json` and `translations/de.json`.
  This is enforced by the test suite in both directions: a key without
  translations fails, and a translation without a matching key fails as an
  orphan. Entities with enum states need their state translations too.
- **Updated device counts.** The sensor counts in `README.md` and in
  `documentation/entities/` are per device type. If your PR adds sensors, adjust
  the numbers in the same PR.
- **A `CHANGELOG.md` entry** and a green test suite.

Adding one sensor therefore touches around eight files, not one. That is normal
here and not a sign you did something wrong.

## Pull Request Guidelines

1. **Fork** the repository and create a feature branch from `main`.
2. Keep each PR focused -- one feature or fix per PR.
3. **Include tests** for new functionality or bug fixes.
4. **Update `CHANGELOG.md`** with your changes under an appropriate category (Added, Changed, Fixed, Removed).
5. Write clear, descriptive commit messages (e.g., `feat: add battery temperature sensor for Delta 2 Max`).
6. Ensure all tests pass before requesting review.

## Reporting Issues

Please use [GitHub Issues](https://github.com/shuette42/ecoflow-energy-ha/issues) and include:

- **Home Assistant version** and **integration version**
- **Device type** (PowerOcean, Delta 2 Max, Smart Plug) and firmware version if known
- **Relevant log entries** from Home Assistant (Settings > System > Logs)
- **Steps to reproduce** the issue
- **Expected vs. actual behavior**

## Questions

For general questions about setup or usage, open a [Discussion](https://github.com/shuette42/ecoflow-energy-ha/discussions) rather than an issue.
