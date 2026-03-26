"""Tests for diagnostics — verifies no credentials are exposed."""

import json
import re


def test_diagnostics_redacts_credentials():
    """The diagnostics module must REDACT all credential fields."""
    with open("custom_components/ecoflow_energy/diagnostics.py") as f:
        source = f.read()

    # These keys must appear as REDACTED in the output dict
    for key in ("access_key", "secret_key", "email", "password"):
        pattern = rf'"{key}":\s*REDACTED'
        assert re.search(pattern, source), f'"{key}" not REDACTED in diagnostics.py'

    # The module must never import or reference CONF_ACCESS_KEY etc.
    # (except in the REDACTED context)
    for conf_name in ("CONF_ACCESS_KEY", "CONF_SECRET_KEY", "CONF_EMAIL", "CONF_PASSWORD"):
        assert conf_name not in source, f"{conf_name} must not appear in diagnostics.py"


def test_diagnostics_no_credential_values_in_output():
    """Static analysis: diagnostics must never do entry.data[CONF_ACCESS_KEY] etc."""
    with open("custom_components/ecoflow_energy/diagnostics.py") as f:
        source = f.read()

    # Must not access raw credential data from entry.data
    dangerous_patterns = [
        r'entry\.data\[.*(ACCESS|SECRET|EMAIL|PASSWORD)',
        r'entry\.data\.get\(.*(access_key|secret_key|email|password)',
    ]
    for pattern in dangerous_patterns:
        assert not re.search(pattern, source, re.IGNORECASE), \
            f"diagnostics.py must not access raw credentials: {pattern}"
