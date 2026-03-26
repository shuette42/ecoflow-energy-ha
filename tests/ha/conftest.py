"""Shared fixtures for HA integration tests.

These tests use pytest-homeassistant-custom-component which provides
a real (in-memory) Home Assistant instance via the ``hass`` fixture.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture(autouse=True)
def _record_threads_before_test():
    """Record threads before test so the framework's leak-check only sees new ones.

    pytest-homeassistant-custom-component checks that no non-DummyThread
    outlives the test. HA Core's _run_safe_shutdown_loop daemon thread can
    linger on slow CI runners. We join it in teardown before the framework check.
    """
    before = set(threading.enumerate())
    yield
    for thread in threading.enumerate():
        if thread not in before and "_run_safe_shutdown_loop" in thread.name:
            thread.join(timeout=5)

from custom_components.ecoflow_energy.const import (  # noqa: E402
    CONF_ACCESS_KEY,
    CONF_DEVICES,
    CONF_EMAIL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SECRET_KEY,
    CONF_USER_ID,
    DEVICE_TYPE_DELTA,
    DEVICE_TYPE_POWEROCEAN,
    DOMAIN,
    MODE_ENHANCED,
    MODE_STANDARD,
)


# ---------------------------------------------------------------------------
# Enable custom integration discovery (required by pytest-homeassistant-custom-component)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable HA to discover custom_components/ecoflow_energy."""
    yield


# ---------------------------------------------------------------------------
# Device info fixtures
# ---------------------------------------------------------------------------

MOCK_DELTA_DEVICE: dict[str, Any] = {
    "sn": "DAEBK5ZZ12340001",
    "name": "Delta 2 Max",
    "product_name": "Delta 2 Max",
    "device_type": DEVICE_TYPE_DELTA,
    "online": 1,
}

MOCK_POWEROCEAN_DEVICE: dict[str, Any] = {
    "sn": "HW52ZAB412340001",
    "name": "PowerOcean",
    "product_name": "PowerOcean",
    "device_type": DEVICE_TYPE_POWEROCEAN,
    "online": 1,
}

MOCK_MQTT_CREDENTIALS: dict[str, str] = {
    "certificateAccount": "test_cert_account",
    "certificatePassword": "test_cert_password",
    "url": "mqtt-e.ecoflow.com",
    "port": "8883",
    "protocol": "mqtts",
}


# ---------------------------------------------------------------------------
# Config entry factories
# ---------------------------------------------------------------------------


@pytest.fixture
def standard_config_entry() -> MockConfigEntry:
    """Create a Standard Mode config entry for a Delta 2 Max."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_access_key",
            CONF_SECRET_KEY: "test_secret_key",
            CONF_MODE: MODE_STANDARD,
            CONF_DEVICES: [MOCK_DELTA_DEVICE],
        },
        unique_id="test_access_key",
    )


@pytest.fixture
def enhanced_config_entry() -> MockConfigEntry:
    """Create an Enhanced Mode config entry for a PowerOcean."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="EcoFlow Energy",
        data={
            CONF_ACCESS_KEY: "test_access_key",
            CONF_SECRET_KEY: "test_secret_key",
            CONF_MODE: MODE_ENHANCED,
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "test_password",
            CONF_USER_ID: "user123",
            CONF_DEVICES: [MOCK_POWEROCEAN_DEVICE],
        },
        unique_id="test_access_key",
    )


@pytest.fixture
def mock_iot_api():
    """Patch IoTApiClient to return mock credentials and devices."""
    with patch(
        "custom_components.ecoflow_energy.coordinator.IoTApiClient",
    ) as cls:
        instance = cls.return_value
        instance.get_mqtt_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
        instance.get_device_list = AsyncMock(return_value=[MOCK_DELTA_DEVICE])
        instance.refresh_credentials = AsyncMock(return_value=MOCK_MQTT_CREDENTIALS)
        yield instance


@pytest.fixture
def mock_mqtt_client():
    """Patch EcoFlowMQTTClient to a no-op mock."""
    with patch(
        "custom_components.ecoflow_energy.coordinator.EcoFlowMQTTClient",
    ) as cls:
        instance = cls.return_value
        instance.create_client.return_value = True
        instance.connect.return_value = True
        instance.start_loop.return_value = None
        instance.is_connected.return_value = True
        instance.disconnect.return_value = None
        instance.cert_account = "test_cert_account"
        instance.wss_mode = False
        instance.last_connect_time = 0
        instance.reconnect_attempts = 0
        instance.publish.return_value = True
        instance.send_energy_stream_switch.return_value = None
        instance.try_reconnect.return_value = None
        yield instance


@pytest.fixture
def mock_http_client():
    """Patch EcoFlowHTTPQuota to return mock data."""
    with patch(
        "custom_components.ecoflow_energy.coordinator.EcoFlowHTTPQuota",
    ) as cls:
        instance = cls.return_value
        instance.get_quota_all = AsyncMock(return_value={
            "pd.soc": 75,
            "pd.wattsInSum": 200,
            "pd.wattsOutSum": 100,
        })
        yield instance


@pytest.fixture
def mock_enhanced_auth():
    """Patch enhanced_auth to return mock login/credentials."""
    with patch(
        "custom_components.ecoflow_energy.coordinator.EcoFlowDeviceCoordinator._fetch_enhanced_credentials",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = (MOCK_MQTT_CREDENTIALS, "user123")
        yield mock
