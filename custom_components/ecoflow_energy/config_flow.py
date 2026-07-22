"""Config flow for EcoFlow Energy integration.

Step 1: User selects auth type (Developer API keys or App login)
Step 2a (Developer): User enters access_key + secret_key
Step 2b (App): User enters email + password
Step 3: User selects devices from auto-discovered list
Step 4: Mode selection - Standard (default) or Enhanced (WSS real-time)
        Enhanced requires email + password and shows a disclaimer.
Step 5: Config entry created
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback

from .config_flow_options import OptionsFlowMixin
from .config_flow_reauth import ReauthFlowMixin
from .config_flow_reconfigure import ReconfigureFlowMixin
from .config_flow_setup import SetupFlowMixin
from .const import AUTH_METHOD_DEVELOPER, DOMAIN, MODE_STANDARD

# hassfest requires config_flow to be a literal file - do not convert this
# module into a config_flow/ package.


class EcoFlowEnergyConfigFlow(
    SetupFlowMixin, ReauthFlowMixin, ReconfigureFlowMixin, ConfigFlow, domain=DOMAIN
):
    """Handle a config flow for EcoFlow Energy."""

    VERSION = 3  # Must match CONFIG_VERSION in __init__.py

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> EcoFlowOptionsFlow:
        """Get the options flow handler."""
        return EcoFlowOptionsFlow()

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._access_key: str = ""
        self._secret_key: str = ""
        self._email: str = ""
        self._password: str = ""
        self._user_id: str = ""
        self._mode: str = MODE_STANDARD
        self._auth_type: str = AUTH_METHOD_DEVELOPER
        self._devices: list[dict[str, Any]] = []
        self._selected_devices: list[dict[str, Any]] = []


class EcoFlowOptionsFlow(OptionsFlowMixin, OptionsFlow):
    """Handle options for EcoFlow Energy."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._all_devices: list[dict[str, Any]] = []
        self._pending_mode: str = ""
        self._pending_devices: list[str] = []
