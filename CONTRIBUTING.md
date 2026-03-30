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
    coordinator.py       # DataUpdateCoordinator (HTTP polling + MQTT push)
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
